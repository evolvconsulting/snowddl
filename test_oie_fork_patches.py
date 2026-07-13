"""OIE fork patch (0.67.5-oie.1) regression tests.

Covers the two parser/emitter fixes carried on top of upstream 0.67.5. Pure-unit
(no Snowflake connection): they exercise the exact emit/parse paths that halted
`snowddl-convert` / `snowddl plan` against the OIE account.

  * Fix #1 — VECTOR emitter round-trip: DESC TABLE emits "VECTOR(FLOAT, 1536)" (space
    after comma); the table parser's col_type_re rejects that space, so the emitted
    YAML failed to re-parse and `plan` halted on the 13 real VECTOR columns. The
    emitter now normalises to the no-space form the parser accepts.
  * Fix #2 — bare-VARCHAR returns: DESC PROCEDURE/FUNCTION can report a bare "VARCHAR"
    (no length), which DataType() rejected -> convert aborted (exit 8). Now defaulted.
"""

from types import SimpleNamespace

import jsonschema
import pytest

from snowddl.blueprint import DataType, AccountObjectIdent
from snowddl.converter.table import TableConverter
from snowddl.converter.function import FunctionConverter
from snowddl.parser.table import col_type_re
from snowddl.parser.procedure import procedure_json_schema
from snowddl.parser.function import function_json_schema
from snowddl.resolver.procedure import ProcedureResolver


# --- Fix #1: VECTOR emitter round-trip -------------------------------------


@pytest.mark.parametrize(
    "desc_type,expected",
    [
        ("VECTOR(FLOAT, 1536)", "VECTOR(FLOAT,1536)"),  # the real OIE embedding cols (1536-d)
        ("VECTOR(FLOAT, 256)", "VECTOR(FLOAT,256)"),
        ("VECTOR(INT, 16)", "VECTOR(INT,16)"),
        ("vector(float, 1536)", "VECTOR(FLOAT,1536)"),  # case-insensitive
        ("VECTOR(FLOAT,1536)", "VECTOR(FLOAT,1536)"),  # already no-space -> unchanged
    ],
)
def test_vector_emitter_drops_space(desc_type, expected):
    assert TableConverter._normalise_col_type(None, desc_type) == expected


def test_vector_emitter_output_reparses():
    # The point of the fix: the emitted type must be accepted by BOTH the table column
    # parser regex and DataType. Upstream 0.67.5 rejected the spaced form -> plan exit 8.
    normalised = TableConverter._normalise_col_type(None, "VECTOR(FLOAT, 1536)")
    assert col_type_re.match(normalised) is not None
    assert str(DataType(normalised)) == "VECTOR(FLOAT,1536)"


def test_non_vector_types_untouched():
    # Targeted fix: it must not rewrite any other type.
    for t in ("NUMBER(38,0)", "VARCHAR(16777216)", "TIMESTAMP_NTZ(9)", "BOOLEAN", "VARIANT"):
        assert TableConverter._normalise_col_type(None, t) == t


# --- Fix #2: bare-VARCHAR returns ------------------------------------------


def test_bare_varchar_return_defaults_length():
    # DESC PROCEDURE on OIE's SP_APPLY_MERGE_LEDGER returns bare "VARCHAR".
    # Upstream DataType("VARCHAR") raised ValueError -> convert exit 8.
    result = FunctionConverter._get_returns_single(None, {"returns": "VARCHAR"})
    assert result == "VARCHAR(16777216)"
    assert str(DataType(result)) == "VARCHAR(16777216)"


def test_sized_return_unchanged():
    assert FunctionConverter._get_returns_single(None, {"returns": "NUMBER(38,0)"}) == "NUMBER(38,0)"


def test_propertyless_return_unchanged():
    # FLOAT / VARIANT have zero properties -> no defaulting, no crash.
    assert FunctionConverter._get_returns_single(None, {"returns": "FLOAT"}) == "FLOAT"
    assert FunctionConverter._get_returns_single(None, {"returns": "VARIANT"}) == "VARIANT"


