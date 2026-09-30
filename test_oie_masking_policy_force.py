"""OIE fork patch (0.67.5-oie.14) regression tests: a column's masking policy changes with FORCE.

OIE-1930 PR-4 moves MART.SIGNAL.RAW_REF from MSK_SIGNAL_BODY_REMOVAL_REQUEST to
MSK_CORPUS_BODY_BY_STATE. Before this patch the new policy's resolver emitted a plain
SET MASKING POLICY, which Snowflake refuses while another policy is set, and the old
policy's resolver emitted UNSET for the reference it lost. Resolvers run in parallel, so
the column was either left unmasked between the two statements or the apply failed.

The patch, two halves:

1. A new reference on a column that carries a DIFFERENT masking policy is SET ... FORCE.
2. A reference a policy lost is not UNSET when another masking-policy blueprint claims
   the same object and first column.

Everything else must emit exactly oie.13's statements: a new reference on an unmasked
column, an unchanged reference, and a reference nobody claims.

Pure-unit: a stub engine records the formatted SQL. No Snowflake connection.
"""

from types import SimpleNamespace

from snowddl.blueprint import (
    DataType,
    Ident,
    MaskingPolicyBlueprint,
    MaskingPolicyReference,
    NameWithType,
    ObjectType,
    SchemaObjectIdent,
)
from snowddl.formatter import SnowDDLFormatter
from snowddl.resolver.masking_policy import MaskingPolicyResolver

SIGNAL = SchemaObjectIdent("", "OIE", "MART", "SIGNAL")
NEW = SchemaObjectIdent("", "OIE", "MART", "MSK_CORPUS_BODY_BY_STATE")
OLD = SchemaObjectIdent("", "OIE", "MART", "MSK_SIGNAL_BODY_REMOVAL_REQUEST")

# oie.13's SET, byte for byte, for the unforced case.
OIE13_SET = (
    'ALTER TABLE "OIE"."MART"."SIGNAL" MODIFY COLUMN "RAW_REF" SET MASKING POLICY '
    '"OIE"."MART"."MSK_CORPUS_BODY_BY_STATE" USING ("RAW_REF", "SIGNAL_ID", "IS_CHUNKED")'
)
UNSET = 'ALTER TABLE "OIE"."MART"."SIGNAL" MODIFY COLUMN "RAW_REF" UNSET MASKING POLICY'


def _bp(name, refs):
    return MaskingPolicyBlueprint(
        full_name=name,
        arguments=[NameWithType(name=Ident("VAL"), type=DataType("VARIANT"))],
        returns=DataType("VARIANT"),
        body="VAL",
        references=[
            MaskingPolicyReference(object_type=ObjectType.TABLE, object_name=obj, columns=[Ident(c) for c in cols])
            for obj, cols in refs
        ],
    )


def _row(policy, column="RAW_REF", kind="MASKING_POLICY"):
    return {
        "POLICY_DB": policy.database,
        "POLICY_SCHEMA": policy.schema,
        "POLICY_NAME": policy.name,
        "POLICY_KIND": kind,
        "REF_ENTITY_DOMAIN": "TABLE",
        "REF_DATABASE_NAME": "OIE",
        "REF_SCHEMA_NAME": "MART",
        "REF_ENTITY_NAME": "SIGNAL",
        "REF_COLUMN_NAME": column,
    }


class _Engine:
    def __init__(self, on_column, by_policy):
        # on_column: rows policy_references(ref_entity_name => ...) returns.
        # by_policy: {policy full name: rows policy_references(policy_name => ...) returns}.
        self.formatter = SnowDDLFormatter()
        self.settings = SimpleNamespace(execute_masking_policy=True)
        self.on_column = on_column
        self.by_policy = by_policy
        self.ddl = []
        self.meta = []

    def execute_meta(self, sql, params=None):
        text = self.formatter.format_sql(sql, params)
        self.meta.append(text)
        if "ref_entity_name" in text:
            return list(self.on_column)
        return list(self.by_policy.get(str(params["policy_name"]), []))

    def execute_unsafe_ddl(self, sql, params=None, condition=True):
        self.ddl.append(" ".join(self.formatter.format_sql(sql, params).split()))


def _resolver(engine, blueprints):
    r = MaskingPolicyResolver.__new__(MaskingPolicyResolver)
    r.engine = engine
    r.config = SimpleNamespace(get_blueprints_by_type=lambda cls: {str(b.full_name): b for b in blueprints})
    return r


NEW_BP = _bp(NEW, [(SIGNAL, ["RAW_REF", "SIGNAL_ID", "IS_CHUNKED"])])
OLD_BP_AFTER = _bp(OLD, [])  # PR-4: the old policy loses its RAW_REF reference


# --- half 1: the new policy's resolver ------------------------------------


