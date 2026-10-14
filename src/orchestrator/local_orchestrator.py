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
    SCHEMA_DESIGN_SKIP_REASON_NO_LLM,
    SKIP_SATISFIES_PREREQUISITE,
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
        triage_key = f"{database_name}/{job_id}/referee-triage/triage.json"
        if not self.store.exists(triage_key):
            return []
        triage = self.store.read_json(triage_key)
        return [a["agent_type"] for a in triage.get("selected_agents", [])]

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
        """Drop the #381 losing Aurora engine from ``engines``, if one is recorded.

        Triage selects both ``aurora_mysql`` and ``aurora_postgresql`` for a
        heterogeneous source (SQL Server, Oracle, DB2) with no Aurora dialect of its
        own, so a caller working off the triage-selected engine list (post-schema
        routing) would otherwise still treat the losing engine as active even after
        the assignment resolver picked one of the two (``Assignment.
        aurora_engine_choice``). A no-op when there is no assignment yet, no recorded
        choice (homogeneous source, or a legacy assignment written before #381), or
        the loser is not even in ``engines`` to begin with.
        """
        from src.agents.referee.aurora_choice import AURORA_ENGINES
        from src.storage.assignment_versioning import assignment_artifact_path

        if assignment_version == 0:
            return engines
        path = assignment_artifact_path(database_name, job_id, assignment_version)
        if not self.store.exists(path):
            return engines
        choice = self.store.read_json(path).get("aurora_engine_choice")
        winner = choice.get("engine") if isinstance(choice, dict) else None
        if winner not in AURORA_ENGINES:
            return engines
        loser = next(e for e in AURORA_ENGINES if e != winner)
        return [e for e in engines if e != loser]

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

    def _run_post_schema_routing(self, job_id: str, database_name: str) -> None:
        """Run the deterministic post-schema router and cascade schema design.

        After all engines complete schema design, reads unsupported patterns and
        the PE reviewers' routing flags (design traces), routes each flagged
        query (with its co-dependency group) to its best remaining engine by the
        shared scorer (#629), writes the moves as a new assignment version, and
        re-runs schema design for the target engines on that version.

        Maintainer decisions (2026-10-14): the reality check consolidates first;
        PE review and the router route only among the engines the approved
        assignment keeps (never an engine the reality check removed); a reroute
        writes a new assignment version, so schema design and the assignment
        agree. Bounded by max_depth=2 to prevent infinite cascades.
        """
        from collections import Counter

        from src.agents.referee.post_schema_router import (
            route_unsupported_queries,
            routing_flags_from_trace,
        )
        from src.agents.referee.tie_break import settled_counts

        selected_engines = self._get_selected_engines(job_id, database_name)
        assignment_version = self._get_assignment_version(job_id, database_name)
        # #381 review round 2: triage selects both competing Aurora engines for a
        # heterogeneous source (SQL Server, Oracle, DB2), but the assignment resolver
        # only ever uses the one it picked -- drop the loser from the router's own
        # active set so it is never offered as a target for an orphaned/unsupported
        # query, same as every other caller downstream of the #381 choice.
        selected_engines = self._drop_losing_aurora_engine(
            job_id, database_name, selected_engines, assignment_version
        )
        # The candidates: the engines the approved assignment keeps. An engine
        # the reality check removed is never a destination.
        # The engines the approved assignment keeps; re-read every pass, so an
        # engine an earlier pass left with no query is never a destination.
        kept = self._get_engines_with_in_scope_queries(job_id, database_name, assignment_version)

        # The shared scorer's inputs (#629): the router re-routes a flagged
        # query to its best remaining engine by the same per-query score the
        # assignment used, never by an engine list or a name in the flag.
        scoring = self._router_scoring_inputs(
            job_id, database_name, selected_engines, assignment_version
        )

        already_routed: set[str] = set()
        all_routings: list = []
        all_terminal: list[str] = []
        max_depth = 2

        for depth in range(max_depth + 1):
            print(f"\n[router] === Routing pass {depth} ===")
            assignment = self._read_assignment(job_id, database_name, assignment_version)
            kept &= self._get_engines_with_in_scope_queries(
                job_id, database_name, assignment_version
            )
            active_engines = [e for e in selected_engines if e in kept]
            scoring["settled"] = settled_counts(assignment.get("query_assignments", []))

            # Load schema outputs
            schema_outputs: dict[str, dict] = {}
            flags_by_engine: dict[str, list] = {}
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

                # The PE reviewer's routing flags, recorded in the design trace
                for trace_key in [
                    f"{database_name}/{job_id}/schema-{engine}/v{assignment_version}/design_trace.json",
                    f"{database_name}/{job_id}/schema-{engine}/v1/design_trace.json",
                    f"{database_name}/{job_id}/schema-{engine}/design_trace.json",
                ]:
                    if self.store.exists(trace_key):
                        flags_by_engine[engine] = routing_flags_from_trace(
                            self.store.read_json(trace_key)
                        )
                        break

            if not schema_outputs:
                print("[router] No schema outputs found — skipping routing")
                return

            # Run router
            router_output = route_unsupported_queries(
                schema_outputs=schema_outputs,
                active_engines=active_engines,
                routing_flags_by_engine=flags_by_engine,
                cascade_depth=depth,
                max_depth=max_depth,
                already_routed=already_routed,
                assigned_engines={
                    qa["query_id"]: qa["assigned_engine"]
                    for qa in assignment.get("query_assignments", [])
                },
                # The customer's own re-routes are never moved (#713 review r3).
                customer_pinned={
                    qa["query_id"]
                    for qa in assignment.get("query_assignments", [])
                    if qa.get("customer_override")
                },
                co_dependency_groups=assignment.get("co_dependency_groups", []),
                **scoring,
            )
            router_output.job_id = job_id

            # Every pass is kept (#713 review): router_output.json accumulates
            # every pass's routings and terminal queries, and each pass is also
            # written on its own, so no pass's scores are lost.
            all_routings.extend(router_output.routings)
            all_terminal.extend(q for q in router_output.terminal_queries if q not in all_terminal)
            prefix_router = f"{database_name}/{job_id}/post-schema-router"
            self.store.write_json(
                f"{prefix_router}/pass-{depth}/router_output.json",
                router_output.model_dump(mode="json"),
            )
            cumulative = router_output.model_copy(
                update={"routings": list(all_routings), "terminal_queries": list(all_terminal)}
            )
            self.store.write_json(
                f"{prefix_router}/router_output.json", cumulative.model_dump(mode="json")
            )

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
            already_routed.update(r.query_id for r in router_output.routings if r.to_engine)

            # The moves become a new assignment version, and every engine the
            # moves touch (the one a query left and the one it joined) is
            # designed again on it (maintainer decision, 2026-10-14), so the
            # design and the assignment agree.
            assignment_version = self._reroute_and_redesign(
                job_id, database_name, assignment, assignment_version, router_output.routings
            )

            print(f"[router] Cascade pass {depth} complete — checking for new unsupported...")

        print(f"[router] Max cascade depth ({max_depth}) reached — stopping")

    def _read_assignment(self, job_id: str, database_name: str, version: int) -> dict:
        """The assignment artifact at ``version`` (``{}`` when there is none)."""
        from src.storage.assignment_versioning import assignment_artifact_path

        if not version:
            return {}
        path = assignment_artifact_path(database_name, job_id, version)
        return self.store.read_json(path) if self.store.exists(path) else {}

    def _rerouted_assignment(
        self,
        job_id: str,
        database_name: str,
        assignment: dict,
        version: int,
        new_version: int,
        routings: list,
    ) -> dict:
        """The assignment ``version`` with the router's moves applied (#629).

        Status ``rerouted_after_approval`` (never the customer approval it
        edits: the customer did not see these moves), ``source =
        post_schema_routing``, ``previous_version``, and one
        ``post_schema_reroutes`` entry per moved query (cumulative), so every
        deliverable can say which queries moved after approval and why.
        """
        from copy import deepcopy
        from datetime import UTC, datetime

        from src.agents.referee.assignment_overrides import refresh_consolidated_assignment
        from src.contracts.assignment_models import AssignmentSource, AssignmentStatus

        assignment = deepcopy(assignment)  # the refresh below edits records in place
        prior = list(assignment.get("post_schema_reroutes") or [])
        approved_version = next(
            (r["approved_version"] for r in prior if r.get("approved_version")), version
        )
        moved = {r.query_id: r for r in routings if r.to_engine}
        query_assignments = []
        for qa in assignment.get("query_assignments", []):
            routing = moved.get(qa.get("query_id"))
            if routing is not None:
                qa = {
                    **qa,
                    "assigned_engine": routing.to_engine,
                    "assignment_reason": f"post-schema routing: {routing.reason}",
                    "tied_engines": [],
                }
            query_assignments.append(qa)
        reroutes = prior + [
            {
                "query_id": r.query_id,
                "from_engine": r.from_engine,
                "to_engine": r.to_engine,
                "reason": r.reason,
                "reason_category": r.reason_category,
                "score_before": r.score_before,
                "score_after": r.score_after,
                "approved_version": approved_version,
            }
            for r in moved.values()
        ]
        revised = {
            **assignment,
            "version": new_version,
            "previous_version": version,
            "status": AssignmentStatus.REROUTED_AFTER_APPROVAL.value,
            "source": AssignmentSource.POST_SCHEMA_ROUTING.value,
            "timestamp": datetime.now(UTC).isoformat(),
            "query_assignments": query_assignments,
            "post_schema_reroutes": reroutes,
            "rolled_back_version": None,
            # #616: moves are per-version (only the Moves that produced this
            # version). The router does not record its own Moves yet (a later
            # #616 change), so the new version starts empty.
            "moves": [],
        }
        prefix = f"{database_name}/{job_id}"
        # The derived views (table mappings, warnings) follow the moved queries,
        # the same refresh the reality check's revision gets (ADR-029).
        before = {
            qa["assigned_engine"]
            for qa in assignment.get("query_assignments", [])
            if qa.get("in_scope", True)
        }
        after = {qa["assigned_engine"] for qa in query_assignments if qa.get("in_scope", True)}
        collector_key = f"{prefix}/collector/output.json"
        collector = self.store.read_json(collector_key) if self.store.exists(collector_key) else {}
        analysis_outputs = {
            e: self.store.read_json(f"{prefix}/analysis-{e}/analysis.json")
            for e in before | after
            if self.store.exists(f"{prefix}/analysis-{e}/analysis.json")
        }
        return refresh_consolidated_assignment(
            revised, collector, analysis_outputs, dead_engines=before - after
        )

    def _copy_schema_artifacts(
        self, job_id: str, database_name: str, engine: str, src: int, dst: int
    ) -> None:
        """Copy ``schema-<engine>/v<src>/*`` to ``v<dst>`` (an engine the moves left alone)."""
        old_prefix = f"{database_name}/{job_id}/schema-{engine}/v{src}/"
        for key in self.store.list_prefix(old_prefix):
            new_key = f"{database_name}/{job_id}/schema-{engine}/v{dst}/" + key[len(old_prefix) :]
            self.store.write_json(new_key, self.store.read_json(key))

    def _schema_engines(self, job_id: str, database_name: str, version: int) -> set[str]:
        """Engines with a ``schema-<engine>/v<version>/`` directory."""
        prefix = f"{database_name}/{job_id}/"
        engines: set[str] = set()
        for key in self.store.list_prefix(prefix):
            parts = str(key).replace(prefix, "").split("/")
            if len(parts) >= 3 and parts[0].startswith("schema-") and parts[1] == f"v{version}":
                engines.add(parts[0][len("schema-") :])
        return engines

    def _reroute_and_redesign(
        self,
        job_id: str,
        database_name: str,
        assignment: dict,
        version: int,
        routings: list,
    ) -> int:
        """Write the moves as version N+1 and redesign every engine they touch.

        Uses the customer-edit staleness rule (``assignment_engine_diff``): an
        engine whose in-scope queries changed -- the one a query left and the
        one it joined -- is designed again on N+1 (its full design, so the
        engine a query left no longer lists it, #713 review round 2); an engine
        the moves left alone has its design copied forward. When a redesign
        fails, a rollback version N+2 restates version N (with its designs)
        and the error is raised, so the effective version never pairs the
        moves with a pre-move design. Returns the new effective version.
        """
        from datetime import UTC, datetime

        from src.agents.schema_design.handler import run_schema_design_auto
        from src.contracts.assignment_models import AssignmentStatus
        from src.storage.assignment_versioning import (
            assignment_artifact_path,
            assignment_engine_diff,
            next_assignment_version,
        )

        new_version = max(next_assignment_version(self.store, database_name, job_id), version + 1)
        revised = self._rerouted_assignment(
            job_id, database_name, assignment, version, new_version, routings
        )
        self.store.write_json(assignment_artifact_path(database_name, job_id, new_version), revised)
        diff = assignment_engine_diff(self.store, database_name, job_id, version, new_version)
        designed = self._schema_engines(job_id, database_name, version)
        print(
            f"[router] Assignment v{version} -> v{new_version}: "
            f"{len([r for r in routings if r.to_engine])} rerouted queries; "
            f"redesigning {', '.join(diff['affected']) or 'nothing'}"
        )
        try:
            for engine in diff["unaffected"]:
                if engine in designed:
                    self._copy_schema_artifacts(job_id, database_name, engine, version, new_version)
            for engine in diff["affected"]:
                run_schema_design_auto(
                    job_id,
                    database_name,
                    engine,
                    self.store,
                    assignment_version=new_version,
                    llm_mode=self.llm_mode,
                )
        except Exception:
            rollback = new_version + 1
            restated = {
                **assignment,
                "version": rollback,
                "previous_version": new_version,
                "source": revised["source"],
                # Never the approval it restates, and dated now (#713 review r3).
                "status": AssignmentStatus.REROUTE_ROLLED_BACK.value,
                "timestamp": datetime.now(UTC).isoformat(),
                "rolled_back_version": new_version,
                # #616: per-version moves; not the restated version's.
                "moves": [],
            }
            self.store.write_json(
                assignment_artifact_path(database_name, job_id, rollback), restated
            )
            for engine in designed:
                self._copy_schema_artifacts(job_id, database_name, engine, version, rollback)
            logger.error(
                "[router] Redesign on v%d failed; v%d restates v%d with its designs",
                new_version,
                rollback,
                version,
            )
            raise
        return new_version

    def _router_scoring_inputs(
        self,
        job_id: str,
        database_name: str,
        engines: list[str],
        assignment_version: int,
    ) -> dict:
        """The shared scorer's inputs for the post-schema router (#629).

        ``ScoringContext.from_pipeline`` over the triage, the engines' analyses
        and the collector, plus the query map, triage signals, source engine
        and the assignment's settled workload (for the shared tie-break).
        """
        from src.agents.referee.aurora_choice import source_database_engine
        from src.agents.referee.query_scoring import ScoringContext
        from src.agents.referee.reality_check_rules import _build_query_signals
        from src.agents.referee.tie_break import settled_counts
        from src.storage.assignment_versioning import assignment_artifact_path

        prefix = f"{database_name}/{job_id}"

        def _read(key: str) -> dict:
            return self.store.read_json(key) if self.store.exists(key) else {}

        collector = _read(f"{prefix}/collector/output.json")
        triage = _read(f"{prefix}/referee-triage/triage.json")
        analysis_outputs = {
            e: data for e in engines if (data := _read(f"{prefix}/analysis-{e}/analysis.json"))
        }
        assignment = (
            _read(assignment_artifact_path(database_name, job_id, assignment_version))
            if assignment_version
            else {}
        )
        return {
            "scoring_context": ScoringContext.from_pipeline(
                analysis_outputs, triage or None, collector or None
            ),
            "query_map": {
                q["query_id"]: q for q in collector.get("queries", {}).get("query_patterns", [])
            },
            "query_signals": _build_query_signals(triage.get("signals", [])),
            "source_engine": source_database_engine(collector),
            "settled": settled_counts(assignment.get("query_assignments", [])),
        }

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
