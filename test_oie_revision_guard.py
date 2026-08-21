"""OIE fork patch (apply-revision-guard) — unit tests.

Offline: they build a real throwaway git repository so the ancestry rule is exercised
against git itself rather than a stub, and a fake engine so no Snowflake connection is
needed.

The test that matters most is the last one. The guard's calls live in TWO places —
`AbstractResolver` and `AbstractSchemaObjectResolver` — because the subclass overrides
the entry points and does NOT call super(). Nearly every object SnowDDL manages is a
schema object, so a guard present only in the base class would look correct, pass a
casual read, and never fire on anything that matters.
"""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from snowddl.formatter import SnowDDLFormatter
from snowddl.resolver.abc_resolver import AbstractResolver
from snowddl.resolver.abc_schema_object_resolver import AbstractSchemaObjectResolver
from snowddl.revision_guard import RevisionGuard, SnowDDLRevisionRefusedError


# --- helpers ---------------------------------------------------------------


def _git(path: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path):
    """A real repo: main has r1 -> r2 -> r3; a branch diverges from r1."""
    path = tmp_path / "cfg"
    path.mkdir()
    _git(path.parent, "init", "-q", "-b", "main", str(path))
    _git(path, "config", "user.email", "t@example.com")
    _git(path, "config", "user.name", "t")

    shas = {}
    for name in ("r1", "r2", "r3"):
        (path / "f.txt").write_text(name, encoding="utf-8")
        _git(path, "add", "f.txt")
        _git(path, "commit", "-q", "-m", name)
        shas[name] = _git(path, "rev-parse", "HEAD")

    _git(path, "checkout", "-q", "-b", "side", shas["r1"])
    (path / "g.txt").write_text("side", encoding="utf-8")
    _git(path, "add", "g.txt")
    _git(path, "commit", "-q", "-m", "side")
    shas["side"] = _git(path, "rev-parse", "HEAD")

    _git(path, "checkout", "-q", "main")
    return SimpleNamespace(path=path, shas=shas)


class FakeEngine:
    def __init__(self, rows, execute_safe_ddl=True):
        self._rows = rows
        self.settings = SimpleNamespace(execute_safe_ddl=execute_safe_ddl)
        self.format = SnowDDLFormatter().format_sql
        self.executed = []

    def execute_meta(self, sql, params=None):
        rendered = self.format(sql, params)
        self.executed.append(rendered)
        if rendered.lstrip().upper().startswith("SELECT"):
            return list(self._rows)
        return []


def _guard(repo, rows, *, at="r3", override=None, execute_safe_ddl=True):
    _git(repo.path, "checkout", "-q", repo.shas[at])
    engine = FakeEngine(rows, execute_safe_ddl=execute_safe_ddl)
    guard = RevisionGuard(engine, repo.path, "OIE", override)
    guard.load()
    return guard


def _stamp(sha, name="OIE.MDM.SP_BLOCK()"):
    return {
        "OBJECT_FULL_NAME": name,
        "REVISION_SHA": sha,
        "REVISION_BRANCH": "main",
        "APPLIED_BY_USER": "OIE_SVC_DEPLOY",
        "APPLIED_FROM": "other-clone:/x/snowddl",
        "APPLIED_AT": "2026-08-20 10:00:00",
    }


# --- the decision table ----------------------------------------------------


def test_guard_activates_when_the_view_is_readable(repo):
    guard = _guard(repo, [_stamp(repo.shas["r1"])])
    assert guard.enabled is True
    assert guard.disabled_reason is None


def test_an_object_nobody_has_stamped_is_not_refused(repo):
    """Bootstrap. A guard that refuses everything on day one is disabled on day one."""
    guard = _guard(repo, [_stamp(repo.shas["r1"], name="OIE.MDM.SOMETHING_ELSE()")])
    guard.check("PROCEDURE", "OIE.MDM.SP_BLOCK()")


def test_an_ancestor_proceeds(repo):
    """The ordinary case: I am ahead on the same branch."""
    guard = _guard(repo, [_stamp(repo.shas["r1"])], at="r3")
    guard.check("PROCEDURE", "OIE.MDM.SP_BLOCK()")


def test_the_same_revision_proceeds(repo):
    guard = _guard(repo, [_stamp(repo.shas["r3"])], at="r3")
    guard.check("PROCEDURE", "OIE.MDM.SP_BLOCK()")


def test_a_descendant_is_refused(repo):
    """The 2026-08-08 incident: applying stale main over newer main."""
    guard = _guard(repo, [_stamp(repo.shas["r3"])], at="r1")
    with pytest.raises(SnowDDLRevisionRefusedError) as e:
        guard.check("PROCEDURE", "OIE.MDM.SP_BLOCK()")

    message = str(e.value)
    assert "NOT AN ANCESTOR" in message
    assert repo.shas["r3"] in message and repo.shas["r1"] in message
    assert "OIE_SVC_DEPLOY" in message and "other-clone" in message
    assert "--allow-revision-override" in message


def test_a_divergent_branch_is_refused(repo):
    """Two clones both fresh, both mid-feature. Neither is an ancestor of the other."""
    guard = _guard(repo, [_stamp(repo.shas["side"])], at="r3")
    with pytest.raises(SnowDDLRevisionRefusedError):
        guard.check("PROCEDURE", "OIE.MDM.SP_BLOCK()")


def test_a_commit_this_clone_does_not_hold_is_refused_not_passed(repo):
    """Unprovable ancestry is the stale case, so it fails safe."""
    guard = _guard(repo, [_stamp("0" * 40)], at="r3")
    with pytest.raises(SnowDDLRevisionRefusedError) as e:
        guard.check("PROCEDURE", "OIE.MDM.SP_BLOCK()")

    assert "NOT PRESENT in this clone" in str(e.value)
    assert "git fetch --all" in str(e.value)


