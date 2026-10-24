"""Local orchestrator — filesystem-backed, no cloud dependencies.

Enforces the same phase ordering as the Step Functions state machine.
Fan-out phases (Analysis, Schema Design) use ProcessPoolExecutor.
Progression stored as JSON in ``{artifact_dir}/.meta/{job_id}.json``.

Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 13.1, 13.2, 13.3
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from src.agents.referee.post_schema_routing_handler import (
    drop_losing_aurora_engine,
    selected_engines,
)
from src.contracts.phase_models import (
    PHASE_PREREQUISITES,
    SCHEMA_DESIGN_SKIP_REASON_NO_LLM,
    SKIP_SATISFIES_PREREQUISITE,
    Phase,
    PhaseProgression,
    PhaseRecord,
    PhaseStatus,
)
from src.orchestrator.base import Orchestrator, PhasePrerequisiteError, PhaseScope
from src.shared.contract_versioning import load_contract
from src.storage.artifact_store import ArtifactStore

if TYPE_CHECKING:
    from src.agents.referee.post_schema_routing_handler import PostSchemaRouting

logger = logging.getLogger(__name__)

# Phases that fan out across engines and can run in parallel
_FAN_OUT_PHASES = {Phase.ANALYSIS, Phase.SCHEMA_DESIGN}

# scripts/run_*.py's own failure-marker phase names (scripts/_job_status.py,
# #517) don't match this orchestrator's Phase values one-for-one: Collect and
# Triage are two separate scripts (and two separate markers), but one single
# Phase.COLLECT_TRIAGE here. Resuming that one phase must therefore clear
# both script phases' markers (#517 round-2 review, I3). Every other phase's
# own value already matches its script's phase name exactly.
_SCRIPT_PHASES_FOR = {Phase.COLLECT_TRIAGE: ("collect", "triage")}


class LocalOrchestrator(Orchestrator):
    """Local-dev orchestrator: direct function calls, same phase ordering rules.

    Progression is persisted as JSON via the ArtifactStore.
    """

    def __init__(self, store: ArtifactStore, llm_mode: str = "none") -> None:
        self.store = store
        self.llm_mode = llm_mode

    # ------------------------------------------------------------------
    # Progression persistence
    # ------------------------------------------------------------------

    def _progression_path(self, job_id: str) -> str:
        return f".meta/{job_id}.json"

    def _save_progression(self, progression: PhaseProgression) -> None:
        path = self._progression_path(progression.job_id)
        self.store.write_json(path, progression.model_dump(mode="json"))

    def _load_progression(self, job_id: str) -> PhaseProgression:
        path = self._progression_path(job_id)
        if not self.store.exists(path):
            return self._infer_progression(job_id)
        raw = self.store.read_json(path)
        return load_contract(PhaseProgression, raw, artifact=path)

    def _infer_progression(self, job_id: str) -> PhaseProgression:
        """Infer phase progression from existing artifacts and persist it.

        Called once for jobs created outside the orchestrator (e.g. via skills).
        After inference the .meta file is written so subsequent calls use the persisted state.
        """
        progression = self._new_progression(job_id)

        # Find the database name by scanning artifact root
        from pathlib import Path

        db_name = ""
        root = Path(getattr(self.store, "base_dir", "./artifacts"))
        for db_dir in root.iterdir():
            if db_dir.is_dir() and (db_dir / job_id).exists():
                db_name = db_dir.name
                break
        if not db_name:
            self._save_progression(progression)
            return progression

        prefix = f"{db_name}/{job_id}/"

        # Check artifact existence to infer completed phases
        phase_markers = [
            (Phase.COLLECT_TRIAGE, ["collector/output.json", "referee-triage/triage.json"]),
            (
                Phase.ANALYSIS,
                [
                    "analysis-dynamodb/analysis.json",
                    "analysis-documentdb/analysis.json",
                    "analysis-elasticache/analysis.json",
                    "analysis-opensearch/analysis.json",
                ],
            ),
            (Phase.ASSIGNMENT, ["assignment/v1/assignment.json"]),
            (Phase.REALITY_CHECK, ["reality-check/output.json"]),
        ]

        for phase, markers in phase_markers:
            if any(self.store.exists(f"{prefix}{m}") for m in markers):
                self._set_phase_status(progression, phase, PhaseStatus.COMPLETED)

        # Determine current phase from what's completed
        if progression.phases[Phase.REALITY_CHECK].status == PhaseStatus.COMPLETED:
            progression.current_phase = Phase.ASSIGNMENT_REVIEW
        elif progression.phases[Phase.ASSIGNMENT].status == PhaseStatus.COMPLETED:
            progression.current_phase = Phase.REALITY_CHECK
        elif progression.phases[Phase.ANALYSIS].status == PhaseStatus.COMPLETED:
            progression.current_phase = Phase.ASSIGNMENT
        elif progression.phases[Phase.COLLECT_TRIAGE].status == PhaseStatus.COMPLETED:
            progression.current_phase = Phase.ANALYSIS

        self._save_progression(progression)
        return progression

    # ------------------------------------------------------------------
    # Phase helpers
    # ------------------------------------------------------------------

    def _new_progression(self, job_id: str) -> PhaseProgression:
        """Create a fresh progression with all phases NOT_STARTED."""
        return PhaseProgression(
            job_id=job_id,
            current_phase=Phase.COLLECT_TRIAGE,
            phases={p: PhaseRecord(phase=p, status=PhaseStatus.NOT_STARTED) for p in Phase},
        )

    def _set_phase_status(
        self,
        progression: PhaseProgression,
        phase: Phase,
        status: PhaseStatus,
        error_message: str | None = None,
        skip_reason: str | None = None,
    ) -> None:
        """Update a phase record's status and timestamps in-place."""
        record = progression.phases[phase]
        record.status = status
        record.skip_reason = skip_reason if status == PhaseStatus.SKIPPED else None
        now = datetime.now(tz=UTC)
        if status == PhaseStatus.IN_PROGRESS:
            record.started_at = now
        elif status in (PhaseStatus.COMPLETED, PhaseStatus.FAILED, PhaseStatus.SKIPPED):
            record.completed_at = now
        if error_message is not None:
            record.error_message = error_message
        progression.current_phase = phase

    def _get_database_name(self, job_id: str) -> str:
        """Resolve database_name from the progression metadata or artifact store."""
        # Try to find collector output to extract database_name
        prefix = ""
        keys = self.store.list_prefix(prefix)
        for key in keys:
            if key.endswith(f"/{job_id}/collector/output.json"):
                # Extract database_name from path: {db}/{job}/collector/output.json
                parts = key.split("/")
                return parts[0]
        # Fallback: scan for any artifact with this job_id
        for key in keys:
            if f"/{job_id}/" in key or key.startswith(f"{job_id}/"):
                parts = key.split("/")
                if len(parts) >= 2 and parts[1] == job_id:
                    return parts[0]
        return "unknown"

    def _get_selected_engines(self, job_id: str, database_name: str) -> list[str]:
        """Read triage output to get selected engines."""
        return selected_engines(self.store, database_name, job_id)

    def _get_assignment_version(self, job_id: str, database_name: str) -> int:
        """Find the latest assignment version (0 when none). ADR-028 shared resolver."""
        from src.storage.assignment_versioning import resolve_effective_assignment_version

        return resolve_effective_assignment_version(self.store, database_name, job_id)

    def _get_engines_with_in_scope_queries(
        self, job_id: str, database_name: str, assignment_version: int
    ) -> set[str]:
        """Return engines that have at least one in-scope query assigned."""
        if assignment_version == 0:
            return set(self._get_selected_engines(job_id, database_name))
        from src.storage.assignment_versioning import engines_with_in_scope_queries

        return engines_with_in_scope_queries(self.store, database_name, job_id, assignment_version)

    def _drop_losing_aurora_engine(
        self,
        job_id: str,
        database_name: str,
        engines: list[str],
        assignment_version: int,
    ) -> list[str]:
        """Drop the #381 losing Aurora engine from ``engines``, if one is recorded
        (``post_schema_routing_handler.drop_losing_aurora_engine``)."""
        return drop_losing_aurora_engine(
            self.store, database_name, job_id, engines, assignment_version
        )

    def _run_phase(
        self,
        job_id: str,
        phase: Phase,
        config: dict | None = None,
        scope: PhaseScope | None = None,
    ) -> None:
        """Run a single phase by dispatching to actual agent handlers."""
        database_name = (config or {}).get("database_name") or self._get_database_name(job_id)

        if phase == Phase.COLLECT_TRIAGE:
            self._run_collect_triage(job_id, database_name)
        elif phase == Phase.ANALYSIS:
            self._run_analysis(job_id, database_name, scope)
        elif phase == Phase.ASSIGNMENT:
            self._run_assignment(job_id, database_name)
        elif phase == Phase.REALITY_CHECK:
            self._run_reality_check(job_id, database_name)
        elif phase == Phase.SCHEMA_DESIGN:
            self._run_schema_design(job_id, database_name, scope)
        elif phase == Phase.LOAD_TEST:
            self._run_load_test(job_id, database_name)
        elif phase == Phase.SYNTHESIS:
            self._run_synthesis(job_id, database_name)
        else:
            logger.info("Phase %s not yet implemented — skipping", phase.value)

    def _run_collect_triage(self, job_id: str, database_name: str) -> None:
        """Run collector and triage agents."""
        from src.agents.collector.handler import run_collector
        from src.agents.referee.triage_handler import run_triage

        run_collector(job_id, database_name, self.store)
        run_triage(job_id, database_name, self.store)

    def _run_analysis(self, job_id: str, database_name: str, scope: PhaseScope | None) -> None:
        """Run analysis for each selected engine in parallel via ThreadPoolExecutor."""
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from src.agents.analysis.handler import run_analysis

        engines = self._get_selected_engines(job_id, database_name)
        if scope and scope.engines:
            engines = [e for e in engines if e in scope.engines]

        if len(engines) <= 1:
            for engine in engines:
                run_analysis(job_id, database_name, engine, self.store, llm_mode=self.llm_mode)
            return

        # Fan-out: run engines in parallel (threads — avoids pickling issues with store)
        logger.info("Fan-out analysis: %d engines in parallel", len(engines))
        with ThreadPoolExecutor(max_workers=len(engines)) as pool:
            futures = {
                pool.submit(
                    run_analysis, job_id, database_name, engine, self.store, llm_mode=self.llm_mode
                ): engine
                for engine in engines
            }
            for future in as_completed(futures):
                engine = futures[future]
                try:
                    future.result()
                    logger.info("Analysis completed for %s", engine)
                except Exception:
                    logger.warning("Analysis failed for %s", engine)
                    raise

    def _run_assignment(self, job_id: str, database_name: str) -> None:
        """Run the assignment resolver agent."""
        from src.agents.referee.assignment_handler import run_assignment_resolver

        run_assignment_resolver(job_id, database_name, self.store)

    def _run_reality_check(self, job_id: str, database_name: str) -> None:
        """Run the CTO-level reality check on the assignment."""
        from src.agents.referee.reality_check_handler import run_reality_check_handler

        run_reality_check_handler(job_id, database_name, self.store, llm_mode=self.llm_mode)

    def _run_schema_design(self, job_id: str, database_name: str, scope: PhaseScope | None) -> None:
        """Run schema design for each assigned engine in parallel.

        Skips engines with zero in-scope queries (SKIPPED status).

        With ``llm_mode="none"`` no engine is designed: every schema designer
        needs a model, so the phase writes no schema outputs, as in the
        deterministic pipeline, and is recorded SKIPPED with
        ``SCHEMA_DESIGN_SKIP_REASON_NO_LLM`` (see ``_skip_reason``). Synthesis
        accepts that skip and reports the engines without a schema design
        (issue #281). The mode is still passed to each engine call so the
        handler enforces it too.
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from src.agents.referee.assignment_overrides import reroute_split_writes
        from src.agents.schema_design.handler import run_schema_design_auto as run_schema_design

        # A resumed job whose saved assignment split a table's writes is re-routed
        # to one source of truth first, as a new version (#992); schema design then
        # runs on that version.
        rerouted = reroute_split_writes(self.store, database_name, job_id, after_approval=True)
        if rerouted is not None:
            print(
                f"[schema-design] re-routed {len(rerouted.moved_query_ids)} queries to one "
                f"source of truth (assignment v{rerouted.assignment.version}); affected "
                f"engines: {', '.join(rerouted.affected_engines)}"
            )

        assignment_version = self._get_assignment_version(job_id, database_name)
        engines_with_queries = self._get_engines_with_in_scope_queries(
            job_id, database_name, assignment_version
        )
        all_engines = self._get_selected_engines(job_id, database_name)

        if scope and scope.engines:
            all_engines = [e for e in all_engines if e in scope.engines]

        engines_to_run = []
        for engine in all_engines:
            if engine not in engines_with_queries:
                logger.info("Skipping schema design for %s — zero in-scope queries", engine)
                continue
            engines_to_run.append(engine)

        if self.llm_mode == "none":
            from src.agents.schema_design.handler import SCHEMA_DESIGN_SKIPPED_NONE_MODE

            print(f"[schema-design] {SCHEMA_DESIGN_SKIPPED_NONE_MODE}")
            logger.info("Schema design skipped for %s: llm_mode=none", engines_to_run)

        if len(engines_to_run) <= 1:
            for engine in engines_to_run:
                run_schema_design(
                    job_id,
                    database_name,
                    engine,
                    self.store,
                    assignment_version=assignment_version,
                    llm_mode=self.llm_mode,
                )
            return

        # Fan-out: run engines in parallel
        logger.info("Fan-out schema design: %d engines in parallel", len(engines_to_run))
        with ThreadPoolExecutor(max_workers=len(engines_to_run)) as pool:
            futures = {
                pool.submit(
                    run_schema_design,
                    job_id,
                    database_name,
                    engine,
                    self.store,
                    assignment_version=assignment_version,
                    llm_mode=self.llm_mode,
                ): engine
                for engine in engines_to_run
            }
            for future in as_completed(futures):
                engine = futures[future]
                try:
                    future.result()
                    logger.info("Schema design completed for %s", engine)
                except Exception:
                    logger.warning("Schema design failed for %s", engine)
                    raise

    def _run_load_test(self, job_id: str, database_name: str) -> None:
        """Run load test for each engine that has a completed schema design."""
        from src.agents.load_test.handler import run_load_test
        from src.contracts.load_test_models import TestConfig

        assignment_version = self._get_assignment_version(job_id, database_name)
        assignment_key = (
            f"{database_name}/{job_id}/assignment/v{assignment_version}/assignment.json"
        )
        if self.store.exists(assignment_key):
            assignment = self.store.read_json(assignment_key)
            engines = {
                engine
                for qa in assignment.get("query_assignments", [])
                if qa.get("in_scope", True)
                # the cache layer is tested on the reads it fronts (#296)
                for engine in (qa["assigned_engine"], qa.get("cache_engine"))
                if engine
            }
        else:
            engines = set(self._get_selected_engines(job_id, database_name))

        for engine in engines:
            schema_key = (
                f"{database_name}/{job_id}/schema-{engine}"
                f"/v{assignment_version}/schema_output.json"
            )
            if not self.store.exists(schema_key):
                logger.info("Skipping load test for %s — schema output not found", engine)
                continue
            logger.info("Running load test for %s", engine)
            run_load_test(
                job_id=job_id,
                database_name=database_name,
                target_engine=engine,
                store=self.store,
                schema_version=assignment_version,
                test_config=TestConfig(),
            )

    def _post_schema_routing(self) -> PostSchemaRouting:
        """The shared post-schema routing on this store, redesigning in this mode."""
        from src.agents.referee.post_schema_routing_handler import (
            PostSchemaRouting,
            pipeline_redesign,
        )

        return PostSchemaRouting(self.store, pipeline_redesign(self.store, self.llm_mode))

    def _run_post_schema_routing(self, job_id: str, database_name: str) -> None:
        """Run the post-schema routing after schema design (#1492: the one
        implementation, ``core.run_post_schema_routing_core``, which ``assess()``
        calls too and AWS Transform is to call, #1498), redesigning with this
        orchestrator's mode."""
        from src.atx_orchestrator.core import run_post_schema_routing_core

        run_post_schema_routing_core(
            job_id, database_name, store=self.store, llm_mode=self.llm_mode
        )

    # ------------------------------------------------------------------
    # Post-schema routing, one pass at a time (headless runs, #1059)
    # ------------------------------------------------------------------

    def run_post_schema_routing_external(self, job_id: str, database_name: str) -> dict:
        """One step of the post-schema routing for a headless run; returns its status
        (``PostSchemaRouting.run_external_step``: the same passes, reroute version
        and cascade bound, the redesign left to the caller's design subagents)."""
        return self._post_schema_routing().run_external_step(job_id, database_name)

    def roll_back_post_schema_routing_external(self, job_id: str, database_name: str) -> dict:
        """After a failed redesign: the rollback version the Bedrock path writes
        (``PostSchemaRouting.roll_back_external``, #1059)."""
        return self._post_schema_routing().roll_back_external(job_id, database_name)

    def _run_synthesis(self, job_id: str, database_name: str) -> None:
        """Run the synthesis agent."""
        from src.agents.referee.synthesis_handler import run_synthesis

        assignment_version = self._get_assignment_version(job_id, database_name)
        run_synthesis(
            job_id,
            database_name,
            self.store,
            assignment_version=assignment_version,
            llm_mode=self.llm_mode,
        )

    # ------------------------------------------------------------------
    # Prerequisite validation
    # ------------------------------------------------------------------

    def _validate_prerequisites(self, job_id: str, phase: Phase) -> None:
        """Raise PhasePrerequisiteError if any prerequisite is not met.

        Met means COMPLETED, or the explicit skip ``_prerequisite_met`` accepts.
        """
        progression = self._load_progression(job_id)
        for prereq in PHASE_PREREQUISITES[phase]:
            if self._prerequisite_met(progression, phase, prereq):
                continue
            prereq_status = progression.phases[prereq].status
            if prereq_status != PhaseStatus.COMPLETED:
                raise PhasePrerequisiteError(
                    f"{prereq.value} must be COMPLETED before {phase.value} "
                    f"(current status: {prereq_status.value})"
                )

    @staticmethod
    def _prerequisite_met(progression: PhaseProgression, phase: Phase, prereq: Phase) -> bool:
        """COMPLETED, or the one explicit skip ``phase`` accepts (``SKIP_SATISFIES_PREREQUISITE``).

        Any other SKIPPED prerequisite (no reason, another reason, another
        phase) still blocks.
        """
        record = progression.phases[prereq]
        if record.status == PhaseStatus.COMPLETED:
            return True
        accepted = SKIP_SATISFIES_PREREQUISITE.get(phase, {}).get(prereq)
        return (
            accepted is not None
            and record.status == PhaseStatus.SKIPPED
            and record.skip_reason == accepted
        )

    def _skip_reason(self, phase: Phase) -> str | None:
        """Why ``phase`` was skipped by this orchestrator's run, or None if it ran."""
        if phase == Phase.SCHEMA_DESIGN and self.llm_mode == "none":
            return SCHEMA_DESIGN_SKIP_REASON_NO_LLM
        return None

    def _clear_script_markers_for_phase(
        self, job_id: str, phase: Phase, database_name: str | None = None
    ) -> None:
        """Clear a stale ``scripts/run_*.py`` failure marker for ``phase``
        (#517 round-1 review, I3) right as this orchestrator is about to
        (re)run it -- whether directly (``resume``) or as a prerequisite
        ``_auto_complete_prerequisites`` is about to force through (#517
        round-3 review). Scoped to the exact phase (and, for a fan-out
        phase, any engine -- #517 round-2 review, I3/I4): resuming the
        whole phase reruns every engine at once, not one specific engine
        retrying on its own, and Collect/Triage are two separate scripts
        (two separate possible markers) under this one orchestrator phase.
        """
        from src.shared.job_status import ANY_ENGINE, clear_failure_marker

        if database_name is None:
            database_name = self._get_database_name(job_id)
        engine = ANY_ENGINE if phase in _FAN_OUT_PHASES else None
        for script_phase in _SCRIPT_PHASES_FOR.get(phase, (phase.value,)):
            clear_failure_marker(self.store, database_name, job_id, script_phase, engine=engine)

    def _auto_complete_prerequisites(self, job_id: str, phase: Phase) -> None:
        """Run any incomplete prerequisite phases before proceeding.

        In local dev mode, this allows the UI to trigger a phase without
        manually running every intermediate step first.
        """
        progression = self._load_progression(job_id)
        for prereq in PHASE_PREREQUISITES[phase]:
            if not self._prerequisite_met(progression, phase, prereq):
                logger.info("Auto-running prerequisite %s for %s", prereq.value, phase.value)
                # Recursively ensure prereq's own prerequisites are met
                self._auto_complete_prerequisites(job_id, prereq)
                # Reload progression (may have been updated by recursive call)
                progression = self._load_progression(job_id)
                # Run the prerequisite phase, clearing any stale script
                # marker for it first (#517 round-3 review) -- this call is
                # itself the "job is moving again" evidence for prereq, the
                # same as resume() is for its own top-level phase.
                self._clear_script_markers_for_phase(job_id, prereq)
                self._set_phase_status(progression, prereq, PhaseStatus.IN_PROGRESS)
                self._save_progression(progression)
                self._run_phase(job_id, prereq)
                skip_reason = self._skip_reason(prereq)
                if skip_reason:
                    self._set_phase_status(
                        progression, prereq, PhaseStatus.SKIPPED, skip_reason=skip_reason
                    )
                else:
                    self._set_phase_status(progression, prereq, PhaseStatus.COMPLETED)
                self._save_progression(progression)

    # ------------------------------------------------------------------
    # Orchestrator interface
    # ------------------------------------------------------------------

    def start_job(self, job_id: str, config: dict) -> None:
        """Run Phase 1 (Collect+Triage) then Phase 2 (Analysis), pause for review.

        ``config["target_databases"]``, when given, is the customer's engine scope
        (#1328): it is validated and recorded as the job's engine scope decision before
        anything runs, and triage routes within it. Left out, every engine is considered.
        """
        scope = config.get("target_databases")
        if scope is not None:
            from src.agents.referee.engine_scope import record_engine_scope

            database_name = config.get("database_name") or self._get_database_name(job_id)
            record_engine_scope(self.store, database_name, job_id, scope, via="library")
        progression = self._new_progression(job_id)

        # Phase 1: Collect + Triage
        self._set_phase_status(progression, Phase.COLLECT_TRIAGE, PhaseStatus.IN_PROGRESS)
        self._save_progression(progression)
        try:
            self._run_phase(job_id, Phase.COLLECT_TRIAGE, config=config)
        except Exception as exc:
            self._set_phase_status(
                progression,
                Phase.COLLECT_TRIAGE,
                PhaseStatus.FAILED,
                error_message=str(exc),
            )
            self._save_progression(progression)
            raise
        self._set_phase_status(progression, Phase.COLLECT_TRIAGE, PhaseStatus.COMPLETED)
        self._save_progression(progression)

        # Phase 2: Analysis (fan-out)
        self._set_phase_status(progression, Phase.ANALYSIS, PhaseStatus.IN_PROGRESS)
        self._save_progression(progression)
        try:
            self._run_phase(job_id, Phase.ANALYSIS, config=config)
        except Exception as exc:
            self._set_phase_status(
                progression,
                Phase.ANALYSIS,
                PhaseStatus.FAILED,
                error_message=str(exc),
            )
            self._save_progression(progression)
            raise
        self._set_phase_status(progression, Phase.ANALYSIS, PhaseStatus.COMPLETED)

        # Pause for review after analysis
        self._set_phase_status(progression, Phase.ANALYSIS, PhaseStatus.AWAITING_REVIEW)
        self._save_progression(progression)

    def resume(self, job_id: str, phase: Phase, scope: PhaseScope | None = None) -> None:
        """Validate prerequisites, then run the requested phase.

        Raises PhasePrerequisiteError if any prerequisite is not met. A phase
        this run skips (``_skip_reason``) is recorded SKIPPED with its reason
        instead of COMPLETED / AWAITING_REVIEW.
        """
        self._validate_prerequisites(job_id, phase)

        # An API-driven resume of this exact phase starting is itself
        # evidence the job is moving again -- clear a stale scripts/run_*.py
        # failure marker for it (#517 round-1 review, I3). database_name is
        # resolved once and reused below instead of _run_phase re-deriving
        # it (#517 round-2 review nit: _get_database_name scans the whole
        # artifact tree).
        database_name = self._get_database_name(job_id)
        self._clear_script_markers_for_phase(job_id, phase, database_name)

        progression = self._load_progression(job_id)

        self._set_phase_status(progression, phase, PhaseStatus.IN_PROGRESS)
        self._save_progression(progression)

        try:
            self._run_phase(job_id, phase, config={"database_name": database_name}, scope=scope)
        except Exception as exc:
            self._set_phase_status(
                progression,
                phase,
                PhaseStatus.FAILED,
                error_message=str(exc),
            )
            self._save_progression(progression)
            raise

        skip_reason = self._skip_reason(phase)
        if skip_reason:
            # Nothing was produced, so there is nothing to review (issue #281).
            self._set_phase_status(progression, phase, PhaseStatus.SKIPPED, skip_reason=skip_reason)
            self._save_progression(progression)
            return

        self._set_phase_status(progression, phase, PhaseStatus.COMPLETED)

        # Set AWAITING_REVIEW after fan-out phases
        if phase in (Phase.ANALYSIS, Phase.SCHEMA_DESIGN):
            self._set_phase_status(progression, phase, PhaseStatus.AWAITING_REVIEW)

        self._save_progression(progression)

    def resume_lenient(self, job_id: str, phase: Phase, scope: PhaseScope | None = None) -> None:
        """Auto-complete missing prerequisites, then run the requested phase.

        Used by the local API to allow the UI to trigger phases without
        manually running every intermediate step first.
        """
        self._auto_complete_prerequisites(job_id, phase)
        # Delegate to standard resume (prerequisites now satisfied)
        self.resume(job_id, phase, scope=scope)

    def confirm_schema_design(self, job_id: str) -> None:
        """Transition SCHEMA_DESIGN to COMPLETED after user confirms all engines."""
        progression = self._load_progression(job_id)
        schema_record = progression.phases[Phase.SCHEMA_DESIGN]

        if schema_record.status != PhaseStatus.AWAITING_REVIEW:
            raise PhasePrerequisiteError(
                f"SCHEMA_DESIGN must be AWAITING_REVIEW to confirm "
                f"(current: {schema_record.status.value})"
            )

        self._set_phase_status(progression, Phase.SCHEMA_DESIGN, PhaseStatus.COMPLETED)
        self._save_progression(progression)

    def get_progression(self, job_id: str) -> PhaseProgression:
        """Read progression from the local artifact store."""
        path = self._progression_path(job_id)
        if not self.store.exists(path):
            return self._new_progression(job_id)
        return self._load_progression(job_id)
