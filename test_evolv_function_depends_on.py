"""evolv fork patch: a FUNCTION declares depends_on on the views its body reads.

A SQL UDF resolves the objects its body names at CREATE time. FunctionResolver runs
before ViewResolver -- views can select a UDF, so it must -- and a function over a view
therefore failed on a fresh one-pass apply. Measured by the catalyst-mdm team on
MDM_DEV, 2026-10-05:

    F_DECISION_RISK_BAND ... ERROR ... Object 'V_DECISION_RISK' does not exist

A second apply worked, because the view existed by then. depends_on is batched per
resolver, so no declaration inside FunctionResolver could wait for a view. The patch:

  * FUNCTION accepts `depends_on` (parser schema, DependsOnMixin, a validator that
    allows views only);
  * FunctionResolver leaves a function that declares depends_on out of its batches,
    but keeps it in its blueprints so it is never a drop candidate;
  * ViewDependentFunctionResolver, placed right after ViewResolver in both resolve
    sequences, creates or compares those functions and drops nothing. With no function
    declaring depends_on it is skipped before it reads Snowflake.

Pure-unit: no Snowflake connection, no credentials.
"""

from concurrent.futures import Future
from pathlib import Path

import jsonschema
import pytest

from snowddl.app.singledb import SingleDbApp
from snowddl.blueprint import (
    BaseDataType,
    DatabaseIdent,
    DataType,
    FunctionBlueprint,
    SchemaObjectIdent,
    SchemaObjectIdentWithArgs,
    TableBlueprint,
    ViewBlueprint,
)
from snowddl.blueprint.blueprint import DependsOnMixin
from snowddl.config import SnowDDLConfig
from snowddl.parser._scanner import DirectoryScanner
from snowddl.parser.function import FunctionParser, function_json_schema
from snowddl.resolver import (
    FunctionResolver,
    MaterializedViewResolver,
    SemanticViewResolver,
    ViewDependentFunctionResolver,
    ViewResolver,
    default_destroy_sequence,
    default_resolve_sequence,
    singledb_destroy_sequence,
    singledb_resolve_sequence,
)
from snowddl.resolver.abc_resolver import AbstractResolver, ResolveResult
from snowddl.validator import FunctionValidator, default_validate_sequence

RESOLVE_SEQUENCES = (default_resolve_sequence, singledb_resolve_sequence)


def _function(name, depends_on=(), database="DB"):
    return FunctionBlueprint(
        full_name=SchemaObjectIdentWithArgs("", database, "SCH", name, data_types=[BaseDataType.VARCHAR]),
        language="SQL",
        body="SELECT 1",
        arguments=[],
        returns=DataType("NUMBER(38,0)"),
        depends_on={SchemaObjectIdent("", database, "SCH", d) for d in depends_on},
    )


def _view(name, database="DB"):
    return ViewBlueprint(full_name=SchemaObjectIdent("", database, "SCH", name), text="SELECT 1 AS c")


class _Config:
    def __init__(self, *blueprints):
        self._by_type = {}
        for bp in blueprints:
            self._by_type.setdefault(type(bp), {})[str(bp.full_name)] = bp

    def get_blueprints_by_type(self, cls):
        return self._by_type.get(cls, {})


# --- Parser -------------------------------------------------------------------


def test_function_schema_accepts_depends_on():
    jsonschema.validate({"returns": "NUMBER", "body": "SELECT 1", "depends_on": ["V_A"]}, function_json_schema)


def test_function_schema_still_refuses_an_unknown_key():
    # additionalProperties stays False: the patch adds one key, it does not open the schema.
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"returns": "NUMBER", "body": "SELECT 1", "depend_on": ["V_A"]}, function_json_schema)


def test_function_schema_refuses_an_empty_depends_on():
    # Same shape as VIEW's key: minItems 1.
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate({"returns": "NUMBER", "body": "SELECT 1", "depends_on": []}, function_json_schema)


def _parse(tmp_path: Path, yaml_text: str):
    fn_dir = tmp_path / "DB" / "SCH" / "function"
    fn_dir.mkdir(parents=True)
    (fn_dir / "f_band(varchar).yaml").write_text(yaml_text, encoding="utf-8")

    config = SnowDDLConfig(env_prefix="")
    parser = FunctionParser(config, DirectoryScanner(tmp_path))
    parser.load_blueprints()

    assert parser.errors == {}
    return config.get_blueprints_by_type(FunctionBlueprint)["DB.SCH.F_BAND(VARCHAR)"]


def test_parser_builds_depends_on_idents_in_both_name_forms(tmp_path):
    # A bare name resolves in the function's own schema; SCHEMA.NAME in another one.
    bp = _parse(
        tmp_path,
        "arguments:\n  p: VARCHAR(16777216)\nreturns: NUMBER(38,0)\nbody: SELECT 1\n"
        "depends_on:\n  - V_LOCAL\n  - OTHER.V_REMOTE\n",
    )

    assert sorted(str(d) for d in bp.depends_on) == ["DB.OTHER.V_REMOTE", "DB.SCH.V_LOCAL"]


