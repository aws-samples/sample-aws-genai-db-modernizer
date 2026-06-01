"""Local orchestrator — filesystem-backed, no cloud dependencies.

Enforces the same phase ordering as the Step Functions state machine.
Fan-out phases (Analysis, Schema Design) use ProcessPoolExecutor.
Progression stored as JSON in ``{artifact_dir}/.meta/{job_id}.json``.

Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 13.1, 13.2, 13.3
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from src.contracts.phase_models import (
    PHASE_PREREQUISITES,
    Phase,
    PhaseProgression,
    PhaseRecord,
    PhaseStatus,
)
from src.orchestrator.base import Orchestrator, PhasePrerequisiteError, PhaseScope
from src.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)

# Phases that fan out across engines and can run in parallel
_FAN_OUT_PHASES = {Phase.ANALYSIS, Phase.SCHEMA_DESIGN}


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
        return PhaseProgression.model_validate(raw)

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
    ) -> None:
        """Update a phase record's status and timestamps in-place."""
        record = progression.phases[phase]
        record.status = status
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
        triage_key = f"{database_name}/{job_id}/referee-triage/triage.json"
        if not self.store.exists(triage_key):
            return []
        triage = self.store.read_json(triage_key)
        return [a["agent_type"] for a in triage.get("selected_agents", [])]

    def _get_assignment_version(self, job_id: str, database_name: str) -> int:
        """Find the latest assignment version."""
        prefix = f"{database_name}/{job_id}/assignment/"
        keys = self.store.list_prefix(prefix)
        max_version = 0
        for key in keys:
            parts = key.replace(prefix, "").split("/")
            if parts and parts[0].startswith("v"):
                try:
                    max_version = max(max_version, int(parts[0][1:]))
                except ValueError:
                    continue
        return max_version

    def _get_engines_with_in_scope_queries(
        self, job_id: str, database_name: str, assignment_version: int
    ) -> set[str]:
        """Return engines that have at least one in-scope query assigned."""
        if assignment_version == 0:
            return set(self._get_selected_engines(job_id, database_name))
        path = f"{database_name}/{job_id}/assignment/v{assignment_version}/assignment.json"
        if not self.store.exists(path):
            return set()
        assignment = self.store.read_json(path)
        engines: set[str] = set()
        for qa in assignment.get("query_assignments", []):
            if qa.get("in_scope", True):
                engines.add(qa["assigned_engine"])
        return engines

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
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        from src.agents.schema_design.handler import run_schema_design_auto as run_schema_design

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

        if len(engines_to_run) <= 1:
            for engine in engines_to_run:
                run_schema_design(
                    job_id,
                    database_name,
                    engine,
                    self.store,
                    assignment_version=assignment_version,
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
                qa["assigned_engine"]
                for qa in assignment.get("query_assignments", [])
                if qa.get("in_scope", True)
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

    def _run_post_schema_routing(self, job_id: str, database_name: str) -> None:
        """Run the deterministic post-schema router and cascade schema design.

        After all engines complete schema design, reads unsupported patterns and
        PE routing notes, routes orphan queries to the next-best engine, then
        re-runs schema design for target engines with injected queries.

        Bounded by max_depth=2 to prevent infinite cascades.
        """
        from collections import Counter

        from src.agents.referee.post_schema_router import route_unsupported_queries
        from src.agents.schema_design.handler import run_schema_design_with_injected

        selected_engines = self._get_selected_engines(job_id, database_name)
        assignment_version = self._get_assignment_version(job_id, database_name)

        # Load query texts for exclusion checking
        collector_key = f"{database_name}/{job_id}/collector/output.json"
        collector = self.store.read_json(collector_key)
        query_texts = {
            q["query_id"]: q.get("query_text", "")
            for q in collector.get("queries", {}).get("query_patterns", [])
        }

        already_routed: set[str] = set()
        max_depth = 2

        for depth in range(max_depth + 1):
            print(f"\n[router] === Routing pass {depth} ===")

            # Load schema outputs
            schema_outputs: dict[str, dict] = {}
            pe_notes_by_engine: dict[str, list[str]] = {}
            for engine in selected_engines:
                # Try versioned path first, then legacy
                for key_template in [
                    f"{database_name}/{job_id}/schema-{engine}/v{assignment_version}/schema_output.json",
                    f"{database_name}/{job_id}/schema-{engine}/v1/schema_output.json",
                    f"{database_name}/{job_id}/schema-{engine}/schema_output.json",
                ]:
                    if self.store.exists(key_template):
                        schema_outputs[engine] = self.store.read_json(key_template)
                        break

                # Load PE notes if available
                for pe_key in [
                    f"{database_name}/{job_id}/schema-{engine}/v{assignment_version}/pe_review.json",
                    f"{database_name}/{job_id}/schema-{engine}/v1/pe_review.json",
                ]:
                    if self.store.exists(pe_key):
                        review = self.store.read_json(pe_key)
                        pe_notes_by_engine[engine] = review.get("pe_notes", [])
                        break

            if not schema_outputs:
                print("[router] No schema outputs found — skipping routing")
                return

            # Run router
            router_output = route_unsupported_queries(
                schema_outputs=schema_outputs,
                active_engines=selected_engines,
                pe_notes_by_engine=pe_notes_by_engine,
                query_texts=query_texts,
                cascade_depth=depth,
                max_depth=max_depth,
                already_routed=already_routed,
            )
            router_output.job_id = job_id

            # Write router output
            router_key = f"{database_name}/{job_id}/post-schema-router/router_output.json"
            self.store.write_json(router_key, router_output.model_dump(mode="json"))

            if not router_output.routings:
                if router_output.terminal_queries:
                    print(
                        f"[router] {len(router_output.terminal_queries)} terminal queries "
                        f"(application-layer): {router_output.terminal_queries[:5]}"
                    )
                else:
                    print("[router] No unsupported queries — all queries have a home")
                return

            # Log what's being routed
            by_target = Counter(r.to_engine for r in router_output.routings if r.to_engine)
            print(f"[router] Routing {len(router_output.routings)} queries:")
            for engine, count in by_target.most_common():
                reasons = {r.reason[:50] for r in router_output.routings if r.to_engine == engine}
                print(f"[router]   {count} → {engine} ({', '.join(reasons)})")
            if router_output.terminal_queries:
                print(f"[router]   {len(router_output.terminal_queries)} → application-layer")

            # Group routings by target engine and run cascade schema design
            by_engine: dict[str, set[str]] = {}
            for r in router_output.routings:
                if r.to_engine:
                    by_engine.setdefault(r.to_engine, set()).add(r.query_id)
                    already_routed.add(r.query_id)

            for engine, injected_ids in by_engine.items():
                print(
                    f"[router] Running cascade schema design for {engine} "
                    f"({len(injected_ids)} injected queries)"
                )
                run_schema_design_with_injected(
                    job_id=job_id,
                    database_name=database_name,
                    target_type=engine,
                    store=self.store,
                    injected_query_ids=injected_ids,
                    assignment_version=assignment_version,
                )

            print(f"[router] Cascade pass {depth} complete — checking for new unsupported...")

        print(f"[router] Max cascade depth ({max_depth}) reached — stopping")

    def _run_synthesis(self, job_id: str, database_name: str) -> None:
        """Run the synthesis agent."""
        from src.agents.referee.synthesis_handler import run_synthesis

        assignment_version = self._get_assignment_version(job_id, database_name)
        run_synthesis(
            job_id,
            database_name,
            self.store,
            assignment_version=assignment_version,
        )

    # ------------------------------------------------------------------
    # Prerequisite validation
    # ------------------------------------------------------------------

    def _validate_prerequisites(self, job_id: str, phase: Phase) -> None:
        """Raise PhasePrerequisiteError if any prerequisite is not COMPLETED."""
        progression = self._load_progression(job_id)
        for prereq in PHASE_PREREQUISITES[phase]:
            prereq_status = progression.phases[prereq].status
            if prereq_status != PhaseStatus.COMPLETED:
                raise PhasePrerequisiteError(
                    f"{prereq.value} must be COMPLETED before {phase.value} "
                    f"(current status: {prereq_status.value})"
                )

    def _auto_complete_prerequisites(self, job_id: str, phase: Phase) -> None:
        """Run any incomplete prerequisite phases before proceeding.

        In local dev mode, this allows the UI to trigger a phase without
        manually running every intermediate step first.
        """
        progression = self._load_progression(job_id)
        for prereq in PHASE_PREREQUISITES[phase]:
            if progression.phases[prereq].status != PhaseStatus.COMPLETED:
                logger.info("Auto-running prerequisite %s for %s", prereq.value, phase.value)
                # Recursively ensure prereq's own prerequisites are met
                self._auto_complete_prerequisites(job_id, prereq)
                # Reload progression (may have been updated by recursive call)
                progression = self._load_progression(job_id)
                # Run the prerequisite phase
                self._set_phase_status(progression, prereq, PhaseStatus.IN_PROGRESS)
                self._save_progression(progression)
                self._run_phase(job_id, prereq)
                self._set_phase_status(progression, prereq, PhaseStatus.COMPLETED)
                self._save_progression(progression)

    # ------------------------------------------------------------------
    # Orchestrator interface
    # ------------------------------------------------------------------

    def start_job(self, job_id: str, config: dict) -> None:
        """Run Phase 1 (Collect+Triage) then Phase 2 (Analysis), pause for review."""
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

        Raises PhasePrerequisiteError if any prerequisite is not COMPLETED.
        """
        self._validate_prerequisites(job_id, phase)

        progression = self._load_progression(job_id)

        self._set_phase_status(progression, phase, PhaseStatus.IN_PROGRESS)
        self._save_progression(progression)

        try:
            self._run_phase(job_id, phase, scope=scope)
        except Exception as exc:
            self._set_phase_status(
                progression,
                phase,
                PhaseStatus.FAILED,
                error_message=str(exc),
            )
            self._save_progression(progression)
            raise

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
