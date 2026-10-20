"""Local orchestrator — filesystem-backed, no cloud dependencies.

Enforces the same phase ordering as the Step Functions state machine.
Fan-out phases (Analysis, Schema Design) use ProcessPoolExecutor.
Progression stored as JSON in ``{artifact_dir}/.meta/{job_id}.json``.

Requirements: 9.1, 9.2, 9.3, 9.4, 9.5, 13.1, 13.2, 13.3
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from src.agents.referee.query_pattern import write_access_patterns
from src.agents.referee.write_groups import assignment_placement_groups, assignment_unit_weights
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
from src.shared.contract_versioning import load_contract, stamp_artifact_version
from src.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)

#: The post-schema router's cascade bound: passes 0..2, the last one only
#: marks what is still flagged as terminal (both modes, #1059).
POST_SCHEMA_MAX_DEPTH = 2

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

        Headless runs take the same passes one at a time
        (:meth:`run_post_schema_routing_external`, #1059): the passes, the
        reroute version and the rollback are the same methods; only the
        redesign between two passes is done by the caller's design subagents.
        """
        run = self._post_schema_routing_start(job_id, database_name)
        scoring = self._router_scoring_inputs(
            job_id, database_name, run["selected_engines"], run["start_version"]
        )
        for depth in range(POST_SCHEMA_MAX_DEPTH + 1):
            print(f"\n[router] === Routing pass {depth} ===")
            routed = self._post_schema_routing_pass(job_id, database_name, run, depth, scoring)
            if routed is None:
                return
            router_output, assignment = routed
            # The moves become a new assignment version, and every engine the
            # moves touch (the one a query left and the one it joined) is
            # designed again on it (maintainer decision, 2026-10-14), so the
            # design and the assignment agree.
            run["assignment_version"] = self._reroute_and_redesign(
                job_id,
                database_name,
                assignment,
                run["assignment_version"],
                router_output.routings,
            )

            print(f"[router] Cascade pass {depth} complete — checking for new unsupported...")

        print(f"[router] Max cascade depth ({POST_SCHEMA_MAX_DEPTH}) reached — stopping")

    def _post_schema_routing_start(self, job_id: str, database_name: str) -> dict:
        """The router's run state before its first pass (JSON-safe, so a headless
        run can keep it between passes, #1059)."""
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
        return {
            "selected_engines": list(selected_engines),
            # The shared scorer's inputs (#629) are read on this version: the
            # router re-routes a flagged query to its best remaining engine by
            # the same per-query score the assignment used, never by an engine
            # list or a name in the flag.
            "start_version": assignment_version,
            "assignment_version": assignment_version,
            "kept": sorted(kept),
            "already_routed": [],
            "routings": [],
            "terminal_queries": [],
            "held_queries": [],
            "terminal": [],
            "held": [],
        }

    def _post_schema_routing_pass(
        self, job_id: str, database_name: str, run: dict, depth: int, scoring: dict
    ) -> tuple | None:
        """One router pass on ``run["assignment_version"]``.

        Writes ``post-schema-router/pass-<depth>/router_output.json`` and the
        cumulative ``router_output.json``, and updates ``run`` (``kept``, the
        accumulated routings and terminal queries, ``already_routed``). Returns
        ``(router_output, assignment)`` when the pass moved queries, else
        ``None`` (no schema output, or nothing to move).
        """
        from collections import Counter

        from src.agents.referee.post_schema_router import (
            route_unsupported_queries,
            routing_flags_from_trace,
        )
        from src.agents.referee.tie_break import settled_counts
        from src.agents.referee.waived_counts import pinned_counts_by_table
        from src.contracts.post_schema_router_output import HeldQuery, QueryRouting, TerminalQuery

        selected_engines = run["selected_engines"]
        assignment_version = run["assignment_version"]
        assignment = self._read_assignment(job_id, database_name, assignment_version)
        kept = set(run["kept"]) & self._get_engines_with_in_scope_queries(
            job_id, database_name, assignment_version
        )
        run["kept"] = sorted(kept)
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
            return None

        already_routed = set(run["already_routed"])
        # Run router
        router_output = route_unsupported_queries(
            schema_outputs=schema_outputs,
            active_engines=active_engines,
            routing_flags_by_engine=flags_by_engine,
            cascade_depth=depth,
            max_depth=POST_SCHEMA_MAX_DEPTH,
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
            # Co-dependency and write groups (ADR-031 Decision 4 and Amendment 9):
            # a reroute moves a table's writes together.
            co_dependency_groups=assignment_placement_groups(assignment),
            # A pinned write holds its table's other writes in place (#992).
            write_groups=assignment.get("write_groups") or {},
            # A write unit is judged on its call-rate-weighted mean (#992).
            unit_weights=assignment_unit_weights(assignment, scoring.get("query_map") or {}),
            # A pinned key-scoped count holds its table's writes too (#1151).
            pinned_counts=pinned_counts_by_table(
                assignment.get("query_assignments", []),
                scoring.get("query_map") or {},
                scoring["scoring_context"],
            ),
            # A tied query is re-routed among its tied engines first and
            # keeps the rest of its tie (ADR-031 Decision 12, #811).
            tied_engines={
                qa["query_id"]: list(qa["tied_engines"])
                for qa in assignment.get("query_assignments", [])
                if qa.get("tied_engines")
            },
            **scoring,
        )
        router_output.job_id = job_id

        # Every pass is kept (#713 review): router_output.json accumulates
        # every pass's routings and terminal queries, and each pass is also
        # written on its own, so no pass's scores are lost.
        all_routings = [QueryRouting.model_validate(r) for r in run["routings"]]
        all_routings.extend(router_output.routings)
        all_terminal = list(run["terminal_queries"])
        all_terminal.extend(q for q in router_output.terminal_queries if q not in all_terminal)
        all_held = list(run.get("held_queries") or [])
        all_held.extend(q for q in router_output.held_queries if q not in all_held)
        # #888: each terminal query once, with the flag that made it terminal.
        all_terminal_records = [TerminalQuery.model_validate(t) for t in run.get("terminal") or []]
        recorded = {t.query_id for t in all_terminal_records}
        all_terminal_records.extend(t for t in router_output.terminal if t.query_id not in recorded)
        # #1033: each held query once, with the flag it was held under.
        all_held_records = [HeldQuery.model_validate(h) for h in run.get("held") or []]
        held_recorded = {h.query_id for h in all_held_records}
        all_held_records.extend(h for h in router_output.held if h.query_id not in held_recorded)
        run["held"] = [h.model_dump(mode="json") for h in all_held_records]
        run["routings"] = [r.model_dump(mode="json") for r in all_routings]
        run["terminal_queries"] = all_terminal
        run["held_queries"] = all_held
        run["terminal"] = [t.model_dump(mode="json") for t in all_terminal_records]
        prefix_router = f"{database_name}/{job_id}/post-schema-router"
        self.store.write_json(
            f"{prefix_router}/pass-{depth}/router_output.json",
            router_output.model_dump(mode="json"),
        )
        cumulative = router_output.model_copy(
            update={
                "routings": list(all_routings),
                "terminal_queries": list(all_terminal),
                "held_queries": list(all_held),
                "terminal": list(all_terminal_records),
                "held": list(all_held_records),
            }
        )
        self.store.write_json(
            f"{prefix_router}/router_output.json", cumulative.model_dump(mode="json")
        )

        if router_output.held_queries:
            print(
                f"[router] {len(router_output.held_queries)} flagged queries held in place: "
                "their table's writes include a write the customer pinned"
            )
        if not router_output.routings:
            if router_output.terminal_queries:
                print(
                    f"[router] {len(router_output.terminal_queries)} terminal queries "
                    f"(application-layer): {router_output.terminal_queries[:5]}"
                )
            else:
                print("[router] No unsupported queries — all queries have a home")
            return None

        # Log what's being routed
        by_target = Counter(r.to_engine for r in router_output.routings if r.to_engine)
        print(f"[router] Routing {len(router_output.routings)} queries:")
        for engine, count in by_target.most_common():
            reasons = {r.reason[:50] for r in router_output.routings if r.to_engine == engine}
            print(f"[router]   {count} → {engine} ({', '.join(reasons)})")
        if router_output.terminal_queries:
            print(f"[router]   {len(router_output.terminal_queries)} → application-layer")
        already_routed.update(r.query_id for r in router_output.routings if r.to_engine)
        run["already_routed"] = sorted(already_routed)
        return router_output, assignment

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

        Each moved query is a ``post_schema_flag`` Move (#616, #489 Track L
        PR4; ``move_ledger.apply_to_assignment``, rule ``flag.capability_gap``,
        actor ``model_flag``) with the router's scores, runner-up and evidence;
        a key-scoped count whose waiver those moves end (its table's writes left the
        engine it is on, #1151) is re-placed after them, a ``post_schema_flag`` rule
        Move with its own ``post_schema_reroutes`` entry;
        the version holds exactly those Moves and is refused when its engines
        and ties do not replay from ``version`` plus them.
        """
        from copy import deepcopy
        from datetime import UTC, datetime

        from src.agents.referee.assignment_overrides import refresh_consolidated_assignment
        from src.agents.referee.move_ledger import (
            apply_to_assignment,
            check_replay,
            ledger_for,
            state_of,
        )
        from src.contracts.assignment_models import AssignmentSource, AssignmentStatus
        from src.contracts.move_ledger import MoveActor, MoveEvidence, MovePass, ReasonCode

        assignment = deepcopy(assignment)  # the refresh below edits records in place
        prior = list(assignment.get("post_schema_reroutes") or [])
        approved_version = next(
            (r["approved_version"] for r in prior if r.get("approved_version")), version
        )
        moved = {r.query_id: r for r in routings if r.to_engine}
        # Copies: the input records stay as they were, so ``before`` below (the
        # engines the input version uses) still sees an engine the moves empty.
        query_assignments = [dict(qa) for qa in assignment.get("query_assignments", [])]
        start = state_of(query_assignments)
        ledger = ledger_for(query_assignments)
        for qa in query_assignments:
            routing = moved.get(qa.get("query_id"))
            if routing is None:
                continue
            apply_to_assignment(
                ledger,
                qa,
                routing.to_engine,
                pass_=MovePass.POST_SCHEMA_FLAG,
                reason_code=ReasonCode.FLAG_CAPABILITY_GAP,
                score_before=routing.score_before,
                score_after=routing.score_after,
                runner_up=routing.runner_up,
                evidence=routing.evidence or MoveEvidence(),
                group_key=routing.group_key,
                # The tie the router left: the query's tie minus the flagged
                # engine while it stays unresolved, else none (ADR-031
                # Decision 12, #811); the reality check's model-flag rule.
                tied_engines=list(routing.tied_engines),
                actor=MoveActor.MODEL_FLAG,
            )
            qa["assignment_reason"] = f"post-schema routing: {routing.reason}"
        prefix = f"{database_name}/{job_id}"
        collector_key = f"{prefix}/collector/output.json"
        collector = self.store.read_json(collector_key) if self.store.exists(collector_key) else {}
        before = {
            qa["assigned_engine"]
            for qa in assignment.get("query_assignments", [])
            if qa.get("in_scope", True)
        }
        after = {qa["assigned_engine"] for qa in query_assignments if qa.get("in_scope", True)}
        analysis_outputs = {
            e: self.store.read_json(f"{prefix}/analysis-{e}/analysis.json")
            for e in before | after
            if self.store.exists(f"{prefix}/analysis-{e}/analysis.json")
        }
        # #1151 (ADR-031 Decision 2 Amendment 12): a flagged write the router moved can
        # take its table's source of truth off the engine a key-scoped count's waiver
        # needs; each such count is re-placed by the shared scorer, a rule Move, and
        # recorded with the reroutes.
        count_warnings: list[str] = []
        count_reroutes = self._replace_lapsed_counts(
            prefix,
            assignment,
            query_assignments,
            ledger,
            collector,
            analysis_outputs,
            count_warnings,
        )
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
        reroutes += [{**r, "approved_version": approved_version} for r in count_reroutes]
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
            # #616: moves are per-version -- exactly the Moves above.
            "moves": [m.model_dump(mode="json") for m in ledger.moves],
        }
        # The derived views (table mappings, warnings) follow the moved queries,
        # the same refresh the reality check's revision gets (ADR-029).
        after = {qa["assigned_engine"] for qa in query_assignments if qa.get("in_scope", True)}
        refreshed = refresh_consolidated_assignment(
            revised, collector, analysis_outputs, dead_engines=before - after
        )
        # Refuse a version whose engines do not follow from ``version`` plus
        # its own Moves (an engine set without move_ledger, #616).
        check_replay(
            ledger.moves,
            start,
            state_of(refreshed.get("query_assignments", [])),
            "post-schema reroute",
        )
        if count_warnings:
            refreshed["validation_warnings"] = [
                *(refreshed.get("validation_warnings") or []),
                *count_warnings,
            ]
        return refreshed

    def _replace_lapsed_counts(
        self,
        prefix: str,
        assignment: dict,
        query_assignments: list[dict],
        ledger,
        collector: dict,
        analysis_outputs: dict[str, dict],
        warnings: list[str],
    ) -> list[dict]:
        """Re-place the key-scoped counts the router's moves left without their waiver
        (#1151, ``waived_counts.replace_lapsed_counts``) on ``query_assignments``, as
        ``post_schema_flag`` rule Moves on ``ledger``; their ``post_schema_reroutes``
        entries (reason category ``source_of_truth``), without ``approved_version``.
        A pinned count is held, never moved, and a count it could not re-place is said
        in ``warnings`` (the version's ``validation_warnings``)."""
        from src.agents.referee.assignment_overrides import SOURCE_OF_TRUTH_REROUTE_CATEGORY
        from src.agents.referee.aurora_choice import source_database_engine
        from src.agents.referee.query_scoring import ScoringContext
        from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA
        from src.agents.referee.waived_counts import replace_lapsed_counts
        from src.agents.referee.write_groups import assignment_sources_of_truth
        from src.contracts.move_ledger import MovePass

        write_groups = assignment.get("write_groups") or {}
        if not write_groups:
            return []
        triage_key = f"{prefix}/referee-triage/triage.json"
        triage = self.store.read_json(triage_key) if self.store.exists(triage_key) else {}
        ctx = ScoringContext.from_pipeline(analysis_outputs, triage or None, collector or None)
        replaced = replace_lapsed_counts(
            query_assignments,
            ledger,
            query_by_id={
                q["query_id"]: q for q in collector.get("queries", {}).get("query_patterns", [])
            },
            ctx=ctx,
            write_groups=write_groups,
            before=assignment_sources_of_truth(assignment),
            pass_=MovePass.POST_SCHEMA_FLAG,
            source_compatible=SOURCE_ENGINE_TO_AURORA.get(source_database_engine(collector)),
            # A rule never moves a customer's pin (ADR-028); the router held the writes
            # of a pinned count's table, so this only guards it.
            pinned="hold",
        )
        warnings.extend(replaced.warnings)
        return [
            {
                "query_id": m.query_id,
                "from_engine": m.from_engine,
                "to_engine": m.to_engine,
                "reason": m.reason,
                "reason_category": SOURCE_OF_TRUTH_REROUTE_CATEGORY,
                "score_before": None,
                "score_after": m.score_after,
            }
            for m in replaced.moved
        ]

    def _copy_schema_artifacts(
        self, job_id: str, database_name: str, engine: str, src: int, dst: int
    ) -> None:
        """Copy ``schema-<engine>/v<src>/*`` to ``v<dst>`` (an engine the moves left alone).

        The design goes through the output-contract gate first (#1186): a saved
        design that fails it raises :class:`SchemaContractError` before any file
        of the engine is copied.
        """
        from src.agents.schema_design.output_contract import write_schema_output

        old_prefix = f"{database_name}/{job_id}/schema-{engine}/v{src}/"
        new_prefix = f"{database_name}/{job_id}/schema-{engine}/v{dst}/"
        keys = list(self.store.list_prefix(old_prefix))
        design = old_prefix + "schema_output.json"
        if design in keys:
            write_schema_output(
                self.store, new_prefix + "schema_output.json", self.store.read_json(design), engine
            )
        for key in keys:
            if key != design:
                self.store.write_json(
                    new_prefix + key[len(old_prefix) :], self.store.read_json(key)
                )

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
        from src.agents.schema_design.handler import run_schema_design_auto

        new_version, revised, diff, designed = self._write_post_schema_reroute(
            job_id, database_name, assignment, version, routings
        )
        try:
            self._carry_forward_designs(job_id, database_name, diff, designed, version, new_version)
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
            self._roll_back_post_schema_reroute(
                job_id, database_name, assignment, version, revised, designed
            )
            raise
        return new_version

    def _write_post_schema_reroute(
        self,
        job_id: str,
        database_name: str,
        assignment: dict,
        version: int,
        routings: list,
    ) -> tuple[int, dict, dict[str, list[str]], set[str]]:
        """Write the router's moves as version N+1 (with its access patterns).

        Returns ``(new_version, revised, diff, designed)``: the revised
        assignment, ``assignment_engine_diff`` of N against N+1 (``affected``
        engines are designed again, ``unaffected`` ones carried forward) and the
        engines with a design on N.
        """
        from src.storage.assignment_versioning import (
            assignment_artifact_path,
            assignment_engine_diff,
            next_assignment_version,
        )

        new_version = max(next_assignment_version(self.store, database_name, job_id), version + 1)
        revised = self._rerouted_assignment(
            job_id, database_name, assignment, version, new_version, routings
        )
        # #972, #1018: written now, so today's version, not the spread one's.
        revised_key = assignment_artifact_path(database_name, job_id, new_version)
        self.store.write_json(revised_key, stamp_artifact_version(revised_key, revised))
        write_access_patterns(self.store, database_name, job_id, new_version)
        diff = assignment_engine_diff(self.store, database_name, job_id, version, new_version)
        designed = self._schema_engines(job_id, database_name, version)
        print(
            f"[router] Assignment v{version} -> v{new_version}: "
            f"{len([r for r in routings if r.to_engine])} rerouted queries; "
            f"redesigning {', '.join(diff['affected']) or 'nothing'}"
        )
        return new_version, revised, diff, designed

    def _carry_forward_designs(
        self,
        job_id: str,
        database_name: str,
        diff: dict[str, list[str]],
        designed: set[str],
        version: int,
        new_version: int,
    ) -> None:
        """Copy the designs of the engines the moves left alone to ``new_version``."""
        for engine in diff["unaffected"]:
            if engine in designed:
                self._copy_schema_artifacts(job_id, database_name, engine, version, new_version)

    def _roll_back_post_schema_reroute(
        self,
        job_id: str,
        database_name: str,
        assignment: dict,
        version: int,
        revised: dict,
        designed: set[str],
    ) -> int:
        """Write version N+2 restating ``assignment`` (N, with its designs) after
        a failed redesign of ``revised`` (N+1). Returns the rollback version."""
        from datetime import UTC, datetime

        from src.contracts.assignment_models import AssignmentStatus
        from src.storage.assignment_versioning import assignment_artifact_path

        new_version = int(revised["version"])
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
            # #616, #770: per-version moves -- the reverse Moves from the
            # abandoned reroute back to the version it restates.
            "moves": self._rollback_moves(revised, assignment),
        }
        rollback_key = assignment_artifact_path(database_name, job_id, rollback)
        self.store.write_json(rollback_key, stamp_artifact_version(rollback_key, restated))
        write_access_patterns(self.store, database_name, job_id, rollback)
        from src.agents.schema_design.output_contract import SchemaContractError

        for engine in designed:
            try:
                self._copy_schema_artifacts(job_id, database_name, engine, version, rollback)
            except SchemaContractError as exc:
                # The restated design fails its contract (#1186): it is not
                # copied, so v<rollback> has no design for the engine.
                logger.error(
                    "[router] v%d %s design fails its contract, not restated on v%d: %s",
                    version,
                    engine,
                    rollback,
                    "; ".join(exc.errors),
                )
        logger.error(
            "[router] Redesign on v%d failed; v%d restates v%d with its designs",
            new_version,
            rollback,
            version,
        )
        return rollback

    # ------------------------------------------------------------------
    # Post-schema routing, one pass at a time (headless runs, #1059)
    # ------------------------------------------------------------------

    def _external_routing_key(self, job_id: str, database_name: str) -> str:
        return f"{database_name}/{job_id}/post-schema-router/external_run.json"

    def run_post_schema_routing_external(self, job_id: str, database_name: str) -> dict:
        """One step of the post-schema routing for a headless run; returns its status.

        The same passes, reroute version and cascade bound as
        :meth:`_run_post_schema_routing` (ADR-031 Decision 11 Amendment 2: a
        flag is evidence, the shared scorer picks among the kept engines), with
        the redesign between two passes left to the caller: Claude Code's design
        subagents, which design each engine in ``redesign_engines`` on the new
        version (and review it) before the caller runs this again. The run
        state is kept in ``post-schema-router/external_run.json``.

        Status ``awaiting_redesign``: a pass moved queries and wrote
        ``assignment_version``; ``missing_designs`` lists the engines in
        ``redesign_engines`` with no design on it yet and ``missing_reviews``
        the redesigned designs with no PE review recorded (no pass runs while
        either lists one). ``complete``: no pass has anything left to move; calling
        again re-runs that last pass (the same result unless a design or
        review changed since, and never past the cascade bound).
        ``rolled_back``: :meth:`roll_back_post_schema_routing_external` ran.
        """
        key = self._external_routing_key(job_id, database_name)
        effective = self._get_assignment_version(job_id, database_name)
        run = self.store.read_json(key) if self.store.exists(key) else None
        if run is None or run.get("assignment_version") != effective:
            # A fresh run: none yet, or the assignment moved on since (a customer edit).
            run = {**self._post_schema_routing_start(job_id, database_name), "next_depth": 0}
            run["status"] = "routing"
        if run["status"] == "rolled_back":
            return self._external_routing_status(run)
        if run["status"] == "awaiting_redesign":
            missing = self._missing_designs(job_id, database_name, run)
            unreviewed = self._missing_reviews(job_id, database_name, run)
            if missing or unreviewed:
                return self._external_routing_status(run, missing, unreviewed)

        depth = int(run["next_depth"])
        routed = None
        if depth <= POST_SCHEMA_MAX_DEPTH:
            print(f"\n[router] === Routing pass {depth} ===")
            scoring = self._router_scoring_inputs(
                job_id, database_name, run["selected_engines"], run["start_version"]
            )
            routed = self._post_schema_routing_pass(job_id, database_name, run, depth, scoring)
        else:
            print(f"[router] Max cascade depth ({POST_SCHEMA_MAX_DEPTH}) reached — stopping")
        if routed is None:
            run["status"] = "complete"
            self.store.write_json(key, run)
            return self._external_routing_status(run)

        router_output, assignment = routed
        version = run["assignment_version"]
        new_version, revised, diff, designed = self._write_post_schema_reroute(
            job_id, database_name, assignment, version, router_output.routings
        )
        try:
            self._carry_forward_designs(job_id, database_name, diff, designed, version, new_version)
        except Exception:
            rollback = self._roll_back_post_schema_reroute(
                job_id, database_name, assignment, version, revised, designed
            )
            # Saved, so a retry finds the rollback, not a fresh run on it.
            run.update(
                status="rolled_back",
                previous_version=version,
                rolled_back_version=new_version,
                assignment_version=rollback,
            )
            self.store.write_json(key, run)
            raise
        run.update(
            status="awaiting_redesign",
            previous_version=version,
            assignment_version=new_version,
            next_depth=depth + 1,
            redesign_engines=list(diff["affected"]),
            pass_rerouted=len([r for r in router_output.routings if r.to_engine]),
        )
        self.store.write_json(key, run)
        return self._external_routing_status(
            run,
            self._missing_designs(job_id, database_name, run),
            self._missing_reviews(job_id, database_name, run),
        )

    def roll_back_post_schema_routing_external(self, job_id: str, database_name: str) -> dict:
        """After a failed redesign: the rollback version the Bedrock path writes (#1059).

        Restates the version the last reroute started from, with its designs,
        exactly as :meth:`_reroute_and_redesign` does when a redesign raises.
        """
        from src.storage.assignment_versioning import assignment_artifact_path

        key = self._external_routing_key(job_id, database_name)
        run = self.store.read_json(key) if self.store.exists(key) else None
        if not run or run.get("status") != "awaiting_redesign":
            raise RuntimeError("no post-schema reroute is waiting for its redesign")
        version, new_version = int(run["previous_version"]), int(run["assignment_version"])
        if self._get_assignment_version(job_id, database_name) != new_version:
            raise RuntimeError(f"the effective assignment is no longer v{new_version}")
        assignment = self._read_assignment(job_id, database_name, version)
        revised = self.store.read_json(assignment_artifact_path(database_name, job_id, new_version))
        designed = self._schema_engines(job_id, database_name, version)
        rollback = self._roll_back_post_schema_reroute(
            job_id, database_name, assignment, version, revised, designed
        )
        run.update(
            status="rolled_back", rolled_back_version=new_version, assignment_version=rollback
        )
        self.store.write_json(key, run)
        return self._external_routing_status(run)

    def _missing_designs(self, job_id: str, database_name: str, run: dict) -> list[str]:
        version = run["assignment_version"]
        return [
            engine
            for engine in run.get("redesign_engines") or []
            if not self.store.exists(
                f"{database_name}/{job_id}/schema-{engine}/v{version}/schema_output.json"
            )
        ]

    def _missing_reviews(self, job_id: str, database_name: str, run: dict) -> list[str]:
        """The redesigned designs with no PE review recorded yet (engines whose
        review can flag; one entry per DynamoDB group), so no pass reads a
        redesign before its review, as on the Bedrock path."""
        from src.agents.schema_design.pe_review_external import missing_reviews

        return missing_reviews(
            self.store,
            database_name,
            job_id,
            run["assignment_version"],
            run.get("redesign_engines") or [],
        )

    @staticmethod
    def _external_routing_status(
        run: dict, missing: list[str] | None = None, unreviewed: list[str] | None = None
    ) -> dict:
        """The one status line a headless caller reads (compact: counts, not ids)."""
        status = {
            "status": run["status"],
            "assignment_version": run["assignment_version"],
            "rerouted": len([r for r in run["routings"] if r.get("to_engine")]),
            "terminal_queries": len(run["terminal_queries"]),
            "held_queries": len(run.get("held_queries") or []),
        }
        if run["status"] == "awaiting_redesign":
            status.update(
                {
                    "pass": run["next_depth"] - 1,
                    "previous_version": run["previous_version"],
                    "pass_rerouted": run.get("pass_rerouted", 0),
                    "redesign_engines": list(run.get("redesign_engines") or []),
                    "missing_designs": list(missing or []),
                    "missing_reviews": list(unreviewed or []),
                }
            )
        elif run["status"] == "rolled_back":
            status.update(
                {
                    "restates": run["previous_version"],
                    "rolled_back_version": run["rolled_back_version"],
                }
            )
        return status

    @staticmethod
    def _rollback_moves(rerouted: dict, restated: dict) -> list[dict]:
        """The Moves of a rollback version: ``rerouted`` undone, back to ``restated`` (#770).

        Built from the abandoned reroute version's own Moves, never from a diff
        of the two versions: each of them, in reverse ``seq`` order, gets one
        ``post_schema_flag`` Move (rule ``rollback.redesign_failed``, actor
        ``rule``) back to the engine and tie the query had before it, with the
        same ``group_key``. It leaves the reroute's destination (that Move's
        ``score_after``) and lands back on the flagged engine, which the flag
        still gates: ``score_after`` is ``None`` and the gate is in
        ``evidence.candidates`` (the undone Move's ladder). A rollback is no
        choice among engines, so it has no runner-up.

        ``check_replay`` then compares ``rerouted`` plus these Moves with
        ``restated`` (the version the reroute was derived from): a query the
        reroute never moved that differs between the two is refused, not
        silently given a Move.

        The tie each reverse Move restores is the one the undone Move found
        (#811). A Move records it in ``previous_tied_engines`` only when it
        changed the tie, so an empty list there means either "the tie was
        empty" or "the tie did not change". The rollback tells them apart from
        the state the reroute started from (``restated``, the reroute's input),
        stepped through the reroute's Moves in order, and refuses a Move whose
        record disagrees with that state.
        """
        from src.agents.referee.move_ledger import apply, check_replay, ledger_for, state_of
        from src.contracts.move_ledger import Move, MoveEvidence, MovePass, ReasonCode

        rerouted_qas = rerouted.get("query_assignments", [])
        ledger = ledger_for(rerouted_qas)
        done = sorted(
            (Move.model_validate(m) for m in rerouted.get("moves") or []),
            key=lambda m: m.seq,
        )
        # The tie each Move found: the reroute's input, stepped through its Moves.
        tie_now = {
            qid: sorted(tie)
            for qid, (_, tie) in state_of(restated.get("query_assignments", [])).items()
        }
        tie_before: dict[int, list[str]] = {}
        for move in done:
            before = tie_now.get(move.query_id, [])
            recorded = sorted(move.evidence.previous_tied_engines)
            if (recorded or before != move.tied_engines) and recorded != before:
                raise RuntimeError(
                    f"post-schema rollback: the Move of {move.query_id!r} records the tie "
                    f"{recorded} before it, but the reroute's input has {before}"
                )
            tie_before[move.seq] = before
            tie_now[move.query_id] = list(move.tied_engines)
        for move in reversed(done):
            if move.from_engine is None:
                raise RuntimeError(
                    f"post-schema rollback: {move.query_id!r} has no engine to go back to"
                )
            restored_tie = tie_before[move.seq]
            current_tie = ledger.tied_of(move.query_id)
            apply(
                ledger,
                move.query_id,
                move.from_engine,
                pass_=MovePass.POST_SCHEMA_FLAG,
                reason_code=ReasonCode.ROLLBACK_REDESIGN_FAILED,
                score_before=move.score_after,
                score_after=None,
                evidence=MoveEvidence(
                    candidates=move.evidence.candidates,
                    previous_tied_engines=current_tie if current_tie != restored_tie else [],
                ),
                group_key=move.group_key,
                tied_engines=restored_tie,
            )
        check_replay(
            ledger.moves,
            state_of(rerouted_qas),
            state_of(restated.get("query_assignments", [])),
            "post-schema rollback",
        )
        return [m.model_dump(mode="json") for m in ledger.moves]

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
        from src.agents.referee.write_groups import assignment_sources_of_truth
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
            # The capability waivers (#377) read the approved version's sources of truth.
            "scoring_context": ScoringContext.from_pipeline(
                analysis_outputs, triage or None, collector or None
            ).with_sources_of_truth(assignment_sources_of_truth(assignment)),
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
