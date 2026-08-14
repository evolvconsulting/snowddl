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

from snowddl.blueprint import DataType, AccountObjectIdent, ObjectType
from snowddl.converter.table import TableConverter
from snowddl.converter.function import FunctionConverter
from snowddl.parser.table import col_type_re, table_json_schema
from snowddl.parser.procedure import procedure_json_schema
from snowddl.parser.function import function_json_schema
from snowddl.resolver.abc_schema_object_resolver import AbstractSchemaObjectResolver


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


def test_grants_absent_never_touches_the_engine():
    # No grants field -> nothing read, nothing issued, for every object type.
    issued = []
    fake = SimpleNamespace(
        engine=SimpleNamespace(
            execute_safe_ddl=lambda *a, **k: issued.append(1),
            execute_meta=lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not read grants")),
        ),
        config=SimpleNamespace(env_prefix=""),
        get_object_type=lambda: ObjectType.PROCEDURE,
        NON_GRANTABLE_OBJECT_TYPES=AbstractSchemaObjectResolver.NON_GRANTABLE_OBJECT_TYPES,
    )
    assert _reconcile(fake, SimpleNamespace(grants=None, full_name="X")) is False
    assert issued == []


def test_procedure_grants_now_authoritative_not_additive():
    # PATCH #10 CHANGES PATCH #8's BEHAVIOUR ON PURPOSE. Additive could re-grant but
    # never revoke, so a procedure could accumulate a grantee nobody declared and the
    # config would keep looking correct. Now the declared list IS the live list.
    #
    # Verified safe before the switch (2026-08-13): all 62 procedure/function objects in
    # OIE that declare grants hold exactly USAGE -> OIE_ENGINEER live, matching their
    # declaration, so this revokes nothing that exists today.
    live = [
        {"privilege": "USAGE", "granted_to": "ROLE", "grantee_name": "OIE_ENGINEER"},
        {"privilege": "USAGE", "granted_to": "ROLE", "grantee_name": "SOMEBODY_UNDECLARED"},
    ]
    fake, issued = _fake_table_resolver(live, object_type=ObjectType.PROCEDURE)
    changed = _reconcile(fake, SimpleNamespace(grants={"USAGE": ["OIE_ENGINEER"]}, full_name="SP_X"))

    assert changed is True
    assert issued["safe"] == [], "OIE_ENGINEER already holds it; nothing to grant"
    assert len(issued["unsafe"]) == 1
    template, params = issued["unsafe"][0]
    assert params["object_type"] == "PROCEDURE"
    assert str(params["role"]) == "SOMEBODY_UNDECLARED"


def test_the_grant_statement_names_the_object_type():
    # One helper serves 31 object types, so the type must come from ObjectType rather
    # than a hardcoded keyword. singular_for_grant exists because a few types spell it
    # differently in GRANT than in SHOW.
    for object_type, expected in [
        (ObjectType.VIEW, "VIEW"),
        (ObjectType.CORTEX_SEARCH_SERVICE, "CORTEX SEARCH SERVICE"),
        (ObjectType.MATERIALIZED_VIEW, "MATERIALIZED VIEW"),
        (ObjectType.STAGE, "STAGE"),
        (ObjectType.TASK, "TASK"),
    ]:
        fake, issued = _fake_table_resolver([], object_type=object_type)
        _reconcile(fake, SimpleNamespace(grants={"USAGE": ["OIE_ENGINEER"]}, full_name="X"))
        assert issued["safe"][0][1]["object_type"] == expected, object_type


def test_constraints_are_not_grantable():
    # A constraint is a property of a table, not a grantable object. Declaring grants on
    # one must be inert rather than emitting SQL Snowflake would reject.
    for object_type in (ObjectType.PRIMARY_KEY, ObjectType.UNIQUE_KEY, ObjectType.FOREIGN_KEY):
        fake, issued = _fake_table_resolver([], object_type=object_type)
        changed = _reconcile(fake, SimpleNamespace(grants={"SELECT": ["R"]}, full_name="X"))
        assert changed is False, object_type
        assert issued == {"safe": [], "unsafe": []}, object_type


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


def _reconcile(fake, bp, is_new=False):
    """Call the shared reconciler the way the resolver base does.

    Patch #10 moved this off TableResolver onto AbstractSchemaObjectResolver, so every
    object type runs the same code. The tests below still describe table cases because
    that is where the semantics were first pinned; they now cover all 31 types.
    """
    return AbstractSchemaObjectResolver._reconcile_object_grants(fake, bp, object_exists=not is_new)


