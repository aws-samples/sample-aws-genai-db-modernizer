"""Local execution service — replaces StepFunctionsService for local development."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path


class LocalExecutionService:
    """Filesystem-backed execution service for local development.

    Implements the same interface as StepFunctionsService but derives all state
    from the artifact directory layout produced by LocalArtifactStore / LocalOrchestrator.
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
        status, stopped_at = self._infer_status(job_dir)

        result: dict = {
            "status": status,
            "started_at": started_at,
            "stopped_at": stopped_at,
            "input": {
                "database_name": db_name,
                "source_database_type": "postgresql",
            },
        }

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

        stages = []
        # Use SFN-compatible state names so assessments.py routes work unchanged.
        # Order mirrors the cloud SFN execution sequence.
        # Format: (sfn_state_name, artifact_dir, [completion_artifacts])
        stage_defs: list[tuple[str, str, list[str]]] = [
            ("RunCollector", "collector", ["collector/output.json"]),
            ("RunRefereeTriage", "referee-triage", ["referee-triage/triage.json"]),
        ]

        # Analysis stages (parallel in cloud, listed per-engine here)
        for engine in ("dynamodb", "documentdb", "elasticache", "opensearch"):
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
        has_schema = any(
            (job_dir / f"schema-{e}").exists()
            for e in ("dynamodb", "documentdb", "elasticache", "opensearch")
        )
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
        for engine in ("dynamodb", "documentdb", "elasticache", "opensearch"):
            schema_dir = job_dir / f"schema-{engine}"
            if schema_dir.exists():
                stage_defs.append(
                    (
                        "RunSchemaDesign",
                        f"schema-{engine}",
                        [
                            f"schema-{engine}/v1/schema_output.json",
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
                    "referee-synthesis",
                    ["referee-synthesis/report.json", "synthesis/report.json"],
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
        assignment_done = self._store.exists(
            f"{db_name}/{job_id}/assignment/v1/assignment.json"
        ) or self._store.exists(f"{db_name}/{job_id}/assignment/assignments.json")
        approval_given = self._store.exists(f".meta/{job_id}.json") and self._is_phase_completed(
            job_id, "assignment_review"
        )
        if assignment_done and not has_schema and not approval_given:
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
        # Wrap in the same shape StepFunctionsService returns
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
        """No-op in local mode — always succeeds."""
        return True

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
                status, stopped_at = self._infer_status(job_dir)
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

    def _is_phase_completed(self, job_id: str, phase_name: str) -> bool:
        """Check if a phase is marked completed in the orchestrator progression file."""
        try:
            meta = self._store.read_json(f".meta/{job_id}.json")
            phase_record = meta.get("phases", {}).get(phase_name, {})
            result: bool = phase_record.get("status") == "completed"
            return result
        except Exception:
            return False

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

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

    def _infer_status(self, job_dir: Path) -> tuple[str, str | None]:
        """Infer SUCCEEDED / RUNNING / FAILED from artifact presence."""
        # Check both synthesis output locations
        completion_markers = [
            job_dir / "referee-synthesis" / "report.json",
            job_dir / "synthesis" / "report.json",
        ]
        for marker in completion_markers:
            if marker.exists():
                try:
                    mtime = marker.stat().st_mtime
                    stopped_at = datetime.fromtimestamp(mtime, tz=UTC).isoformat()
                except OSError:
                    stopped_at = None
                return ("SUCCEEDED", stopped_at)
        return ("RUNNING", None)

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
