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
from snowddl.parser.table import col_type_re, table_json_schema
from snowddl.parser.procedure import procedure_json_schema
from snowddl.parser.function import function_json_schema
from snowddl.resolver.procedure import ProcedureResolver
from snowddl.resolver.table import TableResolver


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


# --- Patch #9: authoritative object-level grants on tables -----------------
# Tables CAN carry object grants, but nothing declared them, so a hand-issued
# GRANT was invisible to review and survived every apply. A `grants` field on a
# table is AUTHORITATIVE for each privilege it names and inert for every other
# one: the declared role list becomes the live grantee list. An empty list is
# the load-bearing case -- it says "no role holds this" and revokes whoever does.


def _table_doc(**extra):
    return {"columns": {"ID": "number(38,0)"}, **extra}


def test_table_grants_field_accepted():
    jsonschema.validate(_table_doc(grants={"INSERT": ["OIE_ENGINEER"]}), table_json_schema)


def test_table_grants_empty_list_accepted():
    # The whole point of the patch: "nobody holds INSERT" must be expressible.
    # The procedure schema's minItems=1 would reject this.
    jsonschema.validate(_table_doc(grants={"INSERT": [], "UPDATE": []}), table_json_schema)


def test_table_grants_value_must_be_role_list():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(_table_doc(grants={"INSERT": "OIE_ENGINEER"}), table_json_schema)


def _fake_table_resolver(live_rows, env_prefix=""):
    issued = {"safe": [], "unsafe": []}
    fake = SimpleNamespace(
        engine=SimpleNamespace(
            execute_meta=lambda template, params: live_rows,
            execute_safe_ddl=lambda template, params: issued["safe"].append((template, params)),
            execute_unsafe_ddl=lambda template, params: issued["unsafe"].append((template, params)),
        ),
        config=SimpleNamespace(env_prefix=env_prefix),
    )
    return fake, issued


def test_table_grants_absent_is_unmanaged():
    # No grants key -> the resolver never even reads the live grants. A table that
    # says nothing about its grants must stay exactly as it is.
    fake, issued = _fake_table_resolver([{"privilege": "INSERT", "granted_to": "ROLE", "grantee_name": "R"}])
    changed = TableResolver._apply_object_grants(fake, SimpleNamespace(grants=None, full_name="T"))
    assert changed is False
    assert issued == {"safe": [], "unsafe": []}


def test_table_grants_empty_list_revokes_every_holder():
    # The OIE-951 case: INSERT declared as held by nobody, live held by one role.
    live = [
        {"privilege": "INSERT", "granted_to": "ROLE", "grantee_name": "OIE_ENGINEER"},
        {"privilege": "OWNERSHIP", "granted_to": "ROLE", "grantee_name": "OIE_ADMIN"},
    ]
    fake, issued = _fake_table_resolver(live)
    changed = TableResolver._apply_object_grants(fake, SimpleNamespace(grants={"INSERT": []}, full_name="T"))

    assert changed is True
    assert issued["safe"] == []
    assert len(issued["unsafe"]) == 1
    template, params = issued["unsafe"][0]
    assert template == "REVOKE {privilege:r} ON TABLE {full_name:i} FROM ROLE {role:i}"
    assert params["privilege"] == "INSERT"
    assert str(params["role"]) == "OIE_ENGINEER"


def test_table_grants_leaves_undeclared_privileges_alone():
    # SELECT is granted live to six roles by Terraform's schema-wide resources and
    # is NOT named in config. Reconciling it would start a revoke/re-grant
    # ping-pong between the two tools, so it must be ignored entirely.
    live = [
        {"privilege": "SELECT", "granted_to": "ROLE", "grantee_name": "OIE_USERS"},
        {"privilege": "SELECT", "granted_to": "ROLE", "grantee_name": "OIE_MDM_READER"},
        {"privilege": "INSERT", "granted_to": "ROLE", "grantee_name": "OIE_ENGINEER"},
    ]
    fake, issued = _fake_table_resolver(live)
    TableResolver._apply_object_grants(fake, SimpleNamespace(grants={"INSERT": []}, full_name="T"))

    assert len(issued["unsafe"]) == 1
    assert issued["unsafe"][0][1]["privilege"] == "INSERT"


