"""evolv fork patch (0.67.5-evolv.2) regression tests.

Two defects found by the first real Catalyst MDM apply through this fork
(88 of 93 objects, exit 8, five ERROR resolutions). Pure-unit: no Snowflake
connection, no credentials.

  * Defect A -- singledb never remapped `depends_on`. `convert_object_recursive`
    walked BaseModel, list and dict but had no `set` branch, so with
    --target-db != --config-db a view's `full_name` moved to the target database
    while its `depends_on` idents stayed in the config database. The equality test
    in `_split_blueprints_into_batches` then never matched, batch 2 came out empty,
    and the fallback dumped every dependent view into one batch -- at
    --max-workers 4 a view was created before the view it selects from.

  * Defect B -- FunctionResolver ran before TableResolver in both resolve
    sequences. A SQL UDF resolves its table references at CREATE time, so
    F_SURVIVORSHIP_DECAYED(DATE) failed with 2003 42S02 against a table that did
    not exist yet, and the view over that function failed in turn.
"""

from snowddl.app.singledb import SingleDbApp
from snowddl.blueprint import DatabaseIdent, SchemaObjectIdent, ViewBlueprint
from snowddl.blueprint.blueprint import DependsOnMixin, FunctionBlueprint
from snowddl.resolver import (
    DynamicTableResolver,
    FunctionResolver,
    MaterializedViewResolver,
    SequenceResolver,
    TableResolver,
    ViewResolver,
    default_destroy_sequence,
    default_resolve_sequence,
    singledb_destroy_sequence,
    singledb_resolve_sequence,
)
from snowddl.resolver.abc_resolver import AbstractResolver


# --- Defect A: depends_on remapping in singledb mode -------------------------


def _app(config_db="MDM", target_db="MDM_SCRATCH"):
    # Bypass __init__ -- it parses argv. Only the two idents matter here.
    app = SingleDbApp.__new__(SingleDbApp)
    app.config_db = DatabaseIdent("", config_db)
    app.target_db = DatabaseIdent("", target_db)
    return app


def _view(name, depends_on=(), database="MDM"):
    return ViewBlueprint(
        full_name=SchemaObjectIdent("", database, "MDM", name),
        text="SELECT 1 AS c",
        depends_on={SchemaObjectIdent("", database, "MDM", d) for d in depends_on},
    )


def test_depends_on_moves_to_the_target_database():
    # The exact pair from the failed apply.
    converted = _app().convert_blueprint(_view("V_SOURCE_RECORD_RESOLVED", ["V_MASTER_RESOLVED"]))

    assert str(converted.full_name) == "MDM_SCRATCH.MDM.V_SOURCE_RECORD_RESOLVED"
    assert [str(d) for d in converted.depends_on] == ["MDM_SCRATCH.MDM.V_MASTER_RESOLVED"]


def test_the_rebuilt_set_is_not_corrupted():
    # Idents hash on str(self), and str() includes the database. Mutating an element
    # in place inside a live set leaves it filed under a stale bucket: `in` misses it
    # and the set is quietly broken. The fix must rebuild the set, not mutate it.
    converted = _app().convert_blueprint(_view("V_A", ["V_B", "V_C"]))

    assert len(converted.depends_on) == 2
    for name in ("V_B", "V_C"):
        probe = SchemaObjectIdent("", "MDM_SCRATCH", "MDM", name)
        assert probe in converted.depends_on, f"{probe} unreachable -- set corrupted"


def test_the_source_blueprint_is_left_alone():
    # convert_blueprint deep-copies; the original config must not move databases.
    original = _view("V_A", ["V_B"])
    _app().convert_blueprint(original)

    assert str(original.full_name) == "MDM.MDM.V_A"
    assert [str(d) for d in original.depends_on] == ["MDM.MDM.V_B"]


def test_identical_config_and_target_db_is_still_a_noop():
    # OIE's conformance workflow runs --config-db OIE --target-db OIE. That path was
    # never broken and must stay byte-identical.
    converted = _app(config_db="OIE", target_db="OIE").convert_blueprint(_view("V_A", ["V_B"], database="OIE"))

    assert str(converted.full_name) == "OIE.MDM.V_A"
    assert [str(d) for d in converted.depends_on] == ["OIE.MDM.V_B"]


def test_empty_depends_on_survives_conversion():
    converted = _app().convert_blueprint(_view("V_LEAF"))

    assert converted.depends_on == set()
    assert str(converted.full_name) == "MDM_SCRATCH.MDM.V_LEAF"


