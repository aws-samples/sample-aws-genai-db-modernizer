"""Local execution service — the API's only execution backend (hosted Step Functions
service retired, #175)."""

from __future__ import annotations

import logging
import os
import shutil
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from src.api.secret_guard import find_submitted_secrets, strip_secrets
from src.contracts.reality_check_marker import MarkerStatus, RealityCheckMarker
from src.report.deliverables import DELIVERABLES_DIR, FOLDER_FILENAMES
from src.shared.contract_versioning import ContractVersionError, load_contract

logger = logging.getLogger(__name__)

# How long after a file under a job folder (or its .meta marker) was last
# touched that delete_job_artifacts refuses to delete it (#529 round-1
# review, I2): a recent write usually means something else -- an in-flight
# resume, a schema-confirm -- is still touching the job, and deleting out
# from under it would race. This is a heuristic (mtime can be forged, and
# a write older than this window racing the delete isn't caught at all --
# see delete_job_artifacts' docstring), not a lock.
_STALE_WINDOW_SECONDS = 60

# _sweep_trash's bounds (#529 round-2 review, S-a): only a tombstone this
# old (left behind by a previous delete whose rmtree step failed) is
# cleared, and at most this many per call, so a large backlog can't make a
# single delete request slow.
_TRASH_SWEEP_MAX_AGE_SECONDS = 3600
_TRASH_SWEEP_MAX_ENTRIES = 5

_ENGINES = (
    "dynamodb",
    "documentdb",
    "elasticache",
    "opensearch",
    "aurora_postgresql",
    "aurora_mysql",
)

# Written by stop_execution, read by _infer_status before anything else (#383):
# the only durable record that a job was deliberately stopped. The API only
# ever runs a phase synchronously inside the POST /resume request handler
# (src/api/routes/phases.py) -- by the time that call returns (success,
# error, or an awaiting-LLM pause) there is no still-running phase left to
# interrupt. This marker instead blocks any *further* API-driven phase:
# phases.py's resume route and schema_revisions.py's confirm-all route both
# check LocalExecutionService.is_cancelled() before doing anything, so a
# cancelled job can't be resumed or have its schema confirmed over the API.
#
# The same file also carries a script-driven failure (scripts/_job_status.py,
# #517): status FAILED, written by a scripts/run_*.py entry point's _error()
# call or an unhandled exception, since that CLI path never touches
# .meta/<job_id>.json the way the orchestrator's own /resume does (#518
# round 1). Read separately by _script_failure, below, at a lower priority
# than a CANCELLED marker or a synthesis report -- see _infer_status's
# docstring for the exact ordering.
_STATUS_MARKER = "_status.json"

# The only status values _infer_status trusts outright from a _status.json
# marker, before anything else. stop_execution only ever writes "CANCELLED"
# -- a marker with anything else (FAILED included -- see _script_failure,
# which checks for it separately and later) is ignored here rather than
# trusted outright, falling through to artifact-derived status instead.
_KNOWN_MARKER_STATUSES = frozenset({"CANCELLED"})


class JobRecentlyModifiedError(Exception):
    """Raised by ``delete_job_artifacts`` when a file under the job folder,
    or its ``.meta`` marker, was modified within ``_STALE_WINDOW_SECONDS``
    (#529 round-1 review, I2). The route maps this to 409 -- the same
    status it already uses for a non-terminal job -- rather than a 400 or
    500, since the right response is "not yet, try again shortly."
    """


class ArtifactDeletionError(OSError):
    """Raised by ``delete_job_artifacts`` when part of the deletion fails
    after some of it already succeeded (#529 round-1 review, I1).
    ``removed`` lists exactly what was confirmed removed before the
    failure and ``cause`` is the underlying ``OSError``, so a caller can
    report a true partial result instead of claiming total success or
    losing track of what did get removed.
    """

    def __init__(self, removed: list[str], cause: OSError) -> None:
        super().__init__(str(cause))
        self.removed = removed
        self.cause = cause


class JobAlreadyStartedError(Exception):
    """``start_execution`` for a job that was already started (#1328 review): its input,
    and with it its engine scope, is fixed once the job runs. ``str(exc)`` says to start
    a new job instead."""


# Files that show a job was started (not merely prepared, which writes only uploads/).
_STARTED_MARKERS = (
    "_meta.json",
    "collector/output.json",
    "referee-triage/triage.json",
    "decisions/engine_scope.json",
)