def _fake_table_resolver(live_rows, env_prefix="", object_type=ObjectType.TABLE):
    issued = {"safe": [], "unsafe": []}
    fake = SimpleNamespace(
        engine=SimpleNamespace(
            execute_meta=lambda template, params: live_rows,
            execute_safe_ddl=lambda template, params: issued["safe"].append((template, params)),
            execute_unsafe_ddl=lambda template, params: issued["unsafe"].append((template, params)),
        ),
        config=SimpleNamespace(env_prefix=env_prefix),
        get_object_type=lambda: object_type,
        NON_GRANTABLE_OBJECT_TYPES=AbstractSchemaObjectResolver.NON_GRANTABLE_OBJECT_TYPES,
    )
    return fake, issued


def test_table_grants_absent_is_unmanaged():
    # No grants key -> the resolver never even reads the live grants. A table that
    # says nothing about its grants must stay exactly as it is.
    fake, issued = _fake_table_resolver([{"privilege": "INSERT", "granted_to": "ROLE", "grantee_name": "R"}])
    changed = _reconcile(fake, SimpleNamespace(grants=None, full_name="T"))
    assert changed is False
    assert issued == {"safe": [], "unsafe": []}


def test_table_grants_empty_list_revokes_every_holder():
    # The OIE-951 case: INSERT declared as held by nobody, live held by one role.
    live = [
        {"privilege": "INSERT", "granted_to": "ROLE", "grantee_name": "OIE_ENGINEER"},
        {"privilege": "OWNERSHIP", "granted_to": "ROLE", "grantee_name": "OIE_ADMIN"},
    ]
    fake, issued = _fake_table_resolver(live)
    changed = _reconcile(fake, SimpleNamespace(grants={"INSERT": []}, full_name="T"))

    assert changed is True
    assert issued["safe"] == []
    assert len(issued["unsafe"]) == 1
    template, params = issued["unsafe"][0]
    assert template == "REVOKE {privilege:r} ON {object_type:r} {full_name:i} FROM ROLE {role:i}"
    assert params["object_type"] == "TABLE"
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
    _reconcile(fake, SimpleNamespace(grants={"INSERT": []}, full_name="T"))

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
    changed = _reconcile(fake, SimpleNamespace(grants={"INSERT": [], "OWNERSHIP": []}, full_name="T"))

    assert changed is False
    assert issued == {"safe": [], "unsafe": []}


def test_table_grants_grants_missing_role():
    fake, issued = _fake_table_resolver([])
    changed = _reconcile(
        fake, SimpleNamespace(grants={"SELECT": ["OIE_VALIDATION"]}, full_name="T")
    )

    assert changed is True
    assert issued["unsafe"] == []
    template, params = issued["safe"][0]
    assert template == "GRANT {privilege:r} ON {object_type:r} {full_name:i} TO ROLE {role:i}"
    assert params["object_type"] == "TABLE"
    assert isinstance(params["role"], AccountObjectIdent)
    assert str(params["role"]) == "OIE_VALIDATION"