class _StubResolver(AbstractResolver):
    """Minimal concrete resolver -- only the batch splitter is exercised."""

    def __init__(self, blueprints):
        self.blueprints = {str(bp.full_name): bp for bp in blueprints}

    def get_object_type(self):
        raise NotImplementedError

    def get_blueprints(self):
        return self.blueprints

    def get_existing_objects(self):
        raise NotImplementedError

    def create_object(self, bp):
        raise NotImplementedError

    def compare_object(self, bp, row):
        raise NotImplementedError

    def drop_object(self, row):
        raise NotImplementedError


def test_converted_blueprints_batch_by_dependency_not_by_luck():
    # The behaviour the remap exists for, on the shape that actually failed: a
    # three-level chain. Unremapped, batch 1 took the one dependency-free view, batch 2
    # came out empty (no depends_on ident ever matched an allocated full_name) and the
    # fallback swept BOTH remaining levels into a single batch -- so at --max-workers 4
    # V_SOURCE_RECORD_RESOLVED could be created before V_MASTER_RESOLVED, which is
    # exactly what happened. Remapped, each level gets its own batch.
    app = _app()
    chain = [
        app.convert_blueprint(_view("V_MASTER_BASE")),
        app.convert_blueprint(_view("V_MASTER_RESOLVED", ["V_MASTER_BASE"])),
        app.convert_blueprint(_view("V_SOURCE_RECORD_RESOLVED", ["V_MASTER_RESOLVED"])),
    ]

    batches = _StubResolver(chain)._split_blueprints_into_batches()

    assert batches == [
        ["MDM_SCRATCH.MDM.V_MASTER_BASE"],
        ["MDM_SCRATCH.MDM.V_MASTER_RESOLVED"],
        ["MDM_SCRATCH.MDM.V_SOURCE_RECORD_RESOLVED"],
    ]


# --- Defect B: functions resolve after tables --------------------------------

RESOLVE_SEQUENCES = (default_resolve_sequence, singledb_resolve_sequence)


def test_functions_resolve_after_tables_in_both_sequences():
    # A SQL UDF resolves its table references at CREATE time (a Scripting procedure
    # body does not, which is why procedures never hit this).
    for sequence in RESOLVE_SEQUENCES:
        assert sequence.index(FunctionResolver) > sequence.index(TableResolver)


def test_functions_still_resolve_before_every_consumer_of_a_function():
    # Views, materialized views and dynamic tables can all select a UDF, so the move
    # must stop short of them.
    for sequence in RESOLVE_SEQUENCES:
        for consumer in (DynamicTableResolver, MaterializedViewResolver, ViewResolver):
            assert sequence.index(FunctionResolver) < sequence.index(consumer)


def test_no_resolver_was_added_dropped_or_duplicated():
    # Guard against a reshuffle: the surrounding order is unchanged.
    for sequence in RESOLVE_SEQUENCES:
        assert len(sequence) == len(set(sequence))
        assert sequence.index(SequenceResolver) < sequence.index(TableResolver)
        assert sequence.index(TableResolver) < sequence.index(ViewResolver)


def test_a_depends_on_declaration_could_not_have_fixed_this():
    # Records why the sequence moved instead of FunctionBlueprint gaining the mixin:
    # depends_on is batched per resolver (`allocated_full_names` is local to one
    # resolver run), so a function -> table edge is invisible to FunctionResolver no
    # matter how it is declared. Only the cross-resolver sequence can express it.
    assert not issubclass(FunctionBlueprint, DependsOnMixin)


def test_teardown_is_untouched():
    # resolver/__init__.py's resolve order is the drop order in reverse only by
    # convention -- teardown runs its own explicit lists, and neither names
    # FunctionResolver, so moving it cannot reorder a drop.
    assert FunctionResolver not in default_destroy_sequence
    assert FunctionResolver not in singledb_destroy_sequence


# --- ADR-005: is_unmanaged split out of is_sandbox ---------------------------
#
# One key carried two behaviours. D-218 extended is_sandbox from "never drop what
# I did not declare" to also mean "recognize but do not manage", and OIE's five
# schemas need both. Catalyst needs the first alone and could not have it: a
# GOLDEN_* table declared under a sandboxed MDM_SERVING parsed, validated and
# blueprinted, then was skipped on every apply, silently. PROBE-ADR002 measured
# exactly that.
#
# After this patch the drop-skip stays on is_sandbox and the two skip-predicates
# read is_unmanaged. A schema needing both carries both. These tests pin the
# split: without them the next upstream move re-keys a predicate by accident and
# nothing fails until a deploy does.

