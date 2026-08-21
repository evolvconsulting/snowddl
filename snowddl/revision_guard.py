"""OIE patch (apply-revision-guard) — an apply refuses to write an object backwards.

Labelled BY NAME, never by number. Two OIE patches were both called "#9" and the
second silently reverted the first; it went unnoticed for about four weeks.

THE PROBLEM. Several clones of a config repo can share one Snowflake identity. An
apply from any of them applies THAT clone's config at whatever revision it is
sitting on, so a clone on stale config silently overwrites a clone's newer work and
exits 0 with a clean plan of its own. Objects are OVERWRITTEN, not dropped, so every
existence check still passes -- SHOW finds them, LAST_ALTERED moves FORWARD, the
object count is right -- and only the bodies move backwards. Measured on the OIE
account over the 30 days to 2026-08-20: 30 revert-and-restore triples across 15
objects, production carrying the reverted body 5 minutes to 46 hours.

THE RULE, and it is one rule. The revision that produced the body currently in the
target must be an ANCESTOR of the revision being applied. That covers both shapes:

  * a clone on stale main          -> the target's revision is a DESCENDANT   -> refuse
  * two clones on divergent branches -> neither is an ancestor of the other   -> refuse
  * the ordinary case, ahead on one branch -> ancestor                        -> proceed

An ancestry that cannot be PROVEN -- the recorded commit is not in this clone -- is a
refusal, not a pass. A missing commit is exactly what a stale clone looks like.

PER OBJECT, NEVER PER DATABASE. A database-level stamp would refuse nearly every
apply, because any two clones mid-feature are divergent, and a control that demands
its override on every run trains people to pass it.

WHERE IT FIRES. `AbstractResolver._create_object_entry_point` /
`_compare_object_entry_point` -- the seam OIE patch #10 added for exactly this kind
of cross-cutting wrap. Raising there means the existing `_process_tasks` records
ERROR, issues ZERO DDL for that object, lets every sibling proceed, and drives the
exit 8 the deploy workflow already fails on.

PLAN NEVER REFUSES. On `plan`, `settings.execute_safe_ddl` is False and nothing is
written, so a refusal there would only red-line the conformance gates. The verdict is
still computed and logged as a warning, which makes a pre-apply plan the cheapest
place to see what an apply would refuse.

SELF-DISABLING WHEN THE TABLE IS ABSENT. A fork user who has not created
OBSERVABILITY.SNOWDDL_APPLIED_REVISION gets a warning and unchanged behaviour, and
that is also how the OIE account bootstraps: the table ships one deploy before the
pin that reads it. Dropping the table therefore disables the guard -- deliberate, and
covered from the other side by SP_CHECK_SNOWDDL_APPLY_MONOTONIC, which reads
ACCOUNT_USAGE.QUERY_HISTORY rather than this table and so still sees a bypass.

COST. Two round trips per apply -- one read at startup, one insert at the end --
independent of how many objects the apply touches. The per-object check is in memory.
"""

from logging import getLogger, NullHandler
from os import environ
from pathlib import Path
from platform import node as hostname
from socket import gethostname
from subprocess import run, DEVNULL, PIPE
from threading import Lock
from typing import Dict, List, Optional
from uuid import uuid4

logger = getLogger(__name__)
logger.addHandler(NullHandler())


REVISION_SCHEMA = "OBSERVABILITY"
REVISION_TABLE = "SNOWDDL_APPLIED_REVISION"
REVISION_VIEW = "V_SNOWDDL_OBJECT_REVISION"

# Only these two mean "this apply wrote the body". NOCHANGE did not change it, so
# stamping it would launder a stale body into looking current.
RECORDED_RESULTS = ("CREATE", "REPLACE")


class SnowDDLRevisionRefusedError(Exception):
    """Raised instead of writing an object whose current body is not an ancestor.

    A distinct type, not a bare Exception: `_process_tasks` turns any exception into
    ResolveResult.ERROR, and a refusal must never read as a generic resolver error.
    """


def _git(config_path: Path, *args: str) -> Optional[str]:
    try:
        proc = run(
            ["git", "-C", str(config_path), *args],
            stdout=PIPE,
            stderr=DEVNULL,
            stdin=DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, ValueError):
        return None

    if proc.returncode != 0:
        return None

    return (proc.stdout or "").strip()