class LocalExecutionService:
    """Filesystem-backed execution service for local development.

    Derives all state from the artifact directory layout produced by
    LocalArtifactStore / LocalOrchestrator.
    """

    def __init__(self, artifact_store) -> None:
        self._store = artifact_store
        # base_dir is a Path object on LocalArtifactStore
        self._root: Path = artifact_store.base_dir

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def start_execution(self, job_id: str, sfn_input: dict) -> dict:
        """Store job metadata locally and return a fake execution ARN.

        Refuses an input that carries a secret rather than writing it to ``_meta.json``
        (ADR-004 Decision 5, #723); the API refuses such a request before it gets here.
        A ``target_databases`` scope (not ``None``) is recorded as the job's engine scope
        decision (#1328), which triage reads. A job that was already started (any of
        ``_STARTED_MARKERS``) raises :class:`JobAlreadyStartedError` and nothing is
        written; a prepared job (only ``uploads/``) starts.
        """
        if find_submitted_secrets(sfn_input):
            raise ValueError("refusing to store an execution input that carries a secret")
        database_name = sfn_input.get("database_name", "unknown")
        if any(self._store.exists(f"{database_name}/{job_id}/{m}") for m in _STARTED_MARKERS):
            # A second start would rewrite the job's input and its engine scope after
            # triage may have routed under the first one (#1328 review).
            raise JobAlreadyStartedError(
                f"Job {job_id} was already started, and its input and engine scope are "
                "fixed. Start a new job (leave job_id out, or prepare a new one) to run "
                "with another scope."
            )
        meta = {
            "job_id": job_id,
            "database_name": database_name,
            "started_at": datetime.now(UTC).isoformat(),
            "input": sfn_input,
        }
        self._store.write_json(f"{database_name}/{job_id}/_meta.json", meta)
        scope = sfn_input.get("target_databases")
        if scope is not None:
            # #1328: the customer's engine scope, read by triage for this job.
            from src.agents.referee.engine_scope import record_engine_scope

            record_engine_scope(self._store, database_name, job_id, scope, via="api")
        return {
            "execution_arn": self._execution_arn(job_id),
            "start_date": meta["started_at"],
        }

    def describe_execution(self, job_id: str) -> dict | None:
        """Reconstruct execution status from artifact directories."""
        db_name, job_dir = self._find_job_dir(job_id)
        if job_dir is None:
            return None

        started_at = self._read_started_at(db_name, job_id)
        status, stopped_at, error = self._infer_status(job_dir)

        result: dict = {
            "status": status,
            "started_at": started_at,
            "stopped_at": stopped_at,
            "input": {
                "database_name": db_name,
                "source_database_type": self._read_source_engine(db_name, job_id),
            },
        }
        if error is not None:
            result["error"] = "PhaseFailed"
            result["cause"] = error

        # Merge full input from _meta.json if available
        meta_path = f"{db_name}/{job_id}/_meta.json"
        if self._store.exists(meta_path):
            try:
                meta = self._store.read_json(meta_path)
                # An input stored before #723 may still hold connection.password.
                result["input"] = strip_secrets(meta.get("input", result["input"]))
            except Exception:
                pass  # nosec B110 - non-critical metadata enrichment

        return result

    def get_execution_history(self, job_id: str) -> list[dict]:
        """Reconstruct per-stage history from which artifact dirs exist."""
        db_name, job_dir = self._find_job_dir(job_id)
        if job_dir is None:
            return []

        from src.storage.assignment_versioning import (
            resolve_downstream_assignment_version,
            synthesis_report_candidates,
        )

        # Schema design and synthesis key their output on the effective assignment
        # version (v2 when Reality Check consolidated), ADR-028.
        schema_version = resolve_downstream_assignment_version(self._store, db_name, job_id)
        job_prefix = f"{db_name}/{job_id}/"
        synthesis_candidates = [
            key.removeprefix(job_prefix)
            for key in synthesis_report_candidates(self._store, db_name, job_id)
        ]

        stages = []
        # Use SFN-compatible state names so assessments.py routes work unchanged.
        # Order mirrors the cloud SFN execution sequence.
        # Format: (sfn_state_name, artifact_dir, [completion_artifacts])
        stage_defs: list[tuple[str, str, list[str]]] = [
            ("RunCollector", "collector", ["collector/output.json"]),
            ("RunRefereeTriage", "referee-triage", ["referee-triage/triage.json"]),
        ]

        # Analysis stages (parallel in cloud, listed per-engine here)
        for engine in _ENGINES:
            analysis_dir = job_dir / f"analysis-{engine}"
            if analysis_dir.exists():
                stage_defs.append(
                    (
                        "RunAnalysis",
                        f"analysis-{engine}",
                        [f"analysis-{engine}/analysis.json"],
                    )
                )

        # Assignment resolution
        stage_defs.append(
            (
                "RunAssignmentResolution",
                "assignment",
                ["assignment/v1/assignment.json", "assignment/assignments.json"],
            )
        )

        # Reality check — only include if directory exists
        has_reality_check = (job_dir / "reality-check").exists()
        if has_reality_check:
            stage_defs.append(("RunRealityCheck", "reality-check", ["reality-check/output.json"]))

        # Assignment update + approval gates.
        # In cloud SFN these appear after reality-check completes.
        # In local mode: if schema dirs exist, approval was implicitly given (completed).
        # If no schema dirs but reality-check is done, show approval as in-progress.
        has_schema = any((job_dir / f"schema-{e}").exists() for e in _ENGINES)
        has_reality_output = self._store.exists(f"{db_name}/{job_id}/reality-check/output.json")

        if has_schema:
            # Approval was given — both stages completed
            stage_defs.append(
                (
                    "UpdateAssignmentVersionAfterRealityCheck",
                    "assignment",
                    ["assignment/v1/assignment.json", "assignment/assignments.json"],
                )
            )
            stage_defs.append(
                (
                    "WaitForAssignmentApproval",
                    "assignment",
                    ["assignment/v1/assignment.json", "assignment/assignments.json"],
                )
            )
        elif has_reality_output:
            # Reality check done but no schema yet — waiting for approval
            stage_defs.append(
                (
                    "UpdateAssignmentVersionAfterRealityCheck",
                    "assignment",
                    ["assignment/v1/assignment.json", "assignment/assignments.json"],
                )
            )
            # WaitForAssignmentApproval will be emitted as in-progress below

        # Schema design stages — only include if directories exist
        for engine in _ENGINES:
            schema_dir = job_dir / f"schema-{engine}"
            if schema_dir.exists():
                stage_defs.append(
                    (
                        "RunSchemaDesign",
                        f"schema-{engine}",
                        [
                            f"schema-{engine}/v{schema_version}/schema_output.json",
                            f"schema-{engine}/schema_output.json",
                        ],
                    )
                )

        # Synthesis — only include if directory exists
        has_synthesis = (job_dir / "referee-synthesis").exists() or (job_dir / "synthesis").exists()
        if has_synthesis:
            stage_defs.append(
                (
                    "RunRefereeSynthesis",
                    (
                        "referee-synthesis"
                        if (job_dir / "referee-synthesis").exists()
                        else "synthesis"
                    ),
                    synthesis_candidates,
                )
            )

        for sfn_name, dir_name, artifact_candidates in stage_defs:
            stage_dir = job_dir / dir_name

            # Check if any completion artifact exists
            found_artifact: str | None = None
            for artifact_rel in artifact_candidates:
                artifact_path = f"{db_name}/{job_id}/{artifact_rel}"
                if self._store.exists(artifact_path):
                    found_artifact = artifact_rel
                    break

            if found_artifact:
                completed_at = self._file_mtime_iso(job_dir / found_artifact)
                stages.append(
                    {
                        "name": sfn_name,
                        "status": "completed",
                        "started_at": self._dir_mtime_iso(stage_dir),
                        "completed_at": completed_at,
                    }
                )
            elif stage_dir.exists():
                stages.append(
                    {
                        "name": sfn_name,
                        "status": "in-progress",
                        "started_at": self._dir_mtime_iso(stage_dir),
                        "completed_at": None,
                    }
                )
            # Don't emit pending stages — cloud SFN only returns reached stages

        # Human gate: if assignment is done but no schema dirs exist,
        # check whether the user already approved via the orchestrator.
        if self._is_awaiting_assignment_approval(db_name, job_id, job_dir):
            stages.append(
                {
                    "name": "WaitForAssignmentApproval",
                    "status": "in-progress",
                    "started_at": self._dir_mtime_iso(job_dir / "assignment"),
                    "completed_at": None,
                }
            )

        return stages

    def get_full_execution_history(self, job_id: str) -> list[dict]:
        """Same as get_execution_history but in hierarchical format."""
        db_name, job_dir = self._find_job_dir(job_id)
        if job_dir is None:
            return []

        stages = self.get_execution_history(job_id)
        return [
            {
                "name": s["name"],
                "type": "Task",
                "status": s["status"],
                "duration_seconds": None,
                "started_after_seconds": None,
                "started_at": s.get("started_at"),
                "completed_at": s.get("completed_at"),
                "children": [],
            }
            for s in stages
        ]

    def stop_execution(self, job_id: str) -> bool:
        """Record that the job was cancelled so it stops reporting RUNNING or
        WAITING forever (#383).

        The API only runs a phase synchronously inside the POST /resume
        request (``src/api/routes/phases.py``), which cannot be interrupted
        mid-run -- it either finishes, raises, or pauses awaiting an LLM
        response before the HTTP call ever returns. There is nothing left
        running by the time this is called. What cancelling does instead is
        write a durable ``_status.json`` marker (status, timestamp, last
        completed phase) that ``_infer_status`` checks before anything else,
        including a synthesis report that might appear later, and that
        ``is_cancelled`` uses to block any further resume or schema-confirm
        call from the API. Returns False if the job isn't found.
        """
        db_name, job_dir = self._find_job_dir(job_id)
        if job_dir is None:
            return False

        marker = {
            "status": "CANCELLED",
            "stopped_at": datetime.now(UTC).isoformat(),
            "last_phase": self._last_phase(job_id),
        }
        self._store.write_json(f"{db_name}/{job_id}/{_STATUS_MARKER}", marker)
        return True

    def _root_realpath(self) -> str:
        return os.path.realpath(str(self._root))

    def _resolve_job_dir(self, database_name: str, job_id: str) -> str:
        """Resolve ``{database_name}/{job_id}`` to its real absolute path,
        refusing anything that isn't *exactly* that literal path under the
        resolved artifact root (#529 round-1 review, C1).

        An earlier version only checked ``realpath(...).startswith(root)``
        -- "somewhere inside the root" -- which a symlink can satisfy while
        still pointing at the *wrong* thing inside the root (another job's
        folder, a database folder, the root itself). This instead requires
        two things, both necessary:

        1. Neither the database directory nor the job directory itself may
           be a symlink (``os.path.islink``) -- even one that happens to
           resolve back to the right place is refused; there is no
           legitimate reason for either to be a link.
        2. ``realpath(root/db/job)`` must equal the *literal* join of the
           resolved root with the unresolved ``database_name``/``job_id``
           components -- not merely land inside the root.

        Works whether or not the path exists yet (``job_status`` calls
        this for a job that might not exist; ``os.path.realpath`` resolves
        as far as it can and appends the rest literally). Raises
        ``ValueError`` on any violation.
        """
        root_realpath = self._root_realpath()
        db_dir = os.path.join(str(self._root), database_name)
        job_dir = os.path.join(db_dir, job_id)
        if os.path.islink(db_dir) or os.path.islink(job_dir):
            raise ValueError(f"path escapes the artifact root: {job_id!r}")
        expected = os.path.join(root_realpath, database_name, job_id)
        if os.path.realpath(job_dir) != expected:
            raise ValueError(f"path escapes the artifact root: {job_id!r}")
        return expected

    def _resolve_meta_path(self, job_id: str) -> str:
        """Same as ``_resolve_job_dir``, for ``.meta/{job_id}.json`` (#529
        round-1 review, C2): neither ``.meta`` nor the marker file itself
        may be a symlink, and the resolved path must exactly equal the
        literal join, not merely land inside the root.
        """
        root_realpath = self._root_realpath()
        meta_dir = os.path.join(str(self._root), ".meta")
        meta_file = os.path.join(meta_dir, f"{job_id}.json")
        if os.path.islink(meta_dir) or os.path.islink(meta_file):
            raise ValueError(f"path escapes the artifact root: {job_id!r}")
        expected = os.path.join(root_realpath, ".meta", f"{job_id}.json")
        if os.path.realpath(meta_file) != expected:
            raise ValueError(f"path escapes the artifact root: {job_id!r}")
        return expected

    def deliverables_dir(self, database_name: str, job_id: str) -> str | None:
        """The real path of ``{database_name}/{job_id}/deliverables/`` (#957), or
        ``None`` when the job has none.

        The job folder resolves through ``_resolve_job_dir`` (no symlinks, the
        exact literal path under the artifact root). Raises ``ValueError`` like
        ``_resolve_job_dir`` when the job path escapes the root. A symlinked
        deliverables folder is caught per file by ``deliverable_file``: no file
        under it resolves inside ``<job dir>/deliverables``.
        """
        root = self._root_realpath() + os.sep
        job_dir = self._resolve_job_dir(database_name, job_id)
        folder = os.path.join(job_dir, DELIVERABLES_DIR)
        # An absolute or traversing database name makes os.path.join drop or
        # leave the root on both sides of _resolve_job_dir's comparison; the
        # folder must still sit under the artifact root itself.
        if not os.path.realpath(folder).startswith(root):
            return None
        return folder if os.path.isdir(folder) else None

    def deliverable_file(self, database_name: str, job_id: str, filename: str) -> str | None:
        """The real path of one deliverables-folder file the UI may download
        (#957), or ``None``.

        Only a name in ``FOLDER_FILENAMES`` (the folder's own definition,
        src/report/deliverables.py) is served, and the path is built from that
        constant, never from the request. The file must be a regular file, not
        a symlink, whose real path stays inside the job's deliverables folder
        and inside the artifact root.
        """
        name = next((f for f in FOLDER_FILENAMES if f == filename), None)
        if name is None:
            return None
        folder = self.deliverables_dir(database_name, job_id)
        if folder is None:
            return None
        root = self._root_realpath() + os.sep
        candidate = os.path.join(folder, name)
        full_path = os.path.realpath(candidate)
        if not full_path.startswith(root) or not full_path.startswith(folder + os.sep):
            return None
        if os.path.islink(candidate) or not os.path.isfile(full_path):
            return None
        return full_path

    def job_status(self, database_name: str, job_id: str) -> str | None:
        """Status of exactly the job at ``{database_name}/{job_id}``.

        Resolves the folder with ``_resolve_job_dir`` -- defense in depth
        behind the route's ``job_id_param``/``database_name_param``
        dependencies, which already reject a malformed value; this only
        matters if a symlink was planted directly on disk. Used by
        ``DELETE .../{job_id}/artifacts`` (#529) to decide 404 vs 409
        against the exact folder it is about to delete, rather than
        ``describe_execution``'s job-id-only scan (``_find_job_dir``), so
        the two can never disagree about which folder they mean.

        Returns None if the folder doesn't exist. Raises ValueError if the
        resolved path escapes the artifact root.
        """
        expected_job_dir = self._resolve_job_dir(database_name, job_id)
        if not os.path.exists(expected_job_dir):
            return None
        # Pass the validated, unresolved-again expected path -- not a
        # fresh os.path.realpath call -- so _infer_status's own direct
        # Path operations (e.g. ``job_dir / "schema-x"``) see exactly the
        # location this method just validated, the same one
        # delete_job_artifacts will act on.
        status, _stopped_at, _error = self._infer_status(Path(expected_job_dir))
        return status

    def _recently_modified(
        self,
        expected_job_dir: str,
        job_dir_exists: bool,
        expected_meta: str,
        meta_exists: bool,
    ) -> bool:
        """True if any file under the job folder, or the ``.meta`` marker
        itself, has an mtime within ``_STALE_WINDOW_SECONDS`` (#529
        round-1 review, I2)."""
        now = time.time()
        paths: list[str] = []
        if job_dir_exists:
            for walk_root, _dirs, files in os.walk(expected_job_dir, followlinks=False):
                paths.extend(os.path.join(walk_root, name) for name in files)
        if meta_exists:
            paths.append(expected_meta)
        for path in paths:
            try:
                mtime = os.lstat(path).st_mtime
            except OSError:
                continue
            if now - mtime < _STALE_WINDOW_SECONDS:
                return True
        return False

    def _job_id_exists_under_another_database(
        self, root_realpath: str, excluding_database: str, job_id: str
    ) -> bool:
        """True if some ``{other_db}/{job_id}`` folder still exists under a
        database directory other than ``excluding_database`` (#529 round-1
        review, S1). ``job_id`` is meant to be globally unique, but nothing
        enforces that for a hand-picked ID, and ``.meta/{job_id}.json`` is
        keyed on ``job_id`` alone -- deleting it out from under a different
        job that happens to share the ID would corrupt that job's own
        status.
        """
        try:
            entries = list(os.scandir(root_realpath))
        except OSError:
            return False
        for entry in entries:
            if entry.name.startswith(".") or entry.name == excluding_database:
                continue
            if not entry.is_dir(follow_symlinks=False):
                continue
            if os.path.lexists(os.path.join(entry.path, job_id)):
                return True
        return False

    def _sweep_trash(self, root_realpath: str) -> None:
        """Best-effort cleanup of tombstones left under ``.trash/`` by a
        previous delete whose ``rmtree`` step failed (round-1 review's
        "what remains unguarded", S-a). Runs once at the start of every
        ``delete_job_artifacts`` call; removes only entries older than
        ``_TRASH_SWEEP_MAX_AGE_SECONDS`` and stops after
        ``_TRASH_SWEEP_MAX_ENTRIES``, so a large backlog can't make a
        single delete request slow, and never raises -- a sweep failure
        must never block the delete it's piggybacking on.
        """
        trash_dir = os.path.join(root_realpath, ".trash")
        if os.path.islink(trash_dir):
            return
        try:
            entries = list(os.scandir(trash_dir))
        except OSError:
            return
        now = time.time()
        swept = 0
        for entry in entries:
            if swept >= _TRASH_SWEEP_MAX_ENTRIES:
                break
            try:
                age = now - entry.stat(follow_symlinks=False).st_mtime
                if age < _TRASH_SWEEP_MAX_AGE_SECONDS:
                    continue
                if entry.is_dir(follow_symlinks=False):
                    shutil.rmtree(entry.path)
                else:
                    os.remove(entry.path)
                swept += 1
            except OSError:
                continue  # nosec B112 - best-effort; try the next entry

    def delete_job_artifacts(self, database_name: str, job_id: str) -> list[str] | None:
        """Delete a finished job's artifact folder and orchestrator marker.

        Removes exactly ``{database_name}/{job_id}/`` and
        ``.meta/{job_id}.json`` (LocalOrchestrator's progression file,
        src/orchestrator/local_orchestrator.py) -- nothing else (#529).
        Callers must confirm the job is terminal first (``job_status``);
        this method doesn't check status itself.

        Both targets are resolved with ``_resolve_job_dir``/
        ``_resolve_meta_path`` (round-1 review, C1/C2). Sequence:

        1. Sweep ``.trash/`` for old tombstones (S-a, above).
        2. Refuse (``JobRecentlyModifiedError``, round-1 I2) if any file
           under the job folder or the ``.meta`` marker was touched in the
           last ``_STALE_WINDOW_SECONDS`` -- a concurrent write (an
           in-flight resume, say) might still be in progress.
        3. List the job folder's files (the ``removed`` report, S-b: *not*
           including the marker this method is about to write itself --
           it isn't one of the job's own artifacts).
        4. Write ``_status.json`` CANCELLED directly at the validated
           ``{database_name}/{job_id}/_status.json`` path (round-2
           review, B) -- *not* through ``stop_execution``, which resolves
           ``job_id`` by scanning for the first database directory that
           happens to contain it, and would silently mark the wrong job if
           two databases share a ``job_id``. Written before the rename, so
           a concurrent API call racing this one still sees a terminal
           job; removed again if the rename then fails (round-2 review,
           C) -- or, if a marker already existed before this call, its
           exact original bytes are restored instead of just deleting the
           CANCELLED one this method wrote (round-3 review) -- so a
           failed delete leaves the job exactly as it found it.
        5. Create (or reuse) ``.trash/`` and refuse -- ``ValueError``, the
           route's fixed 400 -- unless it is a real directory still
           exactly where expected, not a symlink (round-2 review, A): a
           symlinked ``.trash/`` would make the next step rename the job
           folder to wherever that link points, including outside the
           artifact root.
        6. Move the job folder into ``.trash/{database_name}-{job_id}-
           <uuid>`` with ``os.rename`` (atomic on the same filesystem),
           then ``shutil.rmtree`` the tombstone. The rename, not the
           (slower, interruptible) rmtree, is what makes
           ``{database_name}/{job_id}/`` disappear -- a failure in the
           rmtree step is logged but doesn't undo the delete or fail the
           request. A failure in the *rename* itself leaves the job
           folder untouched and, per (4), removes the marker again and
           skips step 7 entirely (round-2 review, C).
        7. Remove ``.meta/{job_id}.json``, unless another database's
           folder still uses the same ``job_id`` (round-1 review, S1), and
           only if step 6 actually removed the job folder (or there was no
           job folder to remove in the first place).

        Returns the sorted list of removed paths, relative to the artifact
        root with forward slashes, or None if neither target exists.
        Raises ``ValueError`` if either resolved target escapes the
        artifact root, or ``.trash/`` isn't a plain directory in place
        (nothing is touched in either case), ``JobRecentlyModifiedError``
        per (2) above, or ``ArtifactDeletionError`` (wrapping the
        underlying ``OSError``, with ``.removed`` set to whatever was
        confirmed removed first) if the rename or the ``.meta`` removal
        itself fails.

        What remains unguarded, deliberately out of scope for this pass:
        a symlink planted *after* the checks above but before the rename
        (a narrow TOCTOU window -- this validates once, not with a held
        lock); a write older than the staleness window racing the rename;
        and two concurrent deletes of the same job (the second rename
        loses to ``FileNotFoundError``, surfaced to that caller as a 500,
        not a silent double-delete).
        """
        expected_job_dir = self._resolve_job_dir(database_name, job_id)
        expected_meta = self._resolve_meta_path(job_id)
        root_realpath = self._root_realpath()

        self._sweep_trash(root_realpath)

        job_dir_exists = os.path.lexists(expected_job_dir)
        meta_exists = os.path.lexists(expected_meta)
        if not job_dir_exists and not meta_exists:
            return None

        if self._recently_modified(expected_job_dir, job_dir_exists, expected_meta, meta_exists):
            raise JobRecentlyModifiedError(
                f"job {job_id!r} has files modified in the last "
                f"{_STALE_WINDOW_SECONDS}s; try again shortly"
            )

        removed: list[str] = []
        deletion_error: OSError | None = None
        job_folder_removed = not job_dir_exists  # nothing to remove counts as "not failed"

        if job_dir_exists:
            # S-b: planned/removed reports only the job's own files -- the
            # marker this method writes next isn't one of them.
            planned = sorted(
                f"{database_name}/{job_id}/"
                + os.path.relpath(os.path.join(walk_root, name), expected_job_dir).replace(
                    os.sep, "/"
                )
                for walk_root, _dirs, files in os.walk(expected_job_dir, followlinks=False)
                for name in files
            )

            status_marker_abs = os.path.join(expected_job_dir, _STATUS_MARKER)
            status_marker_existed_before = os.path.lexists(status_marker_abs)
            original_marker_bytes: bytes | None = None
            if status_marker_existed_before:
                try:
                    with open(status_marker_abs, "rb") as f:
                        original_marker_bytes = f.read()
                except OSError:
                    original_marker_bytes = None
            try:
                self._store.write_json(
                    f"{database_name}/{job_id}/{_STATUS_MARKER}",
                    {
                        "status": "CANCELLED",
                        "stopped_at": datetime.now(UTC).isoformat(),
                        "last_phase": None,
                    },
                )
            except OSError:
                pass  # nosec B110 - best-effort marker; doesn't block the delete

            trash_dir = os.path.join(root_realpath, ".trash")
            root_escape: ValueError | None = None
            try:
                os.makedirs(trash_dir, exist_ok=True)
                if os.path.islink(trash_dir) or os.path.realpath(trash_dir) != trash_dir:
                    root_escape = ValueError(f"path escapes the artifact root: {job_id!r}")
                else:
                    tombstone = os.path.join(
                        trash_dir, f"{database_name}-{job_id}-{uuid.uuid4().hex}"
                    )
                    os.rename(expected_job_dir, tombstone)
                    job_folder_removed = True
            except OSError as exc:
                deletion_error = exc

            if root_escape is not None or deletion_error is not None:
                # The rename never happened (or never got the chance to):
                # undo the marker write so a failed delete leaves the job
                # exactly as it found it (round-2 review, C). If a marker
                # already existed, restore its exact original bytes rather
                # than just leaving the CANCELLED one this method wrote in
                # anticipation of success (round-3 review suggestion) --
                # otherwise a failed delete would silently overwrite a
                # pre-existing status (e.g. an earlier, unrelated
                # cancellation's own stopped_at/last_phase).
                try:
                    if status_marker_existed_before:
                        if original_marker_bytes is not None:
                            with open(status_marker_abs, "wb") as f:
                                f.write(original_marker_bytes)
                    else:
                        os.remove(status_marker_abs)
                except OSError:
                    pass  # nosec B110 - best-effort cleanup
                if root_escape is not None:
                    raise root_escape
            else:
                # {database_name}/{job_id}/ is already gone -- that is the
                # contract this method promises -- even if the tombstone's
                # own cleanup below fails.
                removed.extend(planned)
                try:
                    shutil.rmtree(tombstone)
                except OSError as exc:
                    logger.warning(
                        "delete_job_artifacts: moved %s/%s to %s but failed to erase it: %s",
                        database_name,
                        job_id,
                        tombstone,
                        exc,
                    )

        if meta_exists and job_folder_removed:
            if self._job_id_exists_under_another_database(root_realpath, database_name, job_id):
                pass  # S1 -- another job still uses this marker; leave it
            else:
                try:
                    os.remove(expected_meta)
                    removed.append(f".meta/{job_id}.json")
                except OSError as exc:
                    deletion_error = deletion_error or exc

        if deletion_error is not None:
            raise ArtifactDeletionError(sorted(set(removed)), deletion_error)

        return sorted(set(removed))

    def _last_phase(self, job_id: str) -> str | None:
        """Return the name of the last completed (else in-progress) stage."""
        last_completed: str | None = None
        last_in_progress: str | None = None
        for stage in self.get_execution_history(job_id):
            if stage["status"] == "completed":
                last_completed = stage["name"]
            elif stage["status"] == "in-progress":
                last_in_progress = stage["name"]
        return last_completed or last_in_progress

    def list_executions(
        self, status_filter: str | None = None, max_results: int = 50
    ) -> list[dict]:
        """Scan artifact root for all {db}/{job_id} directories."""
        if not self._root.exists():
            return []

        executions = []
        for db_dir in sorted(self._root.iterdir()):
            if not db_dir.is_dir() or db_dir.name.startswith("."):
                continue
            db_name = db_dir.name
            for job_dir in sorted(db_dir.iterdir(), reverse=True):
                if not job_dir.is_dir() or job_dir.name.startswith("_"):
                    continue
                job_id = job_dir.name
                status, stopped_at, _error = self._infer_status(job_dir)
                started_at = self._read_started_at(db_name, job_id)

                entry = {
                    "job_id": job_id,
                    "status": status,
                    "started_at": started_at,
                    "stopped_at": stopped_at,
                    # Extra field — routes that need database_name can use this
                    "database_name": db_name,
                }

                if status_filter and status != status_filter:
                    continue

                executions.append(entry)
                if len(executions) >= max_results:
                    return executions

        return executions

    def _execution_arn(self, job_id: str) -> str:
        return f"local://{job_id}"

    def is_cancelled(self, job_id: str) -> bool:
        """True once ``stop_execution`` has recorded this job as CANCELLED.

        Used by ``phases.py``'s resume route and ``schema_revisions.py``'s
        confirm-all route (#518 round 1) to refuse any *further* API-driven
        phase once a job is cancelled -- see ``stop_execution``'s docstring
        for why there is nothing already-running left to interrupt.
        """
        execution = self.describe_execution(job_id)
        return execution is not None and execution["status"] == "CANCELLED"

    def _is_phase_completed(self, job_id: str, phase_name: str) -> bool:
        """Check if a phase is marked completed in the orchestrator progression file."""
        try:
            meta = self._store.read_json(f".meta/{job_id}.json")
            phase_record = meta.get("phases", {}).get(phase_name, {})
            result: bool = phase_record.get("status") == "completed"
            return result
        except Exception:
            return False

    def _is_awaiting_assignment_approval(self, db_name: str, job_id: str, job_dir: Path) -> bool:
        """True at the WaitForAssignmentApproval human gate: Reality Check (or
        the assignment resolver) is done, nothing has approved it yet, and
        schema design hasn't started. Shared by ``get_execution_history``
        (the stage list) and ``_infer_status`` (the job's own status, #518
        round 1) so the two never disagree about this gate.

        ``approval_given`` is true once either the API's own
        confirm-all/resume routes recorded ``assignment_review`` completed
        in ``.meta/<job_id>.json``, or ``scripts/run_schema_design.py`` /
        ``scripts/run_synthesis.py`` recorded the same gate passed
        (``mark_assignment_reviewed``, #517) -- in ``.meta`` too when it
        already exists, else in ``decisions/assignment_review.json``
        (``is_assignment_reviewed``, #517 round-2 review N1: never a
        hand-written ``.meta``, which ``LocalOrchestrator`` would then load
        as the job's *entire* progression). Either way this closes the gap
        for a job approved in chat, or run with ``--llm-mode none``
        straight through to Synthesis: either script can only ever run
        after this gate, so it records that fact itself the moment it
        starts, instead of leaving this method to keep reporting WAITING
        for a job that is actually running.
        """
        from src.shared.job_status import is_assignment_reviewed

        has_schema = any((job_dir / f"schema-{e}").exists() for e in _ENGINES)
        assignment_done = self._store.exists(
            f"{db_name}/{job_id}/assignment/v1/assignment.json"
        ) or self._store.exists(f"{db_name}/{job_id}/assignment/assignments.json")
        approval_given = (
            self._store.exists(f".meta/{job_id}.json")
            and self._is_phase_completed(job_id, "assignment_review")
        ) or is_assignment_reviewed(self._store, db_name, job_id)
        return assignment_done and not has_schema and not approval_given

    def _is_schema_design_awaiting_llm(self, job_prefix: str) -> bool:
        """True if any engine's ``--llm-mode external`` schema design request
        is still waiting on a response: ``llm_requests/schema_design_<engine>.json``
        exists but that engine has no ``schema_output.json`` yet (#518 round 1)."""
        for engine in _ENGINES:
            request_path = f"{job_prefix}llm_requests/schema_design_{engine}.json"
            if not self._store.exists(request_path):
                continue
            output_prefix = f"{job_prefix}schema-{engine}/"
            has_output = any(
                key.endswith("/schema_output.json")
                for key in self._store.list_prefix(output_prefix)
            )
            if not has_output:
                return True
        return False

    def _is_synthesis_awaiting_llm(self, db_name: str, job_id: str, job_prefix: str) -> bool:
        """True if ``--llm-mode external`` synthesis is waiting on a response:
        its ``llm_input.json`` exists but no ``report.json`` candidate does
        (#518 round 1). Mirrors ``_synthesis_key``/``_report_key`` in
        ``scripts/run_synthesis.py`` for the versioned vs. legacy path."""
        from src.storage.assignment_versioning import (
            resolve_effective_assignment_version,
            synthesis_report_candidates,
        )

        version = resolve_effective_assignment_version(self._store, db_name, job_id)
        llm_input_key = (
            f"{job_prefix}synthesis/v{version}/llm_input.json"
            if version > 0
            else f"{job_prefix}synthesis/llm_input.json"
        )
        if not self._store.exists(llm_input_key):
            return False
        return not any(
            self._store.exists(candidate)
            for candidate in synthesis_report_candidates(self._store, db_name, job_id)
        )

    def _progression_phase_issue(self, job_id: str) -> tuple[str, str | None] | None:
        """Read ``.meta/<job_id>.json`` directly (never through a helper that
        writes an inferred progression as a side effect, like
        ``LocalOrchestrator.get_progression``/``_load_progression`` -- #518
        round 1 / I4) and report the one thing from it that changes the job's
        own status: a FAILED phase (with its error), else an AWAITING_REVIEW
        phase (WAITING). Returns None when the file is absent or neither
        applies, so the caller falls through to artifact-derived status.
        """
        meta_path = f".meta/{job_id}.json"
        if not self._store.exists(meta_path):
            return None
        try:
            phases = self._store.read_json(meta_path).get("phases", {})
        except ContractVersionError as exc:
            # The job list and status show the refusal instead of a stale RUNNING.
            return ("FAILED", exc.public_message)
        except Exception:
            return None

        for record in phases.values():
            if record.get("status") == "failed":
                return ("FAILED", record.get("error_message"))
        for record in phases.values():
            if record.get("status") == "awaiting_review":
                return ("WAITING", None)
        return None

    def _script_failure(self, job_prefix: str) -> tuple[str, str | None, str] | None:
        """A ``scripts/run_*.py`` failure marker (``_job_status.write_failure_marker``,
        #517): the ``_status.json`` marker ``stop_execution`` also writes, but
        with ``status: FAILED`` instead of ``CANCELLED``. Returns None when
        the file is absent, unreadable, or carries any other status (a
        CANCELLED marker is already handled earlier in ``_infer_status``;
        anything else -- SUPERSEDED, a corrupted write -- falls through to
        artifact-derived status), so the caller falls through the same way
        ``_progression_phase_issue`` does for the orchestrator's own FAILED.
        """
        marker_path = f"{job_prefix}{_STATUS_MARKER}"
        if not self._store.exists(marker_path):
            return None
        try:
            marker = self._store.read_json(marker_path)
        except Exception:
            return None
        if marker.get("status") != "FAILED":
            return None

        phase = marker.get("phase") or "unknown"
        engine = marker.get("engine")
        phase_label = f"{phase}[{engine}]" if engine else phase
        error_type = marker.get("error_type")
        reason = marker.get("message") or "unknown error"
        detail = f"{error_type}: {reason}" if error_type else reason
        return ("FAILED", marker.get("failed_at"), f"{phase_label}: {detail}")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _read_source_engine(self, db_name: str, job_id: str) -> str | None:
        """Return the source engine recorded by the collector, or None if unknown.

        Jobs run by the local scripts and skills have no ``_meta.json``, so the
        collector output is the only record of the source engine.
        """
        path = f"{db_name}/{job_id}/collector/output.json"
        if not self._store.exists(path):
            return None
        try:
            engine = self._store.read_json(path)["metadata"]["source_database"]["engine"]
        except Exception:
            return None
        return str(engine) if engine else None

    def _find_job_dir(self, job_id: str) -> tuple[str, Path | None]:
        """Scan artifact root to find which {db}/{job_id} directory exists."""
        if not self._root.exists():
            return ("", None)
        for db_dir in self._root.iterdir():
            if not db_dir.is_dir() or db_dir.name.startswith("."):
                continue
            job_dir = db_dir / job_id
            if job_dir.exists():
                return (db_dir.name, job_dir)
        return ("", None)

    def _infer_status(self, job_dir: Path) -> tuple[str, str | None, str | None]:
        """Infer CANCELLED / SUCCEEDED / FAILED / WAITING / RUNNING from
        artifact presence. Returns ``(status, stopped_at, error)``; ``error``
        is only ever set for FAILED.

        Order matters (#383, #517, #518 round 1):

        1. A recorded cancellation always wins, even over a synthesis report
           that shows up afterward -- a cancelled job must never flip back to
           looking finished or running.
        2. A synthesis report means the job is genuinely done, even if some
           earlier phase's stale FAILED record is still sitting in
           ``_status.json`` or ``.meta/<job>.json`` from a retried run.
        3. A script-driven failure marker (``_job_status.write_failure_marker``,
           #517) or the orchestrator's own progression file (FAILED or
           AWAITING_REVIEW).
        4. The remaining WAITING gates: assignment approval, schema design or
           synthesis waiting on an external LLM response, Reality Check
           waiting on one.
        5. RUNNING otherwise.
        """
        db_name, job_id = job_dir.parent.name, job_dir.name
        job_prefix = f"{db_name}/{job_id}/"

        marker_path = f"{job_prefix}{_STATUS_MARKER}"
        if self._store.exists(marker_path):
            try:
                marker = self._store.read_json(marker_path)
                status = marker.get("status")
                if status in _KNOWN_MARKER_STATUSES:
                    return (str(status), marker.get("stopped_at"), None)
            except Exception:
                pass  # nosec B110 - fall through to artifact-derived status

        from src.storage.assignment_versioning import synthesis_report_candidates

        # Every synthesis output location, versioned first (ADR-028)
        completion_markers = [
            job_dir / key.removeprefix(job_prefix)
            for key in synthesis_report_candidates(self._store, db_name, job_id)
        ]
        for marker_path_obj in completion_markers:
            if marker_path_obj.exists():
                try:
                    mtime = marker_path_obj.stat().st_mtime
                    stopped_at = datetime.fromtimestamp(mtime, tz=UTC).isoformat()
                except OSError:
                    stopped_at = None
                return ("SUCCEEDED", stopped_at, None)

        script_failure = self._script_failure(job_prefix)
        if script_failure is not None:
            return script_failure

        progression_issue = self._progression_phase_issue(job_id)
        if progression_issue is not None:
            status, error = progression_issue
            return (status, None, error)

        # The remaining WAITING gates are not the same as actively running --
        # the job is blocked on a human or an external LLM response, not on
        # local work (#383, #518 round 1).
        if self._is_awaiting_assignment_approval(db_name, job_id, job_dir):
            return ("WAITING", None, None)

        if self._is_schema_design_awaiting_llm(job_prefix):
            return ("WAITING", None, None)

        if self._is_synthesis_awaiting_llm(db_name, job_id, job_prefix):
            return ("WAITING", None, None)

        awaiting_path = f"{job_prefix}reality-check/awaiting_llm.json"
        if self._store.exists(awaiting_path):
            try:
                awaiting = load_contract(
                    RealityCheckMarker,
                    self._store.read_json(awaiting_path),
                    artifact=awaiting_path,
                )
                if awaiting.status is MarkerStatus.AWAITING_LLM:
                    return ("WAITING", None, None)
            except Exception as exc:
                # An unreadable marker is not a wait: fall through to RUNNING, but say why.
                reason = str(exc) if isinstance(exc, ContractVersionError) else type(exc).__name__
                logger.warning(
                    "%s: unreadable reality-check marker, job reported as running (%s)",
                    awaiting_path,
                    reason,
                )

        return ("RUNNING", None, None)

    def _dir_mtime_iso(self, path: Path) -> str | None:
        """Return ISO timestamp from directory mtime, or None."""
        try:
            mtime = path.stat().st_mtime
            return datetime.fromtimestamp(mtime, tz=UTC).isoformat()
        except OSError:
            return None

    def _file_mtime_iso(self, path: Path) -> str | None:
        """Return ISO timestamp from file mtime, or None."""
        try:
            if path.exists():
                mtime = path.stat().st_mtime
                return datetime.fromtimestamp(mtime, tz=UTC).isoformat()
        except OSError:
            pass
        return None

    def _read_started_at(self, db_name: str, job_id: str) -> str:
        """Read started_at from _meta.json or fall back to directory mtime."""
        meta_path = f"{db_name}/{job_id}/_meta.json"
        if self._store.exists(meta_path):
            try:
                meta = self._store.read_json(meta_path)
                if meta.get("started_at"):
                    started: str = meta["started_at"]
                    return started
            except Exception:
                pass  # nosec B110 - fallback to mtime below
        # Fall back to directory mtime
        job_dir = self._root / db_name / job_id
        try:
            mtime = job_dir.stat().st_mtime
            return datetime.fromtimestamp(mtime, tz=UTC).isoformat()
        except OSError:
            return datetime.now(UTC).isoformat()
