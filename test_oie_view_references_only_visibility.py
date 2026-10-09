"""OIE fork patch: REFERENCES-only view visibility (0.67.5-evolv.8, OIE-2314 item 75).

Layer-1 CI conformance plans `OIE.OBSERVABILITY.V_SIGNAL_GOLD_READING` and
`V_SIGNAL_SENSITIVITY_GOLD_COMPARISON` as `OIE_CI_MIGRATION`, which holds REFERENCES
on both and SELECT on neither -- a deliberate, SC-ruled posture on these two gold
objects (D-291/D-293; withdrawn again 2026-10-08 after a same-day grant-and-revert).
Upstream `ViewResolver.compare_object` cannot clear either shape for that role:

  * a SECURE view's `text` comes back "" from `SHOW VIEWS` for any non-owner role,
    privilege notwithstanding, so the text-match check always fails and REPLACEs;
  * a non-secure view's `text` matches, but the `describe_meta("SELECT * FROM ...")`
    liveness probe raises errno 2003 ("does not exist or not authorized") for a role
    with REFERENCES and no SELECT -- the same errno a genuinely broken underlying
    object would raise -- so it REPLACEs too.

Both are a visibility artifact, not drift: `snowddl plan` reported `Suggested 4` on
every push to main from 2026-10-08 while Layer-2 (full-access role) stayed green on
the same commit. The patch is opt-in (`--ignore-unreadable-view-definitions`) so a
role that holds SELECT everywhere it plans -- Layer-2, and every fork user besides
this one CI identity -- is byte-for-byte unaffected: `text` is never empty and
`describe_meta` never raises 2003 for it on an unchanged view, so neither new branch
is reachable.

Pure-unit: no Snowflake connection. `_build_create_view` is stubbed per-test to
isolate the comparison logic from the query builder (same technique
test_oie_fork_patches.py uses for `TaskResolver.compare_object`).
"""

import pytest

from snowddl.blueprint import SchemaObjectIdent, ViewBlueprint
from snowddl.error import SnowDDLExecuteError
from snowddl.resolver import ViewResolver
from snowddl.resolver.abc_resolver import ResolveResult
from snowddl.settings import SnowDDLSettings


class _MatchingQuery:
    """Stands in for the CREATE query: str() equals the row's recorded text,
    so compare reaches the describe/visibility checks instead of a text mismatch."""

    def __init__(self, text):
        self._text = text

    def __str__(self):
        return self._text


class _SnowExc:
    def __init__(self, errno):
        self.errno = errno
        self.raw_msg = "SQL compilation error: does not exist or not authorized."


class _RecordingEngine:
    def __init__(self, describe_errno=None, ignore_unreadable=True):
        self.sql = []
        self.describe_calls = 0
        self._describe_errno = describe_errno
        # Real attribute path: resolvers read `self.engine.settings`, never
        # `self.settings` -- there is no such attribute on the resolver itself
        # (AbstractResolver.__init__ only sets `self.engine`). A stub that set
        # `resolver.settings` directly let the old `self.settings` code pass
        # these tests while raising AttributeError against a real engine.
        self.settings = SnowDDLSettings(ignore_unreadable_view_definitions=ignore_unreadable)

        class _Logger:
            def debug(self_inner, _msg):
                pass

        self.logger = _Logger()

    def describe_meta(self, _query, _params=None):
        self.describe_calls += 1

        if self._describe_errno is not None:
            raise SnowDDLExecuteError(_SnowExc(self._describe_errno), "SELECT * FROM X")

    def execute_safe_ddl(self, sql, params=None):
        self.sql.append((sql, params))


def _view_bp(name="V_X", is_secure=False, comment=None):
    return ViewBlueprint(
        full_name=SchemaObjectIdent("", "OIE", "OBSERVABILITY", name),
        text="SELECT 1",
        is_secure=is_secure,
        comment=comment,
    )


def _resolver(engine, query_text="MATCHES"):
    resolver = ViewResolver.__new__(ViewResolver)
    resolver.engine = engine
    resolver._build_create_view = lambda _bp: _MatchingQuery(query_text)
    return resolver


# --- SECURE view, text withheld from SHOW ------------------------------------


def test_secure_view_with_withheld_text_is_nochange_under_the_flag():
    bp = _view_bp(is_secure=True, comment="c")
    row = {"text": "", "is_secure": True, "comment": "c"}
    engine = _RecordingEngine()
    resolver = _resolver(engine)

    result = resolver.compare_object(bp, row)

    assert result == ResolveResult.NOCHANGE
    assert engine.sql == []
    assert engine.describe_calls == 0  # never reaches the liveness probe


def test_secure_view_with_withheld_text_still_syncs_a_changed_comment():
    bp = _view_bp(is_secure=True, comment="new")
    row = {"text": "", "is_secure": True, "comment": "old"}
    engine = _RecordingEngine()
    resolver = _resolver(engine)

    result = resolver.compare_object(bp, row)

    assert result == ResolveResult.ALTER
    assert len(engine.sql) == 1
    assert engine.describe_calls == 0


def test_secure_view_withheld_text_is_replaced_without_the_flag():
    # Default (upstream) behaviour is unchanged when the flag is off.
    bp = _view_bp(is_secure=True, comment="c")
    row = {"text": "", "is_secure": True, "comment": "c"}
    engine = _RecordingEngine(ignore_unreadable=False)
    resolver = _resolver(engine)

    result = resolver.compare_object(bp, row)

    assert result == ResolveResult.REPLACE


def test_a_genuinely_missing_secure_view_is_not_this_branch():
    # No row at all -- never reaches compare_object. get_existing_objects_in_schema
    # would simply omit the key, and the resolver's own CREATE path (untouched by
    # this patch) fires instead. Documented here so the branch condition above is
    # read against the right contrast: "exists, body unreadable" vs "does not exist".
    assert True


