"""The post-schema routing: move what a schema design cannot serve, then redesign.

One implementation for every path (#1492): the local pipeline
(``LocalOrchestrator``, the CLI) and the library ``assess()`` reach it through
``src.atx_orchestrator.core.run_post_schema_routing_core`` (AWS Transform is to,
#1498), the headless one-pass-at-a-time run through
:meth:`PostSchemaRouting.run_external_step`. Only the redesign differs between
them: each caller passes the designer it runs schema design with.

After the engines are designed, it reads each design's unsupported patterns and
the PE reviewers' routing flags (design traces), routes each flagged query (with
its placement unit) to its best remaining engine by the shared scorer
(``post_schema_router.route_unsupported_queries``, #629), writes the moves as a
new assignment version through the move ledger, and redesigns the engines the
moves touch on it (ADR-031 Decision 11 Amendment 2).
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from src.agents.referee.engine_scope import check_in_scope, scope_from_store
from src.agents.referee.query_pattern import write_access_patterns
from src.agents.referee.write_groups import assignment_placement_groups, assignment_unit_weights
from src.shared.contract_versioning import stamp_artifact_version
from src.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)

#: The post-schema router's cascade bound: passes 0..2, the last one only
#: marks what is still flagged as terminal (both modes, #1059).
POST_SCHEMA_MAX_DEPTH = 2

#: ``redesign(job_id, database_name, engine, assignment_version)``: design one
#: engine on the reroute's version (its full design), raising when it fails.
Redesign = Callable[[str, str, str, int], None]


def selected_engines(store: ArtifactStore, database_name: str, job_id: str) -> list[str]:
    """The engines triage selected (inside the customer's engine scope)."""
    triage_key = f"{database_name}/{job_id}/referee-triage/triage.json"
    if not store.exists(triage_key):
        return []
    triage = store.read_json(triage_key)
    return [a["agent_type"] for a in triage.get("selected_agents", [])]


def drop_losing_aurora_engine(
    store: ArtifactStore,
    database_name: str,
    job_id: str,
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
    if not store.exists(path):
        return engines
    choice = store.read_json(path).get("aurora_engine_choice")
    winner = choice.get("engine") if isinstance(choice, dict) else None
    if winner not in AURORA_ENGINES:
        return engines
    loser = next(e for e in AURORA_ENGINES if e != winner)
    return [e for e in engines if e != loser]


def pipeline_redesign(store: ArtifactStore, llm_mode: str) -> Redesign:
    """The local pipeline's redesign: ``run_schema_design_auto`` in ``llm_mode``."""

    def redesign(job_id: str, database_name: str, engine: str, assignment_version: int) -> None:
        from src.agents.schema_design.handler import run_schema_design_auto

        run_schema_design_auto(
            job_id,
            database_name,
            engine,
            store,
            assignment_version=assignment_version,
            llm_mode=llm_mode,
        )

    return redesign


class PostSchemaRouting:
    """The post-schema routing of one store, redesigning with ``redesign``."""

    def __init__(self, store: ArtifactStore, redesign: Redesign) -> None:
        self.store = store
        self.redesign = redesign

    def _selected_engines(self, job_id: str, database_name: str) -> list[str]:
        return selected_engines(self.store, database_name, job_id)

    def _effective_version(self, job_id: str, database_name: str) -> int:
        """The latest assignment version (0 when none). ADR-028 shared resolver."""
        from src.storage.assignment_versioning import resolve_effective_assignment_version

        return resolve_effective_assignment_version(self.store, database_name, job_id)

    def _engines_with_in_scope_queries(
        self, job_id: str, database_name: str, assignment_version: int
    ) -> set[str]:
        """The engines with at least one in-scope query on ``assignment_version``."""
        if assignment_version == 0:
            return set(self._selected_engines(job_id, database_name))
        from src.storage.assignment_versioning import engines_with_in_scope_queries

        return engines_with_in_scope_queries(self.store, database_name, job_id, assignment_version)

    def _drop_losing_aurora_engine(
        self, job_id: str, database_name: str, engines: list[str], assignment_version: int
    ) -> list[str]:
        return drop_losing_aurora_engine(
            self.store, database_name, job_id, engines, assignment_version
        )

    def run(self, job_id: str, database_name: str) -> dict:
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
        (:meth:`run_external_step`, #1059): the passes, the
        reroute version and the rollback are the same methods; only the
        redesign between two passes is done by the caller's design subagents.

        Returns the run state: ``assignment_version`` is the effective version it
        leaves (the start version when nothing moved), with every pass's
        ``routings``, ``terminal_queries`` and ``held_queries``.
        """
        run = self._post_schema_routing_start(job_id, database_name)
        scoring = self._router_scoring_inputs(
            job_id, database_name, run["selected_engines"], run["start_version"]
        )
        for depth in range(POST_SCHEMA_MAX_DEPTH + 1):
            print(f"\n[router] === Routing pass {depth} ===")
            routed = self._post_schema_routing_pass(job_id, database_name, run, depth, scoring)
            if routed is None:
                return run
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
        return run

    def _post_schema_routing_start(self, job_id: str, database_name: str) -> dict:
        """The router's run state before its first pass (JSON-safe, so a headless
        run can keep it between passes, #1059)."""
        selected_engines = self._selected_engines(job_id, database_name)
        assignment_version = self._effective_version(job_id, database_name)
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
        kept = self._engines_with_in_scope_queries(job_id, database_name, assignment_version)
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
        from src.agents.schema_design.design_trace import read_design_trace
        from src.contracts.post_schema_router_output import HeldQuery, QueryRouting, TerminalQuery

        selected_engines = run["selected_engines"]
        assignment_version = run["assignment_version"]
        assignment = self._read_assignment(job_id, database_name, assignment_version)
        kept = set(run["kept"]) & self._engines_with_in_scope_queries(
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
                        read_design_trace(self.store, trace_key)
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
        # the same refresh the reality check's revision gets (ADR-029). The cache
        # overlay is kept as approved, its summary recomputed (#1379): a moved query
        # keeps its cache, and only the synthesis safety net drops one, with a note.
        after = {qa["assigned_engine"] for qa in query_assignments if qa.get("in_scope", True)}
        refreshed = refresh_consolidated_assignment(
            revised,
            collector,
            analysis_outputs,
            dead_engines=before - after,
            keep_cache_overlay=True,
        )
        # Refuse a version whose engines do not follow from ``version`` plus
        # its own Moves (an engine set without move_ledger, #616).
        check_replay(
            ledger.moves,
            start,
            state_of(refreshed.get("query_assignments", [])),
            "post-schema reroute",
        )
        check_in_scope(
            refreshed.get("query_assignments", []),
            scope_from_store(self.store, database_name, job_id),
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
        new_version, revised, diff, designed = self._write_post_schema_reroute(
            job_id, database_name, assignment, version, routings
        )
        try:
            self._carry_forward_designs(job_id, database_name, diff, designed, version, new_version)
            for engine in diff["affected"]:
                self.redesign(job_id, database_name, engine, new_version)
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
        # The restated version was checked when written; checked again here, as every
        # writer is (#1327), since ``_rollback_moves`` has no store to read it from.
        check_in_scope(
            restated.get("query_assignments", []),
            scope_from_store(self.store, database_name, job_id),
            "post-schema rollback",
        )
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

    def run_external_step(self, job_id: str, database_name: str) -> dict:
        """One step of the post-schema routing for a headless run; returns its status.

        The same passes, reroute version and cascade bound as
        :meth:`run` (ADR-031 Decision 11 Amendment 2: a
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
        ``rolled_back``: :meth:`roll_back_external` ran.
        """
        key = self._external_routing_key(job_id, database_name)
        effective = self._effective_version(job_id, database_name)
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

    def roll_back_external(self, job_id: str, database_name: str) -> dict:
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
        if self._effective_version(job_id, database_name) != new_version:
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