def test_parser_without_depends_on_gives_an_empty_set(tmp_path):
    bp = _parse(tmp_path, "arguments:\n  p: VARCHAR(16777216)\nreturns: NUMBER(38,0)\nbody: SELECT 1\n")

    assert bp.depends_on == set()


def test_function_blueprint_carries_the_mixin():
    # The batch splitter and the singledb remap both key on DependsOnMixin.
    assert issubclass(FunctionBlueprint, DependsOnMixin)


# --- Validator ----------------------------------------------------------------


def _validate(*blueprints):
    validator = FunctionValidator.__new__(FunctionValidator)
    validator.config = _Config(*blueprints)
    validator.errors = {}
    validator.validate()
    return validator.errors


def test_validator_accepts_a_declared_view():
    assert _validate(_view("V_RISK"), _function("F_BAND", ["V_RISK"])) == {}


def test_validator_refuses_a_view_missing_from_config():
    errors = _validate(_function("F_BAND", ["V_MISSING"]))

    assert list(errors) == ["DB.SCH.F_BAND(VARCHAR)"]
    assert "V_MISSING" in str(errors["DB.SCH.F_BAND(VARCHAR)"])


def test_validator_refuses_a_table():
    # The deferred pass runs after ViewResolver and nothing else, so only a view edge is
    # actually honoured. A table edge would parse, validate and then mean nothing.
    table = TableBlueprint(full_name=SchemaObjectIdent("", "DB", "SCH", "T_RISK"), columns=[])

    assert list(_validate(table, _function("F_BAND", ["T_RISK"]))) == ["DB.SCH.F_BAND(VARCHAR)"]


def test_validator_runs_in_the_default_sequence():
    assert FunctionValidator in default_validate_sequence


# --- Resolve sequence ---------------------------------------------------------


def test_deferred_pass_runs_after_every_view_type_in_both_sequences():
    for sequence in RESOLVE_SEQUENCES:
        deferred = sequence.index(ViewDependentFunctionResolver)

        assert deferred > sequence.index(ViewResolver)
        assert deferred > sequence.index(MaterializedViewResolver)
        assert deferred < sequence.index(SemanticViewResolver)


def test_function_resolver_did_not_move():
    # evolv.2's placement -- after tables, before every consumer of a function -- stands.
    for sequence in RESOLVE_SEQUENCES:
        assert sequence.index(FunctionResolver) < sequence.index(MaterializedViewResolver)
        assert sequence.count(FunctionResolver) == 1
        assert sequence.count(ViewDependentFunctionResolver) == 1


def test_teardown_is_still_untouched():
    for sequence in (default_destroy_sequence, singledb_destroy_sequence):
        assert ViewDependentFunctionResolver not in sequence


def test_both_passes_answer_to_the_function_object_type():
    # --include-object-types / --exclude-object-types FUNCTION must reach both passes.
    deferred = ViewDependentFunctionResolver.__new__(ViewDependentFunctionResolver)

    assert deferred.get_object_type() == FunctionResolver.__new__(FunctionResolver).get_object_type()


# --- Batching -----------------------------------------------------------------


def _resolver(cls, *blueprints):
    resolver = cls.__new__(cls)
    resolver.config = _Config(*blueprints)
    resolver.blueprints = resolver.get_blueprints()
    return resolver


def test_function_resolver_holds_every_function_but_batches_only_the_undeclared():
    resolver = _resolver(FunctionResolver, _function("F_PLAIN"), _function("F_BAND", ["V_RISK"]))

    assert sorted(resolver.blueprints) == ["DB.SCH.F_BAND(VARCHAR)", "DB.SCH.F_PLAIN(VARCHAR)"]
    assert resolver._split_blueprints_into_batches() == [["DB.SCH.F_PLAIN(VARCHAR)"]]


def test_without_depends_on_batching_is_what_upstream_produced():
    # Before the patch FunctionBlueprint had no mixin, so the base splitter put every
    # function in one batch. The override must give the same answer.
    functions = [_function(f"F_{i}") for i in range(5)]
    resolver = _resolver(FunctionResolver, *functions)

    assert resolver._split_blueprints_into_batches() == AbstractResolver._split_blueprints_into_batches(resolver)
    assert len(resolver._split_blueprints_into_batches()) == 1


def test_deferred_pass_holds_only_the_declared():
    resolver = _resolver(ViewDependentFunctionResolver, _function("F_PLAIN"), _function("F_BAND", ["V_RISK"]))

    assert list(resolver.blueprints) == ["DB.SCH.F_BAND(VARCHAR)"]
    assert resolver._split_blueprints_into_batches() == [["DB.SCH.F_BAND(VARCHAR)"]]