class RevisionContext:
    """What revision is this apply, and where is it being run from."""

    def __init__(self, config_path: Path):
        self.config_path = Path(config_path)
        self.apply_id = str(uuid4())

        # CI hands us the SHA directly. Prefer the working copy's own HEAD when both
        # exist and agree; when only the environment has it, take it -- but a value
        # git cannot confirm is still usable only for RECORDING, never for ancestry,
        # which is why `is_resolvable` below is about git, not about this string.
        head = _git(self.config_path, "rev-parse", "HEAD")
        self.sha = head or environ.get("GITHUB_SHA") or None

        branch = _git(self.config_path, "rev-parse", "--abbrev-ref", "HEAD")
        if branch in (None, "HEAD"):
            branch = environ.get("GITHUB_REF_NAME") or None
        self.branch = branch

        status = _git(self.config_path, "status", "--porcelain", "--", ".")
        self.is_dirty = bool(status)

        run_id = environ.get("GITHUB_RUN_ID")
        if run_id:
            self.applied_from = f"github-actions/{run_id}:{self.config_path}"
        else:
            host = hostname() or gethostname() or "unknown-host"
            self.applied_from = f"{host}:{self.config_path}"

    @property
    def is_resolvable(self) -> bool:
        """Can this clone answer ancestry questions at all?"""
        return bool(self.sha) and _git(self.config_path, "rev-parse", "--git-dir") is not None