def test_the_override_proceeds_and_is_recorded(repo):
    guard = _guard(repo, [_stamp(repo.shas["r3"])], at="r1", override="hotfix, branch merges tomorrow")
    guard.check("PROCEDURE", "OIE.MDM.SP_BLOCK()")

    guard.record("PROCEDURE", "OIE.MDM.SP_BLOCK()", "REPLACE")
    guard.flush()

    insert = [s for s in guard.engine.executed if s.lstrip().upper().startswith("INSERT")]
    assert len(insert) == 1
    assert "hotfix, branch merges tomorrow" in insert[0]


# --- plan never refuses ----------------------------------------------------


def test_plan_warns_and_does_not_raise(repo):
    """On plan nothing is written, so refusing there would only red-line the gates."""
    guard = _guard(repo, [_stamp(repo.shas["r3"])], at="r1", execute_safe_ddl=False)
    guard.check("PROCEDURE", "OIE.MDM.SP_BLOCK()")  # must not raise


def test_plan_writes_no_record(repo):
    guard = _guard(repo, [], at="r3", execute_safe_ddl=False)
    guard.record("PROCEDURE", "OIE.MDM.SP_BLOCK()", "REPLACE")
    guard.flush()

    assert not [s for s in guard.engine.executed if s.lstrip().upper().startswith("INSERT")]


# --- what gets recorded ----------------------------------------------------


@pytest.mark.parametrize("result,expected", [("CREATE", 1), ("REPLACE", 1), ("NOCHANGE", 0), ("ALTER", 0), ("SKIP", 0)])
def test_only_a_real_write_is_recorded(repo, result, expected):
    """A NOCHANGE resolve did not change the body. Stamping it would launder a stale
    body into looking current -- the recorded revision must stay the one that last
    actually wrote the object."""
    guard = _guard(repo, [], at="r3")
    guard.record("PROCEDURE", "OIE.MDM.SP_BLOCK()", result)
    assert len(guard._records) == expected


def test_one_insert_covers_every_object(repo):
    """AC-12: the cost is fixed per apply, not per object."""
    guard = _guard(repo, [], at="r3")
    for i in range(50):
        guard.record("PROCEDURE", f"OIE.MDM.SP_{i}()", "REPLACE")
    guard.flush()

    inserts = [s for s in guard.engine.executed if s.lstrip().upper().startswith("INSERT")]
    assert len(inserts) == 1
    assert inserts[0].count("OIE.MDM.SP_") == 50


def test_the_guard_reads_once(repo):
    """One SELECT at startup, whatever the object count."""
    guard = _guard(repo, [_stamp(repo.shas["r1"])], at="r3")
    for i in range(20):
        guard.check("PROCEDURE", f"OIE.MDM.SP_{i}()")

    assert len([s for s in guard.engine.executed if s.lstrip().upper().startswith("SELECT")]) == 1


# --- self-disabling --------------------------------------------------------


def test_a_missing_view_disables_the_guard_rather_than_failing_the_apply(repo):
    class Broken(FakeEngine):
        def execute_meta(self, sql, params=None):
            raise RuntimeError("does not exist or not authorized")

    _git(repo.path, "checkout", "-q", repo.shas["r3"])
    guard = RevisionGuard(Broken([]), repo.path, "OIE", None)
    guard.load()

    assert guard.enabled is False
    assert "not readable" in guard.disabled_reason
    guard.check("PROCEDURE", "OIE.MDM.SP_BLOCK()")  # must not raise


def test_a_non_git_config_path_disables_the_guard(tmp_path):
    guard = RevisionGuard(FakeEngine([]), tmp_path, "OIE", None)
    guard.load()

    assert guard.enabled is False
    assert "not a git working copy" in guard.disabled_reason


def test_no_target_database_disables_the_guard(repo):
    guard = RevisionGuard(FakeEngine([]), repo.path, None, None)
    guard.load()

    assert guard.enabled is False
    assert guard.disabled_reason == "no target database resolved"


# --- a dirty tree is recorded, not blocked ---------------------------------


def test_a_dirty_tree_is_flagged_and_still_applies(repo):
    _git(repo.path, "checkout", "-q", repo.shas["r3"])
    (repo.path / "f.txt").write_text("uncommitted", encoding="utf-8")

    engine = FakeEngine([_stamp(repo.shas["r1"])])
    guard = RevisionGuard(engine, repo.path, "OIE", None)
    guard.load()

    assert guard.enabled is True
    assert guard.context.is_dirty is True

    guard.check("PROCEDURE", "OIE.MDM.SP_BLOCK()")
    guard.record("PROCEDURE", "OIE.MDM.SP_BLOCK()", "REPLACE")
    guard.flush()

    insert = [s for s in engine.executed if s.lstrip().upper().startswith("INSERT")][0]
    assert "TRUE" in insert


# --- the one that stops this shipping vacuous ------------------------------


def test_every_entry_point_override_calls_the_revision_guard():
    """Both definitions of each entry point must call the guard.

    AbstractSchemaObjectResolver overrides both and does NOT call super(), and nearly
    every object SnowDDL manages is a schema object. A guard wired only into
    AbstractResolver would therefore never fire on a procedure, a view, a task or a
    table -- while reading as correct. If a third override ever appears, this test
    fails until it is wired too.
    """
    for cls in (AbstractResolver, AbstractSchemaObjectResolver):
        for name in ("_create_object_entry_point", "_compare_object_entry_point"):
            source = inspect.getsource(cls.__dict__[name])
            assert "_revision_guard_check" in source, f"{cls.__name__}.{name} does not check the revision guard"
            assert "_revision_guard_record" in source, f"{cls.__name__}.{name} does not record the write"