# --- One resolve() each, end to end over a recording engine --------------------


class _Settings:
    exclude_object_types = []
    include_object_types = []


class _Context:
    from snowddl.blueprint import Edition

    edition = Edition.ENTERPRISE


class _SyncExecutor:
    """Runs each task at submit, in submit order, so the log order is deterministic."""

    def submit(self, fn, *args):
        future = Future()
        try:
            future.set_result(fn(*args))
        except Exception as e:
            future.set_exception(e)
        return future


class _IntentionCache:
    def check_parent_object_drop_intention(self, *_):
        return False

    def add_object_drop_intention(self, *_):
        pass


class _Logger:
    def debug(self, *_):
        pass

    info = warning = debug


class _Engine:
    def __init__(self, log):
        self.settings = _Settings()
        self.context = _Context()
        self.executor = _SyncExecutor()
        self.intention_cache = _IntentionCache()
        self.logger = _Logger()
        self.log = log

    def flush_thread_buffers(self):
        pass


def _recording(cls, existing):
    """The real resolve() -- _is_skipped, _resolve_drop, the batch loop -- with only the
    Snowflake reads and the DDL entry points replaced by a log."""
    # `existing` is the live functions. Only the two function passes read it.
    existing = existing if issubclass(cls, FunctionResolver) else ()

    class Recording(cls):
        def get_existing_objects(self):
            self.engine.log.append((cls.__name__, "SHOW"))
            return {name: {"name": name} for name in existing}

        def _create_object_entry_point(self, bp):
            self.engine.log.append((cls.__name__, "CREATE", str(bp.full_name)))
            return ResolveResult.CREATE

        def _compare_object_entry_point(self, bp, row):
            self.engine.log.append((cls.__name__, "COMPARE", str(bp.full_name)))
            return ResolveResult.NOCHANGE

        def drop_object(self, row):
            self.engine.log.append((cls.__name__, "DROP", row["name"]))
            return ResolveResult.DROP

    return Recording


def _run(blueprints, existing=()):
    from snowddl.blueprint import SchemaBlueprint, SchemaIdent

    log = []
    config = _Config(SchemaBlueprint(full_name=SchemaIdent("", "DB", "SCH")), *blueprints)

    # The order both resolve sequences run these three in.
    for cls in (FunctionResolver, ViewResolver, ViewDependentFunctionResolver):
        engine = _Engine(log)
        engine.config = config
        _recording(cls, existing)(engine).resolve()

    return log


def test_a_function_over_a_view_is_created_after_the_view():
    log = _run([_view("V_RISK"), _function("F_PLAIN"), _function("F_BAND", ["V_RISK"])])
    creates = [entry[2] for entry in log if entry[1] == "CREATE"]

    assert creates == ["DB.SCH.F_PLAIN(VARCHAR)", "DB.SCH.V_RISK", "DB.SCH.F_BAND(VARCHAR)"]


def test_an_existing_deferred_function_is_compared_once_and_never_dropped():
    existing = ["DB.SCH.F_BAND(VARCHAR)"]
    log = _run([_view("V_RISK"), _function("F_BAND", ["V_RISK"])], existing=existing)

    assert [e for e in log if e[1] == "DROP"] == []
    assert [e for e in log if e[1] == "COMPARE"] == [("ViewDependentFunctionResolver", "COMPARE", "DB.SCH.F_BAND(VARCHAR)")]


def test_an_undeclared_function_is_still_dropped_once():
    log = _run([_function("F_PLAIN")], existing=["DB.SCH.F_STRAY(VARCHAR)"])

    assert [e for e in log if e[1] == "DROP"] == [("FunctionResolver", "DROP", "DB.SCH.F_STRAY(VARCHAR)")]


def test_no_depends_on_means_the_deferred_pass_never_reads_snowflake():
    # The "behaves exactly as before" guarantee: skip_on_empty_blueprints stops the
    # second pass before its SHOW USER FUNCTIONS, so no extra query is issued.
    log = _run([_view("V_RISK"), _function("F_PLAIN")])

    assert [e for e in log if e[0] == "ViewDependentFunctionResolver"] == []


# --- singledb -----------------------------------------------------------------


def test_singledb_moves_function_depends_on_to_the_target_database():
    app = SingleDbApp.__new__(SingleDbApp)
    app.config_db = DatabaseIdent("", "MDM")
    app.target_db = DatabaseIdent("", "MDM_SCRATCH")

    converted = app.convert_blueprint(_function("F_BAND", ["V_RISK"], database="MDM"))

    assert str(converted.full_name) == "MDM_SCRATCH.SCH.F_BAND(VARCHAR)"
    assert [str(d) for d in converted.depends_on] == ["MDM_SCRATCH.SCH.V_RISK"]
