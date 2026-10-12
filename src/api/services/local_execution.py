"""Local execution service — the API's only execution backend (hosted Step Functions
service retired, #175)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

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
        """Store job metadata locally and return a fake execution ARN."""
        database_name = sfn_input.get("database_name", "unknown")
        meta = {
            "job_id": job_id,
            "database_name": database_name,
            "started_at": datetime.now(UTC).isoformat(),
            "input": sfn_input,
        }
        self._store.write_json(f"{database_name}/{job_id}/_meta.json", meta)
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
                result["input"] = meta.get("input", result["input"])
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
                awaiting = self._store.read_json(awaiting_path)
                if awaiting.get("status") == "awaiting_llm":
                    return ("WAITING", None, None)
            except Exception:
                pass  # nosec B110 - fall through to RUNNING

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