def test_a_column_under_another_policy_is_set_with_force():
    engine = _Engine(on_column=[_row(OLD)], by_policy={})
    changed = _resolver(engine, [NEW_BP, OLD_BP_AFTER])._apply_policy_refs(NEW_BP)
    assert changed is True
    assert engine.ddl == [OIE13_SET + " FORCE"]


def test_an_unmasked_column_is_set_exactly_as_oie13():
    engine = _Engine(on_column=[], by_policy={})
    _resolver(engine, [NEW_BP])._apply_policy_refs(NEW_BP)
    assert engine.ddl == [OIE13_SET]


def test_a_row_access_policy_on_the_object_does_not_force():
    engine = _Engine(on_column=[_row(OLD, column=None, kind="ROW_ACCESS_POLICY")], by_policy={})
    _resolver(engine, [NEW_BP])._apply_policy_refs(NEW_BP)
    assert engine.ddl == [OIE13_SET]


def test_another_column_masked_on_the_same_table_does_not_force():
    engine = _Engine(on_column=[_row(OLD, column="SUBJECT")], by_policy={})
    _resolver(engine, [NEW_BP])._apply_policy_refs(NEW_BP)
    assert engine.ddl == [OIE13_SET]


def test_an_existing_reference_issues_nothing_and_reads_no_column():
    engine = _Engine(on_column=[_row(OLD)], by_policy={str(NEW): [_row(NEW)]})
    changed = _resolver(engine, [NEW_BP])._apply_policy_refs(NEW_BP)
    assert changed is False
    assert engine.ddl == []
    assert not any("ref_entity_name" in m for m in engine.meta)


def test_create_object_path_also_forces():
    # create_object -> _apply_policy_refs(skip_existing=True): the new policy is created
    # in the same apply that moves the column onto it.
    engine = _Engine(on_column=[_row(OLD)], by_policy={})
    _resolver(engine, [NEW_BP, OLD_BP_AFTER])._apply_policy_refs(NEW_BP, skip_existing=True)
    assert engine.ddl == [OIE13_SET + " FORCE"]


# --- half 2: the old policy's resolver -------------------------------------


def test_a_lost_reference_another_blueprint_claims_is_not_unset():
    engine = _Engine(on_column=[], by_policy={str(OLD): [_row(OLD)]})
    changed = _resolver(engine, [NEW_BP, OLD_BP_AFTER])._apply_policy_refs(OLD_BP_AFTER)
    assert engine.ddl == []
    assert changed is False


def test_a_lost_reference_nobody_claims_is_unset_exactly_as_oie13():
    engine = _Engine(on_column=[], by_policy={str(OLD): [_row(OLD)]})
    changed = _resolver(engine, [OLD_BP_AFTER])._apply_policy_refs(OLD_BP_AFTER)
    assert engine.ddl == [UNSET]
    assert changed is True


def test_a_claim_on_a_different_column_does_not_stop_the_unset():
    other = _bp(NEW, [(SIGNAL, ["SUBJECT"])])
    engine = _Engine(on_column=[], by_policy={str(OLD): [_row(OLD)]})
    _resolver(engine, [other, OLD_BP_AFTER])._apply_policy_refs(OLD_BP_AFTER)
    assert engine.ddl == [UNSET]


# --- the swap, both orders: never a moment with no policy ------------------


def _swap(order):
    """Run both resolvers in `order` against one shared column state."""
    state = {"policy": OLD}
    statements = []

    class _Shared(_Engine):
        def execute_meta(self, sql, params=None):
            text = self.formatter.format_sql(sql, params)
            if "ref_entity_name" in text:
                return [_row(state["policy"])] if state["policy"] else []
            name = str(params["policy_name"])
            return [_row(state["policy"])] if state["policy"] and str(state["policy"]) == name else []

        def execute_unsafe_ddl(self, sql, params=None, condition=True):
            text = " ".join(self.formatter.format_sql(sql, params).split())
            statements.append(text)
            if "UNSET MASKING POLICY" in text:
                state["policy"] = None
            elif "SET MASKING POLICY" in text:
                if state["policy"] and not text.endswith(" FORCE"):
                    raise RuntimeError("Snowflake refuses a SET while another policy is attached")
                state["policy"] = NEW
            if state["policy"] is None:
                statements.append("<<unmasked>>")

    bps = [NEW_BP, OLD_BP_AFTER]
    for bp in order:
        _resolver(_Shared(on_column=[], by_policy={}), bps)._apply_policy_refs(bp)
    return state["policy"], statements


def test_the_swap_new_first_is_one_forced_statement():
    final, statements = _swap([NEW_BP, OLD_BP_AFTER])
    assert final == NEW
    assert statements == [OIE13_SET + " FORCE"]


def test_the_swap_old_first_is_one_forced_statement():
    final, statements = _swap([OLD_BP_AFTER, NEW_BP])
    assert final == NEW
    assert statements == [OIE13_SET + " FORCE"]
