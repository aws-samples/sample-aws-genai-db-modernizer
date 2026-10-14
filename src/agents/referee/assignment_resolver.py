"""
Assignment Resolver — Auto-generates query→engine mappings from analysis results.

Implements the assignment resolution algorithm:
  1. Build signal-based overrides from triage (query-level pattern→engine mapping)
  2. Build anti-pattern penalties from analysis (query-level demotions)
  3. Build co-dependency groups from significant JOIN relationships
  4. Score each query against each engine (0–100), adjusted by signals + anti-patterns
  5. Assign co-dependent groups atomically (all queries → best engine for group)
  6. Assign remaining queries individually (highest adjusted score wins)
  7. Fallback to aurora for queries with no scores
  8. Mark the cache overlay (hot reads ElastiCache can front, #296)
  9. Derive table assignments, dropping source_tables noise the SQL parser
     introduced that is not a table or view the collector saw (#316)

Only system-of-record engines own queries (#296): ElastiCache is a cache layer
(``cache_overlay.CACHE_OVERLAY_ENGINES``) and never owns one, and an engine that
is not a system of record never owns a write. Exact score ties go to the
source-compatible engine; any other tie has no ADR-031 rule and is reported as
unresolved rather than decided by table traffic, query count or name (#503).

The key insight: assignment happens QUERY BY QUERY, not table by table.
Triage signals like text_search→opensearch override table-level averages
because the signal is about the query's workload pattern, not the table's
general suitability.

``resolve()`` accepts an optional ``routing_trace: RoutingTrace`` (#497), off by
default, that records each query's full per-engine score ladder for
``tests/e2e/routing_snapshot.py``'s per-query regression snapshot. See
``routing_trace.RoutingTrace``.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime

from src.agents.referee.aurora_choice import (
    AURORA_ENGINES,
    choose_heterogeneous_engine,
    detect_heterogeneous_features,
    pick_aurora_engine,
    pick_aurora_engine_with_tie,
    source_database_engine,
)
from src.agents.referee.cache_overlay import apply_cache_overlay, can_own, overlay_summary
from src.agents.referee.co_dependency import build_co_dependency_groups, is_significant_join
from src.agents.referee.query_scoring import (
    ScoringContext,
    compute_query_confidence,
    score_query_for_engine,
)
from src.agents.referee.reality_check import opensearch_justification
from src.agents.referee.routing_trace import RoutingTrace
from src.agents.referee.table_assignments import derive_table_assignments, retained_engine_for
from src.agents.referee.table_resolution import TableNameResolver
from src.agents.referee.tie_break import break_tie
from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA
from src.agents.referee.utility_statements import is_utility_statement
from src.contracts.assignment_models import (
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    AuroraEngineChoice,
    CacheOverlaySummary,
    QueryAssignment,
)
from src.shared.engine_names import PLACEHOLDER_ENGINES, display_engine, without_placeholder_engines
from src.shared.signal_labels import signal_noun

# Callers import these from here; they live in the modules imported above.
__all__ = [
    "AssignmentResolver",
    "RoutingTrace",
    "break_owner_tie",
    "build_co_dependency_groups",
    "derive_table_assignments",
    "is_significant_join",
    "retained_engine_for",
]

# Triage signals that strongly indicate an engine is the RIGHT fit for a query.
# When a signal maps query→engine and that engine was selected by triage,
# the query should go to that engine regardless of table-level confidence.
# Cache signals (leaderboard_pattern, session_store) are not here: ElastiCache
# never owns a query, so they are only cache-overlay hints for hot reads (#296,
# ``cache_overlay.CACHE_HINT_SIGNALS``).
SIGNAL_ENGINE_OVERRIDES: dict[str, str] = {
    "text_search": "opensearch",
    "graph_traversal": "neptune",
}

# Engines excluded from the SIGNAL_ENGINE_OVERRIDES entry above unless the
# #326 justification floor agrees the signal is real (#475): a text_search
# signal alone pinned any `LIKE`, including a parameterized `LIKE ?` with no
# visible wildcard, to OpenSearch regardless of traffic or search depth --
# exactly the mechanism that kept 4 shallow, low-traffic admin-search queries
# on a standing OpenSearch domain in a maintainer's run (#326). Reusing
# ``reality_check_floors.opensearch_justification`` keeps the override and the
# reality-check floor applying the same rule, instead of two copies that can
# drift. Scoped by override engine, not by signal name, so a future signal
# that also maps to opensearch gets the same gate for free.
JUSTIFICATION_GATED_OVERRIDE_ENGINES: frozenset[str] = frozenset({"opensearch"})


class AssignmentResolver:
    """Generates default query→engine assignments from analysis outputs."""

    def resolve(
        self,
        triage: dict,
        analysis_outputs: dict[str, dict],
        collector_output: dict,
        *,
        routing_trace: RoutingTrace | None = None,
    ) -> Assignment:
        """Produce initial assignment based on query-level scoring.

        For each query:
        1. Check if triage signals mandate a specific engine (override)
        2. Compute per-engine confidence adjusted by anti-pattern penalties
        3. Pick the engine with highest adjusted score

        Respects co-dependent query groups (queries sharing significant
        JOINs on the same tables stay together).
        """
        # Local copy: this method mutates ``analysis_outputs`` (#381, Step 4b below)
        # when it collapses two competing Aurora engines to one, and the caller's own
        # dict is also handed to ``AssignmentValidator`` afterwards -- it must see every
        # engine it originally analyzed, not this method's internal decision.
        analysis_outputs = without_placeholder_engines(analysis_outputs)

        queries = collector_output.get("queries", {}).get("query_patterns", [])
        tables = collector_output.get("database_schema", {}).get("tables", [])
        # selected_agents can be a list of strings or dicts with agent_type
        raw_selected = triage.get("selected_agents", [])
        selected_engines = {
            a["agent_type"] if isinstance(a, dict) else a for a in raw_selected
        } - PLACEHOLDER_ENGINES  # ADR-031 Decision 9: never a candidate
        # Computed early (moved up from Step 4b below, #475) so Step 1 can gate
        # a justification-gated signal override by the same source engine the
        # #326 justification floor itself keys off.
        source_engine = source_database_engine(collector_output)

        # Step 1: Build signal-based overrides from triage
        signal_overrides = self._build_signal_overrides(
            triage, selected_engines, queries, source_engine
        )

        # Step 2/3b: One shared context (``ScoringContext.from_pipeline``,
        # #549 round-2 review) builds the anti-pattern penalty map and the
        # per-query capability map -- triage's own ``query_capabilities``
        # (built from the collector's signals, #338 follow-up, review of
        # #375 finding 8: a bare COUNT(*)/FOUND_ROWS()/SQL_CALC_FOUND_ROWS
        # query, or a SHOW/SET statement before the resolver's own utility
        # pin runs, needs this gate so it never wins the v1 confidence-
        # scoring path onto an engine that cannot run it), falling back to
        # ``triage.detect_query_capabilities`` run against the collector
        # with triage's own signals (never a fresh re-detection of the
        # signals themselves) only when triage.json itself has no
        # ``query_capabilities`` key at all. This is the SAME context
        # ``score_query_for_engine`` (Step 4 below) and every other per-query
        # scorer (``cache_overlay.fallback_owner``, #549) build theirs with,
        # so a gate or the penalty can never drift out of agreement between
        # this method and another caller.
        scoring_ctx = ScoringContext.from_pipeline(analysis_outputs, triage, collector_output)
        anti_pattern_map = scoring_ctx.anti_pattern_map
        anti_pattern_notes = scoring_ctx.anti_pattern_notes

        # Step 3: Build co-dependency groups
        co_dep_groups = build_co_dependency_groups(queries, tables)

        # Step 4: Score each query against each engine (adjusted), via the
        # one shared pipeline (``score_query_for_engine``): the hard
        # exclusion gate, the hard capability gate, the base confidence and
        # the anti-pattern penalty, clamped at 0. A gated ``None`` still
        # needs a 0 recorded in ``scores`` here -- unlike a caller comparing
        # a few candidates, this method must give EVERY analyzed engine an
        # entry for Step 5/6 below to look up -- and its ``gate_reason``
        # becomes the human-readable ``exclusion_notes`` entry directly, with
        # no second run of the gates to find out why (#549 round-2 review,
        # Suggestion 1).
        scores: dict[str, dict[str, int]] = {}
        # query_id -> list of exclusion notes. Each note keeps the raw ``engine`` id
        # and ``gate`` kind (``EngineScore.gate_kind``, never a text match on
        # ``gate_reason`` -- #572) separately from its ``customer_text``: the
        # routing trace (Step 7c) and the homogeneous-Aurora pruning below only
        # ever need to match on engine/gate, never on rendered prose. ``gate_reason``
        # is already customer-safe prose -- display_engine, the exclusion's
        # customer-facing ``customer_message``, a capability label, never the raw
        # engine id, a rule_id tag or the technical, log-only ``description`` --
        # built that way in ``query_scoring.score_query_for_engine`` itself (#572),
        # so every caller of that one shared pipeline gets it for free.
        exclusion_notes: dict[str, list[dict[str, str]]] = {}
        for engine in analysis_outputs:
            for query in queries:
                qid = query["query_id"]
                scores.setdefault(qid, {})

                result = score_query_for_engine(query, engine, scoring_ctx)
                if result.score is None:
                    scores[qid][engine] = 0
                    # A gated EngineScore always carries its reason and kind (see
                    # score_query_for_engine's own contract); the "if" is just
                    # mypy narrowing, never expected to be False.
                    if result.gate_reason is not None:
                        exclusion_notes.setdefault(qid, []).append(
                            {
                                "engine": engine,
                                "gate": result.gate_kind or "",
                                "customer_text": result.gate_reason,
                            }
                        )
                    continue

                scores[qid][engine] = int(result.score)

        # Step 4b: Collapse a heterogeneous source's two competing Aurora engines to
        # one (#381). A source with no Aurora dialect of its own (SQL Server, Oracle,
        # DB2) has triage select both aurora_mysql and aurora_postgresql, and both were
        # just scored above like any other candidate. Compare the total adjusted score
        # each would earn across every query -- the design's "winner takes all
        # relational queries" -- before any query is actually assigned, and drop the
        # losing engine from this method's own ``analysis_outputs``/``scores`` so every
        # step after this one (co-dependency groups, ties, the Aurora fallback, the
        # utility-statement pin, table derivation) only ever sees the winner, the same
        # as a homogeneous source only ever had one Aurora candidate to begin with.
        aurora_engine_choice: AuroraEngineChoice | None = None
        both_aurora = AURORA_ENGINES & set(analysis_outputs)
        if source_engine not in SOURCE_ENGINE_TO_AURORA and len(both_aurora) == 2:
            totals = {
                engine: float(sum(scores.get(q["query_id"], {}).get(engine, 0) for q in queries))
                for engine in both_aurora
            }
            features = detect_heterogeneous_features(collector_output, source_engine)
            winner, trace = choose_heterogeneous_engine(sorted(both_aurora), totals, features)
            loser = next(e for e in both_aurora if e != winner)
            analysis_outputs = {e: a for e, a in analysis_outputs.items() if e != loser}
            for qid_scores in scores.values():
                qid_scores.pop(loser, None)
            aurora_engine_choice = AuroraEngineChoice(
                source_engine=source_engine,
                engine=winner,
                totals=trace["totals"],
                margin=trace["margin"],
                deciding_features=trace["deciding_features"],
                reason=trace["reason"],
            )

        # The "retained Aurora engine" passed to ``derive_table_assignments`` below,
        # the same role ``retained_engine_for`` plays for every other caller (#317):
        # the source's own dialect when it has one, else the #381 winner just computed,
        # else ``None`` (no source engine at all, or no Aurora candidate selected --
        # unaffected by this change). Computed here, rather than by calling
        # ``retained_engine_for(collector_output)`` again, because that helper only
        # knows ``SOURCE_ENGINE_TO_AURORA`` -- it has no way to see the #381 choice
        # this method just made.
        effective_relational_engine = SOURCE_ENGINE_TO_AURORA.get(source_engine) or (
            aurora_engine_choice.engine if aurora_engine_choice else None
        )

        # Capability guarantee for a homogeneous source (review of #474, round 3):
        # this exact query already ran on the source database, so the
        # source-compatible Aurora engine (MySQL/MariaDB -> aurora_mysql,
        # PostgreSQL -> aurora_postgresql) is always ownable and serviceable for
        # it -- never hard-excluded (``check_exclusions``) or capability-zeroed
        # (``can_engine_serve_capability``) the way a purpose-built engine can
        # be, even if a future rule would otherwise drop it. This is a presence
        # guarantee, not a score override: it still competes on its own
        # confidence score below, and a clearly higher-scoring engine still
        # wins. An exact tie already goes to it, via the resolver's existing
        # source-compatible tie-break (``break_owner_tie``'s first rule) -- not
        # duplicated here. A heterogeneous source (#381, no entry in
        # ``SOURCE_ENGINE_TO_AURORA``) has no such guarantee: ``aurora_engine_
        # choice``'s winner, if any, is only a normal scored candidate here.
        homogeneous_aurora = SOURCE_ENGINE_TO_AURORA.get(source_engine)
        if homogeneous_aurora and homogeneous_aurora in analysis_outputs:
            analysis = analysis_outputs[homogeneous_aurora]
            for query in queries:
                qid = query["query_id"]
                base_score = compute_query_confidence(qid, query, homogeneous_aurora, analysis)
                penalty = anti_pattern_map.get((qid, homogeneous_aurora), 0)
                scores.setdefault(qid, {})[homogeneous_aurora] = max(0, base_score - penalty)
                if qid in exclusion_notes:
                    exclusion_notes[qid] = [
                        n for n in exclusion_notes[qid] if n["engine"] != homogeneous_aurora
                    ]
                    if not exclusion_notes[qid]:
                        del exclusion_notes[qid]

        # Step 5: Assign co-dependent groups atomically. Only engines that may own
        # every query of the group compete (cache engines never own; engines that
        # are not a system of record own no write, #296).
        query_by_id = {q["query_id"]: q for q in queries}
        assigned: dict[str, str] = {}
        assigned_confidence: dict[str, int] = {}
        assigned_reason: dict[str, str] = {}
        assigned_signal: dict[str, str] = {}

        # Queries left for the Aurora fallback. The engine is chosen once the
        # rest are assigned, since it can depend on how many queries each
        # Aurora engine already serves (#288).
        fallback_qids: list[str] = []
        # Exact score ties, resolved once every untied query is placed:
        # source-compatible wins, otherwise reported as unresolved (#503).
        tied: list[tuple[list[str], list[str], int, str]] = []

        for group in co_dep_groups:
            candidates = [
                e
                for e in analysis_outputs
                if all(can_own(e, query_by_id.get(qid)) for qid in group)
            ]
            if not candidates:
                for qid in group:
                    fallback_qids.append(qid)
                    assigned[qid] = ""
                    assigned_confidence[qid] = 0
                    assigned_reason[qid] = "no analysis available"
                continue
            group_score = {
                e: sum(scores.get(qid, {}).get(e, 0) for qid in group) / len(group)
                for e in candidates
            }
            if routing_trace is not None:
                routing_trace.record_group(group, group_score)
            top = max(group_score.values())
            best = [e for e in candidates if group_score[e] == top]
            if len(best) > 1:
                tied.append((list(group), best, 0, "co-dependency group"))
                for qid in group:
                    assigned[qid] = ""
                continue
            for qid in group:
                assigned[qid] = best[0]
                assigned_confidence[qid] = scores.get(qid, {}).get(best[0], 0)
                assigned_reason[qid] = f"co-dependency group → {display_engine(best[0])}"

        # Step 6: Assign remaining queries individually
        for query in queries:
            qid = query["query_id"]
            if qid in assigned:
                continue

            # Check signal override first
            if qid in signal_overrides and can_own(signal_overrides[qid]["engine"], query):
                engine = signal_overrides[qid]["engine"]
                signal = signal_overrides[qid]["signal"]
                assigned[qid] = engine
                assigned_confidence[qid] = scores.get(qid, {}).get(engine, 0)
                # #584: readable signal and engine names; the raw signal
                # travels as data in ``signal_override`` (assigned_signal).
                assigned_reason[qid] = (
                    f"signal override: {signal_noun(signal)} → {display_engine(engine)}"
                )
                assigned_signal[qid] = signal
                continue

            # Otherwise pick highest adjusted score among the engines that may own it
            query_scores = {e: s for e, s in scores.get(qid, {}).items() if can_own(e, query)}
            if query_scores:
                top_score = max(query_scores.values())
                best = [e for e in query_scores if query_scores[e] == top_score]
                if len(best) > 1:
                    tied.append(([qid], best, top_score, "individual"))
                    assigned[qid] = ""
                    continue
                assigned[qid] = best[0]
                assigned_confidence[qid] = top_score
                assigned_reason[qid] = f"highest fit for {display_engine(best[0])}"
            else:
                # Step 7: Fallback to aurora engine from triage
                fallback_qids.append(qid)
                assigned[qid] = ""
                assigned_confidence[qid] = 0
                assigned_reason[qid] = "no engine scored this query"

        # Step 6b: Resolve exact ties (ADR-031 Decision 2, #503). ``settled_counts``
        # is a snapshot of every non-tied query's engine, taken once here, before
        # this loop resolves ANY tie -- so one tie's placeholder pick never feeds
        # the settled-workload count another tie in the same batch is judged
        # against (that would make the result depend on the order ``tied`` happens
        # to be in, which #503 explicitly rules out). Computed unconditionally
        # (not only ``if tied:``) and persisted onto ``routing_trace`` (#503): the
        # reality check reuses this SAME snapshot -- rather than recomputing its
        # own, later,
        # from the final v1 assignment (after the Aurora fallback, the utility
        # pin and signal overrides have filled in more queries) -- so a tie that
        # is otherwise unchanged can never silently re-pick between the same two
        # engines just because the two stages measured "settled" differently.
        settled_counts = Counter(e for e in assigned.values() if e)
        if routing_trace is not None:
            routing_trace.record_settled_counts(settled_counts)
        assigned_tied_engines: dict[str, list[str]] = {}
        if tied:
            source_aurora = SOURCE_ENGINE_TO_AURORA.get(source_engine)
            for qids, best, top_score, kind in tied:
                # Sorted member ids (#503): the hash fallback's seed must not
                # depend on ``qids``'s own input order,
                # which here comes from ``build_co_dependency_groups``'s (or the
                # query list's) incidental order, not a stable identifier.
                hash_seed = ",".join(sorted(qids))
                winner, why, tie_kind = break_owner_tie(
                    best, source_aurora, settled_counts=settled_counts, query_id=hash_seed
                )
                for qid in qids:
                    if routing_trace is not None:
                        routing_trace.record_tie(qid, tie_kind)
                    assigned[qid] = winner
                    if tie_kind == "tie_unresolved":
                        assigned_tied_engines[qid] = sorted(best)
                    if kind == "individual":
                        assigned_confidence[qid] = top_score
                        assigned_reason[qid] = f"highest fit for {display_engine(winner)} ({why})"
                    else:
                        assigned_confidence[qid] = scores.get(qid, {}).get(winner, 0)
                        assigned_reason[qid] = (
                            f"co-dependency group → {display_engine(winner)} ({why})"
                        )

        if fallback_qids:
            # #381 review: still ``selected_engines`` (the fallback must use whichever
            # Aurora engine triage selected, whether or not it was ever analyzed -- the
            # pre-#381 contract this fallback has always had), but with the Step 4b
            # loser explicitly excluded, so a heterogeneous source's fallback only ever
            # sees the engine the resolver actually chose, the same restriction every
            # other step after Step 4b already respects. A two-Aurora-engine
            # ``fallback_pool`` is reachable here (#550 review, Important 4): triage can
            # select both for a heterogeneous source while fewer than two actually got
            # analyzed, which skips the Step 4b collapse above and leaves both in
            # ``selected_engines`` -- see
            # ``test_fallback_reaches_the_shared_tie_break_with_both_aurora_engines``.
            fallback_pool = selected_engines - (
                {e for e in AURORA_ENGINES if e != aurora_engine_choice.engine}
                if aurora_engine_choice
                else set()
            )
            # #550 review (Critical 1/2): the SAME settled-workload snapshot Step 6b
            # just used (``settled_counts``, taken before any tie -- including this
            # one -- was resolved), never a fresh recount of ``assigned`` that would
            # fold Step 6b's own placeholder picks back in; and a tie key built from
            # these specific queries' own ids, the same way Step 6b's ``hash_seed`` is,
            # so this fallback's tie-break is not a constant, workload-independent
            # name preference.
            aurora_fallback, fallback_tie_kind = _resolve_aurora_fallback(
                fallback_pool,
                source_engine,
                settled_counts,
                prior_choice=aurora_engine_choice.engine if aurora_engine_choice else None,
                tie_key=",".join(sorted(fallback_qids)),
            )
            fallback_tied = sorted(AURORA_ENGINES & fallback_pool)
            for qid in fallback_qids:
                assigned[qid] = aurora_fallback
                if fallback_tie_kind == "tie_unresolved":
                    assigned_tied_engines[qid] = fallback_tied
                    if routing_trace is not None:
                        routing_trace.record_tie(qid, fallback_tie_kind)

        # Step 6c: Surface a reduced capability-redesign penalty's reason
        # (#477) in the assignment reason -- but only on the engine the note
        # is actually about, and only when the query ended up assigned to
        # that engine. Attaching it regardless of the winner (as an earlier
        # version of this fix did) would show up on, say, an Aurora win's
        # reason and misleadingly read as if Aurora were the one that needed
        # the redesign; checking ``assigned[qid]`` here means the note only
        # ever describes the engine the query actually landed on.
        for qid, engine in assigned.items():
            note = anti_pattern_notes.get((qid, engine))
            if note:
                assigned_reason[qid] = f"{assigned_reason[qid]} | {note}"

        # Step 7b: Utility and metadata statements never enter engine routing
        # (#327). SHOW/SET/DESCRIBE/EXPLAIN/CREATE/ALTER/DROP and catalog
        # lookups (information_schema, pg_catalog) are database-administration
        # traffic, not an application access pattern a target engine needs to
        # serve -- a purpose-built engine like DynamoDB has no concept of a
        # column list to SHOW or a session variable to SET. They are pinned to
        # the source-compatible relational engine, overriding whatever the
        # confidence-scoring path above picked, so every other artifact still
        # accounts for every collected query. If no Aurora engine is in play,
        # the scored assignment is left as-is rather than left unset.
        utility_qids = [
            query["query_id"]
            for query in queries
            if is_utility_statement(query.get("query_text"), query.get("tables_accessed"))
        ]
        # #550 review: the pre-tie ``settled_counts`` snapshot (Critical 2), never a
        # fresh recount of ``assigned``, which by now also folds in Step 6b's own
        # placeholder picks and the Aurora fallback just above; and a tie key built
        # from these specific utility queries' own ids (Critical 1), the same way
        # the fallback above and Step 6b's own ``hash_seed`` are. This candidate pool
        # (``analysis_outputs.keys()``) cannot actually hold both Aurora engines by
        # the time this runs though (Important 4): Step 4b above always collapses
        # them to one when the source is heterogeneous and both were analyzed, and a
        # homogeneous source's own dialect always resolves ``pick_aurora_engine``
        # before a tie would ever matter -- so, unlike the fallback above, this stays
        # on the plain, tie-kind-less ``pick_aurora_engine`` rather than building out
        # ``tied_engines``/``record_tie`` bookkeeping for a tie this pool cannot
        # produce today.
        utility_engine = pick_aurora_engine(
            analysis_outputs.keys(),
            source_engine,
            settled_counts,
            prior_choice=aurora_engine_choice.engine if aurora_engine_choice else None,
            tie_key=",".join(sorted(utility_qids)),
        )
        if utility_engine:
            for qid in utility_qids:
                assigned[qid] = utility_engine
                assigned_confidence[qid] = 100
                assigned_reason[qid] = (
                    "utility/metadata statement — kept on source-compatible "
                    f"relational engine ({display_engine(utility_engine)})"
                )
                assigned_signal.pop(qid, None)

        # Step 7c: Populate the opt-in routing trace (#497), off by default
        # (``routing_trace is None``). Reads ``exclusion_notes``, ``anti_pattern_map``
        # and ``assigned``/``assigned_reason`` as finalized above. The score recorded
        # for a non-gated engine is always ``scores[qid][engine]`` -- the value
        # Step 5/6 above actually compared when choosing an engine, including #381's
        # Aurora-engine collapse and the #474 homogeneous-engine guarantee -- never a
        # recompute. ``base`` (a fresh, pure, side-effect-free
        # ``compute_query_confidence`` call) and ``penalty`` are kept alongside as
        # diagnostics; a gap between the used score and ``max(0, base - penalty)``
        # (there should never be one, but a future change to either side could
        # silently open one) is recorded as ``adj`` rather than raised, so the
        # snapshot and its diff -- not a crash -- are what surface it (#497 review).
        if routing_trace is not None:
            for query in queries:
                qid = query["query_id"]
                notes = exclusion_notes.get(qid, [])
                ladder: dict[str, object] = {}
                for engine, analysis in analysis_outputs.items():
                    gate = None
                    for excl_note in notes:
                        if excl_note["engine"] == engine:
                            gate = excl_note["gate"]
                            break
                    if gate:
                        ladder[engine] = {"gate": gate}
                        continue
                    base = compute_query_confidence(qid, query, engine, analysis)
                    penalty = anti_pattern_map.get((qid, engine), 0)
                    used = scores.get(qid, {}).get(engine, 0)
                    adj = used - max(0, base - penalty)
                    if not can_own(engine, query):
                        entry: dict[str, object] = {
                            "gate": "write_gate",
                            "base": base,
                            "used": used,
                        }
                        if adj:
                            entry["adj"] = adj
                        ladder[engine] = entry
                    elif penalty or adj:
                        entry = {"base": base, "penalty": penalty, "used": used}
                        if adj:
                            entry["adj"] = adj
                        ladder[engine] = entry
                    else:
                        ladder[engine] = used
                payload: dict[str, object] = {
                    "engines": ladder,
                    "assigned_engine": assigned.get(qid, ""),
                    "assigned_reason": assigned_reason.get(qid, ""),
                }
                group_info = routing_trace.group_by_query.get(qid)
                if group_info is not None:
                    payload["group"] = group_info
                signal = assigned_signal.get(qid)
                if signal is not None:
                    payload["signal"] = signal
                tie_rule = routing_trace.tie_break_by_query.get(qid)
                if tie_rule is not None:
                    payload["tie_break"] = tie_rule
                routing_trace.record(qid, payload)

        # Step 8: Cache overlay (#296): hot bounded reads get cache_engine; the
        # owner stays the engine assigned above.
        overlay_dicts = apply_cache_overlay(
            [
                {"query_id": q["query_id"], "assigned_engine": assigned[q["query_id"]]}
                for q in queries
            ],
            queries,
            analysis_outputs.keys(),
        )
        overlay_by_id = {o["query_id"]: o for o in overlay_dicts}

        # Build query→tables lookup
        query_tables: dict[str, list[str]] = {}
        for query in queries:
            qid = query["query_id"]
            query_tables[qid] = query.get("tables_accessed", [])

        # Build query assignments
        query_assignments: list[QueryAssignment] = []
        for query in queries:
            qid = query["query_id"]
            engine = assigned[qid]
            confidence = assigned_confidence.get(qid, 0)
            reason = assigned_reason.get(qid, f"highest fit for {display_engine(engine)}")

            # Append exclusion notes to the reason if this query was excluded from other engines
            if qid in exclusion_notes:
                reason = (
                    reason + " | " + "; ".join(n["customer_text"] for n in exclusion_notes[qid])
                )

            query_assignments.append(
                QueryAssignment(
                    query_id=qid,
                    assigned_engine=engine,
                    confidence=confidence,
                    source_tables=query_tables.get(qid, []),
                    assignment_reason=reason,
                    signal_override=assigned_signal.get(qid),
                    cache_engine=overlay_by_id[qid]["cache_engine"],
                    cache_pattern=overlay_by_id[qid]["cache_pattern"],
                    cache_reason=overlay_by_id[qid]["cache_reason"],
                    tied_engines=assigned_tied_engines.get(qid, []),
                )
            )

        # Step 9: Derive table assignments. Noise the SQL parser can put in
        # source_tables (CTE aliases, catalogs, sequences, keywords, columns)
        # is dropped first (#316), scoped to the tables and views the
        # collector actually saw. A table's primary_engine must always be a
        # durable owner (#317), so pass the retained source-compatible engine
        # as the fallback for tables with no owner engine among their
        # assigned engines.
        table_assignments, unresolved_table_names = derive_table_assignments(
            query_assignments,
            known_tables=TableNameResolver.from_collector(collector_output),
            retained_engine=effective_relational_engine,
        )

        # Flatten co-dep groups to list of lists of query_ids
        co_dep_lists = [list(g) for g in co_dep_groups]

        return Assignment(
            job_id=collector_output.get("job_id", "unknown"),
            version=1,
            status=AssignmentStatus.AUTO_GENERATED,
            source=AssignmentSource.ASSIGNMENT_RESOLUTION,
            timestamp=datetime.now(tz=UTC),
            query_assignments=query_assignments,
            table_assignments=table_assignments,
            co_dependency_groups=co_dep_lists,
            validation_warnings=[],
            cache_overlay=_overlay_model(overlay_summary(overlay_dicts, queries)),
            unresolved_table_names=unresolved_table_names,
            aurora_engine_choice=aurora_engine_choice,
        )

    def _build_signal_overrides(
        self,
        triage: dict,
        selected_engines: set[str],
        queries: list[dict],
        source_engine: str,
    ) -> dict[str, dict]:
        """Build query_id → {engine, signal} overrides from triage signals.

        Only creates overrides when:
        - The signal type has a known engine mapping (SIGNAL_ENGINE_OVERRIDES)
        - The target engine was selected by triage (available for assignment)
        - The signal has specific query_ids attached
        - For a ``JUSTIFICATION_GATED_OVERRIDE_ENGINES`` target (opensearch,
          #475): the #326 justification floor
          (``reality_check_floors.opensearch_justification``) agrees, per query,
          that the signal reflects real search depth and enough traffic or
          corpus size -- a signal alone (e.g. any ``LIKE``, including a
          parameterized ``LIKE ?`` with no visible wildcard) is not enough to
          pin a standing engine regardless of load. A query the floor
          rejects is not forced anywhere; it falls through to normal
          confidence scoring instead.
        """
        overrides: dict[str, dict] = {}
        query_map: dict[str, dict] | None = None
        for signal in triage.get("signals", []):
            signal_name = signal.get("signal", "")
            override_engine = SIGNAL_ENGINE_OVERRIDES.get(signal_name)
            if not override_engine:
                continue
            if override_engine not in selected_engines:
                continue
            qids = list(signal.get("query_ids", []))
            if override_engine in JUSTIFICATION_GATED_OVERRIDE_ENGINES:
                if query_map is None:
                    query_map = {q["query_id"]: q for q in queries}
                # Grouped by the SIGNAL's own query_ids, not by whatever
                # OpenSearch ends up owning (#475 review, suggestion 1): no
                # assignment exists yet at this point in the resolver, only
                # candidates for the override, so the traffic/corpus floor is
                # judged against exactly the queries this signal is deciding
                # for -- never diluted or inflated by a query that might
                # independently win OpenSearch on its own score, or by one
                # this signal has nothing to do with. The reality check's own
                # mandatory-engine floor (below, ``opensearch_justification``
                # called with ``current_qas``) asks a different, later
                # question -- does the engine's CURRENT, possibly-absorbed
                # workload still earn its place -- so it groups by actual
                # ownership instead; the two are intentionally not the same
                # population.
                verdicts = opensearch_justification(
                    [{"query_id": qid} for qid in qids], query_map, source_engine
                )
                qids = [qid for qid in qids if verdicts.get(qid, (False, ""))[0]]
            for qid in qids:
                overrides[qid] = {
                    "engine": override_engine,
                    "signal": signal_name,
                }
        return overrides


# ---------------------------------------------------------------------------
# Owner tie-break (#296, #503)
# ---------------------------------------------------------------------------


def _overlay_model(summary: dict | None) -> CacheOverlaySummary | None:
    return CacheOverlaySummary(**summary) if summary else None


def break_owner_tie(
    engines: list[str],
    source_aurora: str | None,
    *,
    settled_counts: Mapping[str, int] | None = None,
    query_id: str = "",
) -> tuple[str, str, str]:
    """Pick the owner among engines with the same score, and say why
    (ADR-031 Decision 2, #503).

    1. the source-compatible engine (the Aurora engine of the source's
       dialect), when it is one of the tied engines;
    2. otherwise there is no ADR-031 rule for this tie: a deterministic
       engine is still returned (``pick_unresolved_tie_placeholder``'s
       maintainer-decided placeholder, via ``break_tie``), but it is
       reported honestly, in customer-facing wording, as an unresolved tie
       rather than a real rule.

    The returned ``reason`` is the WHOLE customer-facing explanation, not a
    fragment a caller wraps in its own sentence with a raw engine code
    (#503): no raw engine code, no internal ``ADR-031``/issue reference, and
    the pick is never called a "preference" or "non-preferential" either way.

    Table traffic and the engine's name are never the reason a non-Aurora
    tie is decided (#503) -- this is the same ``break_tie`` the reality
    check's placement step uses, so the two cannot drift back out of
    agreement on what a tie means. ``settled_counts``/``query_id`` feed the
    unresolved-tie placeholder only; they never affect the source-compatible
    rule above.

    Cache engines are never in ``engines`` (they own nothing), so a tie can
    never tip a query, hot or not, onto the cache. The first assignment has
    no "current engine" for a query yet (nothing is assigned before this
    runs), so ``break_tie``'s stability rule never fires here -- only in the
    reality check, where a query already has one.

    Returns ``(winner, reason, tie_kind)``.
    """
    winner, tie_kind = break_tie(
        engines, set(), source_aurora, settled_counts=settled_counts, query_id=query_id
    )
    others = ", ".join(display_engine(e) for e in sorted(engines) if e != winner)
    if tie_kind == "source_compatible":
        return (
            winner,
            f"scores tie with {others}; {display_engine(winner)} is the source-compatible "
            "relational engine",
            tie_kind,
        )
    return (
        winner,
        f"scores tie with {others}; no routing rule decides between them; "
        f"listed under {display_engine(winner)} pending your choice",
        tie_kind,
    )


# ---------------------------------------------------------------------------
# Aurora fallback resolution
# ---------------------------------------------------------------------------


def _resolve_aurora_fallback(
    selected_engines: set[str],
    source_engine: str = "",
    query_counts: Mapping[str, int] | None = None,
    *,
    prior_choice: str | None = None,
    tie_key: str = "",
) -> tuple[str, str | None]:
    """Determine which Aurora engine to use as fallback, and the tie kind
    when the pick stayed unresolved (``None`` otherwise, #550 Important 3).

    Uses the Aurora engine selected by triage. When both are selected, the one
    matching the source database's dialect wins, then the shared ADR-031
    tie-break on the recorded SETTLED workload decides (``pick_aurora_engine_
    with_tie``, #288, #550). If no Aurora engine was selected, uses a generic
    'aurora' placeholder (legacy behavior) and no tie.

    ``prior_choice`` (#381 review) is ``Assignment.aurora_engine_choice.engine``
    when the resolver already made that choice this run -- passed through so
    a 0/0-count tie here never overrides it. ``query_counts`` must be a
    genuine settled count (the resolver's own pre-tie snapshot, never a raw
    recount of the current, possibly already-tied, assignment) and
    ``tie_key`` a stable identifier for the queries this call is deciding --
    see :func:`src.agents.referee.aurora_choice.pick_aurora_engine_with_tie`.
    """
    winner, tie_kind = pick_aurora_engine_with_tie(
        selected_engines,
        source_engine,
        query_counts,
        prior_choice=prior_choice,
        tie_key=tie_key,
    )
    return winner or "aurora", tie_kind