def test_table_grants_ignores_ownership_and_user_grants():
    # Ownership is transferred, not granted; user grants belong to SCIM. Neither
    # may be revoked by a config that names the privilege.
    live = [
        {"privilege": "OWNERSHIP", "granted_to": "ROLE", "grantee_name": "OIE_ADMIN"},
        {"privilege": "INSERT", "granted_to": "USER", "grantee_name": "SOMEBODY"},
    ]
    fake, issued = _fake_table_resolver(live)
    changed = TableResolver._apply_object_grants(fake, SimpleNamespace(grants={"INSERT": [], "OWNERSHIP": []}, full_name="T"))

    assert changed is False
    assert issued == {"safe": [], "unsafe": []}


def test_table_grants_grants_missing_role():
    fake, issued = _fake_table_resolver([])
    changed = TableResolver._apply_object_grants(
        fake, SimpleNamespace(grants={"SELECT": ["OIE_VALIDATION"]}, full_name="T")
    )

    assert changed is True
    assert issued["unsafe"] == []
    template, params = issued["safe"][0]
    assert template == "GRANT {privilege:r} ON TABLE {full_name:i} TO ROLE {role:i}"
    assert isinstance(params["role"], AccountObjectIdent)
    assert str(params["role"]) == "OIE_VALIDATION"


def test_table_grants_in_sync_is_a_noop():
    # Already correct -> no statements, and compare_object must stay NOCHANGE.
    live = [{"privilege": "INSERT", "granted_to": "ROLE", "grantee_name": "OIE_ENGINEER"}]
    fake, issued = _fake_table_resolver(live)
    changed = TableResolver._apply_object_grants(
        fake, SimpleNamespace(grants={"INSERT": ["OIE_ENGINEER"]}, full_name="T")
    )

    assert changed is False
    assert issued == {"safe": [], "unsafe": []}


def test_table_grants_env_prefix_is_applied_exactly_once():
    # A prefixed env compares in the LIVE namespace but re-grants from the BARE
    # name. Getting this wrong yields OIE_OIE_ENGINEER, which does not exist and
    # fails at apply time rather than in review.
    live = [{"privilege": "INSERT", "granted_to": "ROLE", "grantee_name": "DEV__OIE_ENGINEER"}]
    fake, issued = _fake_table_resolver(live, env_prefix="DEV__")
    changed = TableResolver._apply_object_grants(
        fake, SimpleNamespace(grants={"INSERT": ["OIE_ENGINEER"]}, full_name="T")
    )

    assert changed is False, "prefixed live grantee must match the bare declared role"

    fake, issued = _fake_table_resolver([], env_prefix="DEV__")
    TableResolver._apply_object_grants(fake, SimpleNamespace(grants={"INSERT": ["OIE_ENGINEER"]}, full_name="T"))
    assert str(issued["safe"][0][1]["role"]) == "DEV__OIE_ENGINEER"


def test_table_grants_new_table_never_reads_live_grants():
    # A table being CREATEd has no grants to read, and under `plan` it does not
    # exist at all -- SHOW GRANTS would abort the plan. is_new must skip the read
    # and still issue the declared grants.
    def _explode(template, params):
        raise AssertionError("SHOW GRANTS must not run for a table that does not exist yet")

    issued = {"safe": [], "unsafe": []}
    fake = SimpleNamespace(
        engine=SimpleNamespace(
            execute_meta=_explode,
            execute_safe_ddl=lambda template, params: issued["safe"].append((template, params)),
            execute_unsafe_ddl=lambda template, params: issued["unsafe"].append((template, params)),
        ),
        config=SimpleNamespace(env_prefix=""),
    )
    bp = SimpleNamespace(grants={"SELECT": ["OIE_VALIDATION"], "INSERT": []}, full_name="T")
    changed = TableResolver._apply_object_grants(fake, bp, is_new=True)

    assert changed is True
    assert len(issued["safe"]) == 1
    assert issued["unsafe"] == [], "nothing to revoke on a table that has no grants yet"