class RevisionGuard:
    def __init__(self, engine, config_path, database: Optional[str], override_reason: Optional[str] = None):
        self.engine = engine
        self.context = RevisionContext(Path(config_path))
        self.database = database
        self.override_reason = override_reason or None

        self.enabled = False
        self.disabled_reason: Optional[str] = None

        self._stamps: Dict[str, Dict] = {}
        self._records: List[Dict] = []
        self._ancestry_cache: Dict[str, bool] = {}
        self._lock = Lock()

    # -- lifecycle ---------------------------------------------------------

    def load(self):
        """One query. Disables the guard rather than failing the apply."""
        if not self.database:
            self.disabled_reason = "no target database resolved"
            return

        if not self.context.is_resolvable:
            # Deliberately NOT a silent pass. Without a revision every comparison is
            # unanswerable, and an unanswerable comparison is the stale case.
            self.disabled_reason = (
                f"config path [{self.context.config_path}] is not a git working copy, or git is unavailable -- "
                f"the apply-revision guard cannot answer whether this config is newer than the target"
            )
            logger.warning(f"Apply-revision guard DISABLED: {self.disabled_reason}")
            return

        try:
            cur = self.engine.execute_meta(
                "SELECT OBJECT_FULL_NAME, REVISION_SHA, REVISION_BRANCH, APPLIED_BY_USER, APPLIED_FROM, APPLIED_AT "
                "FROM {database:i}.{schema:i}.{view:i}",
                {"database": self.database, "schema": REVISION_SCHEMA, "view": REVISION_VIEW},
            )
        except Exception as e:
            self.disabled_reason = f"{REVISION_SCHEMA}.{REVISION_VIEW} is not readable in [{self.database}]: {e}"
            logger.warning(f"Apply-revision guard DISABLED: {self.disabled_reason}")
            return

        for r in cur:
            self._stamps[str(r["OBJECT_FULL_NAME"])] = r

        self.enabled = True
        logger.info(
            f"Apply-revision guard ACTIVE: revision [{self.context.sha}]"
            f"{' (DIRTY working tree)' if self.context.is_dirty else ''}, "
            f"{len(self._stamps)} object(s) already stamped in [{self.database}]"
        )

        if self.context.is_dirty:
            # Recorded and warned, never blocked: a dirty tree has no SHA, so ancestry
            # cannot express it, and blocking every dirty apply kills the incident loop.
            logger.warning(
                "Apply-revision guard: the config working tree has UNCOMMITTED changes. "
                "Every object this apply writes is stamped IS_DIRTY=TRUE, which tells the next "
                "applier that the recorded commit does not fully describe what is deployed."
            )

    def flush(self):
        """One INSERT. Nothing to do on plan -- nothing was written."""
        if not self._records or not self.engine.settings.execute_safe_ddl:
            return

        columns = (
            "APPLY_ID, APPLIED_AT, OBJECT_TYPE, OBJECT_FULL_NAME, RESOLVE_RESULT, "
            "REVISION_SHA, REVISION_BRANCH, IS_DIRTY, TARGET_DB, APPLIED_BY_USER, "
            "APPLIED_FROM, OVERRIDE_REASON"
        )

        values = []
        for rec in self._records:
            values.append(
                self.engine.format(
                    "({apply_id:s}, CURRENT_TIMESTAMP(), {object_type:s}, {object_full_name:s}, "
                    "{resolve_result:s}, {revision_sha:s}, {revision_branch:s}, {is_dirty:r}, "
                    "{target_db:s}, CURRENT_USER(), {applied_from:s}, {override_reason:s})",
                    {
                        "apply_id": self.context.apply_id,
                        "object_type": rec["object_type"],
                        "object_full_name": rec["object_full_name"],
                        "resolve_result": rec["resolve_result"],
                        "revision_sha": self.context.sha,
                        "revision_branch": self.context.branch,
                        "is_dirty": "TRUE" if self.context.is_dirty else "FALSE",
                        "target_db": self.database,
                        "applied_from": self.context.applied_from,
                        "override_reason": self.override_reason,
                    },
                )
            )

        self.engine.execute_meta(
            "INSERT INTO {database:i}.{schema:i}.{table:i} (" + columns + ") VALUES " + ", ".join(values),
            {"database": self.database, "schema": REVISION_SCHEMA, "table": REVISION_TABLE},
        )

        logger.info(f"Apply-revision guard: recorded {len(self._records)} object write(s) at [{self.context.sha}]")

    # -- per-object --------------------------------------------------------

    def check(self, object_type_name: str, object_full_name: str):
        if not self.enabled:
            return

        stamp = self._stamps.get(object_full_name)
        if stamp is None:
            # Bootstrap. An object nobody has stamped yet is recorded, never refused --
            # a guard that refuses everything on day one is disabled on day one.
            return

        recorded_sha = str(stamp["REVISION_SHA"] or "")
        if not recorded_sha or recorded_sha == self.context.sha:
            return

        if self.override_reason:
            return

        verdict = self._ancestry(recorded_sha)

        if verdict is True:
            return

        message = self._refusal_message(object_type_name, object_full_name, stamp, verdict)

        if not self.engine.settings.execute_safe_ddl:
            # plan: say it, do not raise. Nothing is being written.
            logger.warning(f"Apply-revision guard WOULD REFUSE on apply: {message}")
            return

        raise SnowDDLRevisionRefusedError(message)

    def record(self, object_type_name: str, object_full_name: str, resolve_result_name: str):
        if not self.enabled or resolve_result_name not in RECORDED_RESULTS:
            return

        with self._lock:
            self._records.append(
                {
                    "object_type": object_type_name,
                    "object_full_name": object_full_name,
                    "resolve_result": resolve_result_name,
                }
            )

    # -- internals ---------------------------------------------------------

    def _ancestry(self, recorded_sha: str) -> Optional[bool]:
        """True = ancestor (proceed). False = not an ancestor. None = cannot be proven."""
        with self._lock:
            if recorded_sha in self._ancestry_cache:
                return self._ancestry_cache[recorded_sha]

            if _git(self.context.config_path, "cat-file", "-e", f"{recorded_sha}^{{commit}}") is None:
                result: Optional[bool] = None
            else:
                proc = run(
                    [
                        "git",
                        "-C",
                        str(self.context.config_path),
                        "merge-base",
                        "--is-ancestor",
                        recorded_sha,
                        str(self.context.sha),
                    ],
                    stdout=DEVNULL,
                    stderr=DEVNULL,
                    stdin=DEVNULL,
                )
                result = proc.returncode == 0

            self._ancestry_cache[recorded_sha] = result
            return result

    def _refusal_message(self, object_type_name, object_full_name, stamp, verdict) -> str:
        who = stamp.get("APPLIED_BY_USER") or "unknown"
        where = stamp.get("APPLIED_FROM") or "unknown"
        when = stamp.get("APPLIED_AT")
        branch = stamp.get("REVISION_BRANCH") or "unknown branch"

        if verdict is None:
            why = (
                f"commit [{stamp['REVISION_SHA']}] is NOT PRESENT in this clone, so it cannot be shown to be "
                f"older than [{self.context.sha}]. Run `git fetch --all` and try again -- an unfetched commit "
                f"is exactly what a stale clone looks like"
            )
        else:
            why = (
                f"commit [{stamp['REVISION_SHA']}] is NOT AN ANCESTOR of [{self.context.sha}], so writing it "
                f"would move the body BACKWARDS or sideways. Merge that work first, or re-apply from a revision "
                f"that contains it"
            )

        return (
            f"REFUSED to write {object_type_name} [{object_full_name}] in [{self.database}]: {why}. "
            f"The body currently there was applied by [{who}] from [{where}] at [{when}] on [{branch}]. "
            f"To overwrite it deliberately, re-run with --allow-revision-override '<reason>'; the reason is recorded."
        )