def test_bare_varchar_upstream_would_crash():
    # Guard: prove the pre-patch code path (raw DataType on a bare sized type) really failed,
    # so this test is non-vacuous.
    with pytest.raises(ValueError):
        DataType("VARCHAR")


# --- Patch #8: object-level grants on procedures/functions -----------------
# Procedures/functions cannot COPY GRANTS, so CREATE OR REPLACE drops their
# grants. A `grants` config field is re-applied on every create/replace (self-
# heal, additive) and captured on import so the round-trip is reproducible.


@pytest.mark.parametrize("schema", [procedure_json_schema, function_json_schema])
def test_grants_field_accepted(schema):
    doc = {"returns": "VARCHAR(16777216)", "body": "x", "grants": {"USAGE": ["OIE_ENGINEER"]}}
    jsonschema.validate(doc, schema)  # must not raise


@pytest.mark.parametrize("schema", [procedure_json_schema, function_json_schema])
def test_grants_value_must_be_role_list(schema):
    # A privilege must map to an array of role names, not a bare string.
    doc = {"returns": "VARCHAR(16777216)", "body": "x", "grants": {"USAGE": "OIE_ENGINEER"}}
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(doc, schema)


def _fake_converter(rows):
    return SimpleNamespace(engine=SimpleNamespace(execute_meta=lambda template, params: rows))


def test_get_object_grants_captures_role_privileges_only():
    rows = [
        {"privilege": "USAGE", "granted_to": "ROLE", "grantee_name": "OIE_ENGINEER"},
        {"privilege": "OWNERSHIP", "granted_to": "ROLE", "grantee_name": "OIE_ADMIN"},  # ownership excluded
        {"privilege": "USAGE", "granted_to": "USER", "grantee_name": "SOMEBODY"},  # user grant excluded (SCIM)
        {"privilege": "USAGE", "granted_to": "ROLE", "grantee_name": "OIE_ADMIN"},
    ]
    result = FunctionConverter._get_object_grants(_fake_converter(rows), "PROCEDURE", "OIE", "MDM", "SP_X", "VARCHAR")
    assert result == {"USAGE": ["OIE_ADMIN", "OIE_ENGINEER"]}  # sorted; ownership + user grants dropped


def test_get_object_grants_none_when_no_role_privileges():
    rows = [{"privilege": "OWNERSHIP", "granted_to": "ROLE", "grantee_name": "OIE_ADMIN"}]
    assert FunctionConverter._get_object_grants(_fake_converter(rows), "FUNCTION", "OIE", "MDM", "F", "") is None


def test_apply_object_grants_noop_when_absent():
    # No grants field -> no GRANT issued (never touches the engine).
    issued = []
    fake = SimpleNamespace(
        engine=SimpleNamespace(execute_safe_ddl=lambda *a, **k: issued.append(1)),
        config=SimpleNamespace(env_prefix=""),
    )
    ProcedureResolver._apply_object_grants(fake, SimpleNamespace(grants=None, full_name="X"))
    assert issued == []


def test_apply_object_grants_issues_one_grant_per_role():
    issued = []
    fake = SimpleNamespace(
        engine=SimpleNamespace(execute_safe_ddl=lambda template, params: issued.append((template, params))),
        config=SimpleNamespace(env_prefix=""),
    )
    bp = SimpleNamespace(grants={"USAGE": ["OIE_ENGINEER", "OIE_ADMIN"]}, full_name="FULLNAME")
    ProcedureResolver._apply_object_grants(fake, bp)

    assert len(issued) == 2
    assert all("GRANT {privilege:r} ON PROCEDURE {full_name:i} TO ROLE {role:i}" == t for t, p in issued)
    assert {p["privilege"] for t, p in issued} == {"USAGE"}
    assert all(isinstance(p["role"], AccountObjectIdent) for t, p in issued)