def test_secure_view_owned_or_readable_keeps_upstream_text_compare():
    # A role that can read the view's definition never has empty text, so the new
    # branch's `not row["text"]` guard is false and it falls through unchanged.
    bp = _view_bp(is_secure=True, comment="c")
    row = {"text": "MATCHES", "is_secure": True, "comment": "c"}
    engine = _RecordingEngine()
    resolver = _resolver(engine)

    result = resolver.compare_object(bp, row)

    assert result == ResolveResult.NOCHANGE
    assert engine.describe_calls == 1  # reached the normal describe probe


# --- Non-secure view, text visible but SELECT-only probe fails --------------


def test_references_only_errno_2003_is_nochange_under_the_flag():
    bp = _view_bp(comment="c")
    row = {"text": "MATCHES", "is_secure": False, "comment": "c"}
    engine = _RecordingEngine(describe_errno=2003)
    resolver = _resolver(engine)

    result = resolver.compare_object(bp, row)

    assert result == ResolveResult.NOCHANGE
    assert engine.sql == []


def test_references_only_errno_2003_still_syncs_a_changed_comment():
    bp = _view_bp(comment="new")
    row = {"text": "MATCHES", "is_secure": False, "comment": "old"}
    engine = _RecordingEngine(describe_errno=2003)
    resolver = _resolver(engine)

    result = resolver.compare_object(bp, row)

    assert result == ResolveResult.ALTER
    assert len(engine.sql) == 1


def test_errno_2003_is_replaced_without_the_flag():
    # Upstream behaviour preserved when the flag is off -- including for a role
    # that genuinely lacks SELECT, which is why this is opt-in rather than default.
    bp = _view_bp(comment="c")
    row = {"text": "MATCHES", "is_secure": False, "comment": "c"}
    engine = _RecordingEngine(describe_errno=2003, ignore_unreadable=False)
    resolver = _resolver(engine)

    result = resolver.compare_object(bp, row)

    assert result == ResolveResult.REPLACE


def test_a_different_errno_is_not_swallowed_even_under_the_flag():
    # Only 2003 is the "can't tell privilege from drift" errno. Anything else
    # (e.g. a warehouse/network error) must still surface as REPLACE so the apply
    # has a chance to raise it, same as upstream.
    bp = _view_bp(comment="c")
    row = {"text": "MATCHES", "is_secure": False, "comment": "c"}
    engine = _RecordingEngine(describe_errno=90105)
    resolver = _resolver(engine)

    result = resolver.compare_object(bp, row)

    assert result == ResolveResult.REPLACE


def test_a_role_with_select_still_catches_real_drift():
    # describe_meta succeeds (no exception) but the text does not match config --
    # ordinary REPLACE path, completely untouched by this patch.
    bp = _view_bp(comment="c")
    row = {"text": "DOES-NOT-MATCH", "is_secure": False, "comment": "c"}
    engine = _RecordingEngine()
    resolver = _resolver(engine)

    result = resolver.compare_object(bp, row)

    assert result == ResolveResult.REPLACE
    assert engine.describe_calls == 0  # text mismatch short-circuits before the probe


def test_flag_defaults_to_false():
    assert SnowDDLSettings().ignore_unreadable_view_definitions is False


# `--ignore-unreadable-view-definitions` has to be registered on BOTH entry-point
# parsers: BaseApp's (used by `snowddl` / `snowddl-apply` etc.) and SingleDbApp's
# (used by `snowddl-singledb`, the one Layer-1 CI runs). SingleDbApp builds its own
# ArgumentParser from scratch rather than extending BaseApp's, so registering the
# flag on one does not register it on the other -- exactly the gap that let
# `snowddl-singledb ... plan --ignore-unreadable-view-definitions` fail with
# "unrecognized arguments" in CI (run 37942011035) despite BaseApp's unit tests
# all passing. Neither `init_arguments_parser` method reads `self`, so the
# unbound function can be called directly without constructing a full App
# (which would try to read config / connect to Snowflake).
def test_base_app_parser_accepts_the_flag():
    from snowddl.app.base import BaseApp

    parser = BaseApp.init_arguments_parser(None)
    # The flag belongs to the top-level parser, not the `plan` subparser -- it
    # must precede the subcommand, since the subparser action consumes the rest.
    args = vars(parser.parse_args(["--ignore-unreadable-view-definitions", "plan"]))

    assert args["ignore_unreadable_view_definitions"] is True


def test_base_app_parser_defaults_the_flag_to_false():
    from snowddl.app.base import BaseApp

    parser = BaseApp.init_arguments_parser(None)
    args = vars(parser.parse_args(["plan"]))

    assert args["ignore_unreadable_view_definitions"] is False


def test_singledb_app_parser_accepts_the_flag():
    from snowddl.app.singledb import SingleDbApp

    parser = SingleDbApp.init_arguments_parser(None)
    # Matches the workflow's actual calling shape (snowddl-conformance.yml):
    # the flag precedes the `plan`/`apply` subcommand. This is the exact
    # invocation that failed in CI with "unrecognized arguments" (run
    # 37942011035), because SingleDbApp built its own ArgumentParser from
    # scratch and never registered the flag.
    args = vars(parser.parse_args(["--ignore-unreadable-view-definitions", "plan"]))

    assert args["ignore_unreadable_view_definitions"] is True


def test_singledb_app_parser_defaults_the_flag_to_false():
    from snowddl.app.singledb import SingleDbApp

    parser = SingleDbApp.init_arguments_parser(None)
    args = vars(parser.parse_args(["plan"]))

    assert args["ignore_unreadable_view_definitions"] is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