import jsonschema
import pytest

from snowddl.blueprint import SchemaBlueprint, SchemaIdent
from snowddl.parser.database import database_json_schema
from snowddl.parser.schema import schema_json_schema
from snowddl.resolver.abc_schema_object_resolver import AbstractSchemaObjectResolver
from snowddl.resolver.schema import SchemaResolver


def _schema_bp(name, **flags):
    return SchemaBlueprint(full_name=SchemaIdent("", "DB", name), **flags)


class _ConfigStub:
    def __init__(self, blueprints):
        self._blueprints = blueprints

    def get_blueprints_by_type(self, _cls):
        return self._blueprints


class _ConcreteObjectResolver(AbstractSchemaObjectResolver):
    """Smallest concrete subclass. The abstract methods are never called -- only
    the inherited predicate is under test -- but the class cannot be built
    without them."""

    def get_object_type(self):  # pragma: no cover - never called
        raise NotImplementedError

    def get_blueprints(self):  # pragma: no cover - never called
        raise NotImplementedError

    def get_existing_objects_in_schema(self, schema):  # pragma: no cover
        raise NotImplementedError

    def create_object(self, bp):  # pragma: no cover - never called
        raise NotImplementedError

    def compare_object(self, bp, row):  # pragma: no cover - never called
        raise NotImplementedError

    def drop_object(self, row):  # pragma: no cover - never called
        raise NotImplementedError


def _object_predicate(schema_bp):
    """`AbstractSchemaObjectResolver._is_unmanaged_blueprint` over one schema."""
    resolver = _ConcreteObjectResolver.__new__(_ConcreteObjectResolver)
    resolver.config = _ConfigStub({"DB.SCH": schema_bp})
    return resolver._is_unmanaged_blueprint("DB.SCH.SOME_OBJECT")


def _schema_predicate(schema_bp):
    """`SchemaResolver._is_unmanaged_blueprint` over one schema."""
    resolver = SchemaResolver.__new__(SchemaResolver)
    resolver.blueprints = {"DB.SCH": schema_bp}
    return resolver._is_unmanaged_blueprint("DB.SCH")


def test_is_unmanaged_skips_create_and_compare():
    # D-218's behaviour, re-keyed. Both predicates fire on the new flag.
    bp = _schema_bp("SCH", is_unmanaged=True)
    assert _object_predicate(bp) is True
    assert _schema_predicate(bp) is True


def test_is_sandbox_alone_still_creates_its_declared_children():
    # The criterion this whole ADR exists for. A schema that only wants the
    # drop-skip deploys its own declared objects -- before the split this
    # returned True and the objects were skipped without a word.
    bp = _schema_bp("SCH", is_sandbox=True)
    assert _object_predicate(bp) is False
    assert _schema_predicate(bp) is False


def test_the_drop_skip_stays_on_is_sandbox():
    # _resolve_drop reads schema_bp.is_sandbox directly, not the predicate. Pin
    # the attribute so a future re-key of the predicates cannot silently take the
    # drop-skip with them -- that is what puts 616 undeclared OIE objects into a
    # plain apply's drop path.
    import inspect

    source = inspect.getsource(AbstractSchemaObjectResolver._resolve_drop)
    assert "schema_bp.is_sandbox" in source
    assert "is_unmanaged" not in source


def test_both_flags_together_is_the_oie_shape():
    bp = _schema_bp("SCH", is_sandbox=True, is_unmanaged=True)
    assert _object_predicate(bp) is True
    assert bp.is_sandbox is True


def test_parsers_accept_the_key_at_both_levels():
    # additionalProperties is False on both, so an unrecognised key halts the run
    # at exit(1) before any DDL. That is why the fork patch lands before OIE's.
    jsonschema.validate({"is_sandbox": True, "is_unmanaged": True}, schema_json_schema)
    jsonschema.validate({"is_sandbox": True, "is_unmanaged": True}, database_json_schema)


def test_an_unknown_flag_is_still_refused():
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"is_unmanged": True}, schema_json_schema)


def test_schema_inherits_is_unmanaged_from_its_database():
    # Mirrors the inheritance is_sandbox already had. Without it a database-level
    # flag would be accepted by the parser and then do nothing.
    import inspect

    from snowddl.parser import schema as schema_parser

    source = inspect.getsource(schema_parser)
    assert 'schema_params.get("is_unmanaged", database_params.get("is_unmanaged", False))' in source