def test_table_grants_in_sync_is_a_noop():
    # Already correct -> no statements, and compare_object must stay NOCHANGE.
    live = [{"privilege": "INSERT", "granted_to": "ROLE", "grantee_name": "OIE_ENGINEER"}]
    fake, issued = _fake_table_resolver(live)
    changed = _reconcile(
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
    changed = _reconcile(
        fake, SimpleNamespace(grants={"INSERT": ["OIE_ENGINEER"]}, full_name="T")
    )

    assert changed is False, "prefixed live grantee must match the bare declared role"

    fake, issued = _fake_table_resolver([], env_prefix="DEV__")
    _reconcile(fake, SimpleNamespace(grants={"INSERT": ["OIE_ENGINEER"]}, full_name="T"))
    assert str(issued["safe"][0][1]["role"]) == "DEV__OIE_ENGINEER"


def test_table_grants_new_table_never_reads_live_grants():
    # A table being CREATEd has no grants to read, and under `plan` it does not
    # exist at all -- SHOW GRANTS would abort the plan. is_new must skip the read
    # and still issue the declared grants.
    def _explode(template, params):
        raise AssertionError("SHOW GRANTS must not run for an object that does not exist yet")

    issued = {"safe": [], "unsafe": []}
    fake = SimpleNamespace(
        engine=SimpleNamespace(
            execute_meta=_explode,
            execute_safe_ddl=lambda template, params: issued["safe"].append((template, params)),
            execute_unsafe_ddl=lambda template, params: issued["unsafe"].append((template, params)),
        ),
        config=SimpleNamespace(env_prefix=""),
        get_object_type=lambda: ObjectType.TABLE,
        NON_GRANTABLE_OBJECT_TYPES=AbstractSchemaObjectResolver.NON_GRANTABLE_OBJECT_TYPES,
    )
    bp = SimpleNamespace(grants={"SELECT": ["OIE_VALIDATION"], "INSERT": []}, full_name="T")
    changed = _reconcile(fake, bp, is_new=True)

    assert changed is True
    assert len(issued["safe"]) == 1
    assert issued["unsafe"] == [], "nothing to revoke on a table that has no grants yet"


# --- Patch #10: coverage across every schema-object type ---------------------
# The point of #10 is that no object type is left out. A per-type test would pass
# while one parser silently lacked the key, so these enumerate instead of listing.


def _schema_object_parsers():
    import importlib
    import pkgutil
    import snowddl.parser as parser_pkg

    found = {}
    for mod_info in pkgutil.iter_modules(parser_pkg.__path__):
        if mod_info.name.startswith("_") or mod_info.name == "abc_parser":
            continue
        mod = importlib.import_module(f"snowddl.parser.{mod_info.name}")
        for attr in dir(mod):
            # `<module>_json_schema` is the object's own schema. The prefix check matters:
            # several parsers IMPORT database_json_schema / schema_json_schema from
            # parser.schema, and those describe a database or schema, not this object.
            if not attr.endswith("_json_schema") or not attr.startswith(f"{mod_info.name}_"):
                continue
            schema = getattr(mod, attr)
            if isinstance(schema, dict) and "properties" in schema:
                found.setdefault(mod_info.name, {})[attr] = schema
    return found


# Account-level objects: their grants are modelled through SnowDDL's role engine and,
# in OIE, owned by Terraform. Deliberately out of scope for object-level `grants:`.
ACCOUNT_LEVEL_PARSERS = {
    "account_params", "business_role", "database", "database_role",
    "external_access_integration", "network_policy", "outbound_share",
    "resource_monitor", "schema", "technical_role", "user", "warehouse",
    "account_policy", "placeholder",
}


def test_every_schema_object_parser_accepts_grants():
    missing = []
    checked = 0
    for mod_name, schemas in _schema_object_parsers().items():
        if mod_name in ACCOUNT_LEVEL_PARSERS:
            continue
        for schema_name, schema in schemas.items():
            checked += 1
            if "grants" not in schema["properties"]:
                missing.append(f"{mod_name}.{schema_name}")

    # A discovery bug (renamed module, changed schema convention) would empty the sweep,
    # and an empty sweep passes the assertion below while proving nothing.
    assert checked >= 28, f"parser discovery found only {checked} schema-object parsers -- sweep is broken"

    assert not missing, (
        "these schema-object parsers still cannot express object grants, which is the "
        f"whole point of patch #10: {sorted(missing)}"
    )


def test_no_parser_requires_a_non_empty_role_list():
    # minItems must be 0 everywhere: an empty list is how a config says "no role holds
    # this", and patch #8 shipped minItems 1, which made that unsayable.
    offenders = []
    for mod_name, schemas in _schema_object_parsers().items():
        for schema_name, schema in schemas.items():
            g = schema["properties"].get("grants")
            if g and g.get("additionalProperties", {}).get("minItems", 0) != 0:
                offenders.append(f"{mod_name}.{schema_name}")
    assert not offenders, f"grants role list must accept an empty list: {offenders}"


@pytest.mark.parametrize(
    "mod_name",
    ["view", "cortex_search_service", "dynamic_table", "stage", "task", "stream", "sequence"],
)
def test_representative_types_validate_an_empty_and_a_populated_block(mod_name):
    # Spot-check that the injected schema actually validates, not merely exists.
    schemas = _schema_object_parsers()[mod_name]
    schema = next(s for s in schemas.values() if "grants" in s["properties"])
    sub = {"type": "object", "properties": {"grants": schema["properties"]["grants"]}}
    jsonschema.validate({"grants": {"USAGE": []}}, sub)
    jsonschema.validate({"grants": {"USAGE": ["OIE_ENGINEER"]}}, sub)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"grants": {"USAGE": "OIE_ENGINEER"}}, sub)


def test_every_resolver_is_still_instantiable():
    # Patch #10 added methods to AbstractResolver, and an @abstractmethod landing on the
    # wrong one makes EVERY resolver abstract -- `snowddl plan` then dies at startup with
    # "Can't instantiate abstract class SchemaResolver". That is exactly what happened
    # while 41 unit tests were green, because none of them builds a real resolver: the
    # grant tests drive _reconcile_object_grants on a stub. This asserts the shape the
    # others cannot see.
    import inspect
    import snowddl.resolver as resolver_pkg
    from snowddl.resolver.abc_resolver import AbstractResolver

    still_abstract = {}

    for name in dir(resolver_pkg):
        cls = getattr(resolver_pkg, name)

        if not inspect.isclass(cls) or not issubclass(cls, AbstractResolver):
            continue

        # The abc_* base classes are meant to stay abstract.
        if cls.__module__.rsplit(".", 1)[-1].startswith("abc_"):
            continue

        if getattr(cls, "__abstractmethods__", frozenset()):
            still_abstract[name] = sorted(cls.__abstractmethods__)

    assert not still_abstract, f"concrete resolvers left abstract: {still_abstract}"
