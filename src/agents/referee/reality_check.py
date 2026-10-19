"""
Reality Check — CTO-level optimization of query-to-engine assignments.

Runs after the initial assignment and before schema design. Thinks like a CTO:
"Every additional database engine = monitoring, backups, failover, expertise,
and operational burden. Does this engine earn its place?"

Default posture: 2 engines max. A third engine must provide genuinely unique
capabilities that no other committed engine can serve.

Steps, each in its own module:
  1. Settle (Phase 1, ``reality_check_settle``) — which engines earn a place: an
     engine is removed when the other committed engines can serve its queries
     (shared per-query scores)
  2. Place (Phase 2, ``reality_check_place``) — every query on its
     highest-scoring remaining engine
  3. Justification floors (``reality_check_floors``) — a mandatory engine must
     still earn its fixed cost
  4. Records (``reality_check_records``) — the consolidation records are
     derived from the move ledger: one per net per-query move (#616)
  5. Architectural patterns and recommendations (``reality_check_patterns``)

Every engine change these steps make (and the handler's cache owner
normalisation, model flags, sanity sweep and customer-override restore) goes
through the move ledger (``move_ledger.apply_to_assignment``, #616), so the
reality check's version holds exactly the Moves that produced it and replays
from its stored input version.

The eligibility gate, engine costs and tie-break all passes share live in
``reality_check_rules``. This module runs the steps in order and re-exports
their names, so callers keep importing from ``reality_check``.

The output is a revised assignment (new version) plus architectural recommendations
that feed into synthesis.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING

from src.agents.referee.aurora_choice import source_database_engine
from src.agents.referee.capability_registry import triage_query_capabilities
from src.agents.referee.move_ledger import MoveLedger, ledger_for
from src.agents.referee.reality_check_floors import (
    NATIVE_TEXT_SEARCH_SOURCE_ENGINES,
    OPENSEARCH_MIN_CALLS_PER_SECOND,
    OPENSEARCH_MIN_CORPUS_ROWS_AVG,
    OPENSEARCH_NATIVE_OVERRIDE_MIN_CALLS_PER_SECOND,
    _apply_justification_floors,
    _find_best_absorber_for_query,
    _workload_ever_justified,
    closed_to_pinned_search,
    opensearch_justification,
)
from src.agents.referee.reality_check_patterns import (
    ARCHITECTURAL_PATTERNS,
    _build_recommendations,
    _detect_architectural_patterns,
    _engine_infra_cost,
    format_pattern_recommendation,
    patterns_for_assignment,
    refresh_patterns_and_recommendations,
)
from src.agents.referee.reality_check_place import _place_settled_queries
from src.agents.referee.reality_check_records import _queries, consolidations_from_moves
from src.agents.referee.reality_check_rules import (
    AURORA_ENGINES,
    ENGINE_BASE_COST,
    SPECIALIST_ABSORB_CAPABILITIES,
    SPECIALIST_ABSORB_SIGNALS,
    TARGET_ENGINE_COUNT,
    UNIQUE_DELTA_THRESHOLD,
    _build_query_signals,
    _current_for_eligibility,
    _group_eligible_engines,
    _is_signal_override,
    _pick_winner,
    _query_eligible_engines,
    _resolver_score,
    _top_scorers,
    may_absorb,
)
from src.agents.referee.reality_check_settle import (
    _evaluate_engine_removal,
    _removal_tiebreak_key,
    _round_evaluation_order,
    _settle_committed_engines,
)
from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA
from src.agents.referee.write_groups import assignment_sources_of_truth

if TYPE_CHECKING:
    from src.agents.referee.query_scoring import ScoringContext

from src.shared.engine_names import without_placeholder_engines
from src.shared.engines_in_use import engines_in_use

# Names callers and tests import from this module; they live in the step modules.
__all__ = [
    "ARCHITECTURAL_PATTERNS",
    "AURORA_ENGINES",
    "ENGINE_BASE_COST",
    "NATIVE_TEXT_SEARCH_SOURCE_ENGINES",
    "OPENSEARCH_MIN_CALLS_PER_SECOND",
    "OPENSEARCH_MIN_CORPUS_ROWS_AVG",
    "OPENSEARCH_NATIVE_OVERRIDE_MIN_CALLS_PER_SECOND",
    "SPECIALIST_ABSORB_CAPABILITIES",
    "SPECIALIST_ABSORB_SIGNALS",
    "TARGET_ENGINE_COUNT",
    "UNIQUE_DELTA_THRESHOLD",
    "_build_query_signals",
    "_build_recommendations",
    "_current_for_eligibility",
    "_detect_architectural_patterns",
    "_engine_infra_cost",
    "_evaluate_engine_removal",
    "_find_best_absorber_for_query",
    "_group_eligible_engines",
    "_is_signal_override",
    "_pick_winner",
    "_place_settled_queries",
    "_queries",
    "_query_eligible_engines",
    "_removal_tiebreak_key",
    "_resolver_score",
    "_round_evaluation_order",
    "_settle_committed_engines",
    "_top_scorers",
    "_workload_ever_justified",
    "format_pattern_recommendation",
    "may_absorb",
    "opensearch_justification",
    "patterns_for_assignment",
    "refresh_patterns_and_recommendations",
    "run_reality_check",
]


def _scoring_context(
    analysis_outputs: dict[str, dict],
    triage: dict,
    collector_output: dict,
    sources_of_truth: Mapping[str, str],
) -> ScoringContext:
    """The check's one shared scoring context (#490/#549), its capability waivers
    reading the input version's sources of truth (#377)."""
    from src.agents.referee.query_scoring import ScoringContext

    return ScoringContext.from_pipeline(
        analysis_outputs, triage, collector_output
    ).with_sources_of_truth(sources_of_truth)


def run_reality_check(
    assignment: dict,
    triage: dict,
    analysis_outputs: dict[str, dict],
    collector_output: dict,
    query_capabilities: dict[str, list[str]] | None = None,
    source_engine: str | None = None,
    ledger: MoveLedger | None = None,
    customer_kept_engines: Iterable[str] = (),
    sources_of_truth: Mapping[str, str] | None = None,
) -> dict:
    """Run the CTO-level reality check on the assignment.

    ``sources_of_truth`` (``{table: engine}``) are the input version's, its
    customer-pinned writes included (the handler holds pinned queries out of
    ``assignment``): the capability waivers (#377) read them in the trace's scores,
    Phase 1/2 eligibility and the floors alike. ``None`` reads them from
    ``assignment``.

    ``source_engine`` is the source database engine (e.g. ``"mysql"``); it picks
    the Aurora engine when both are committed. Defaults to the collector's.

    ``ledger`` (#616) is the move ledger every engine change of this check is
    recorded on (Phase 1/2 and the justification floors, through
    ``move_ledger.apply_to_assignment``); a fresh one starting from
    ``assignment``'s engines when ``None``. Returned as ``move_ledger``.

    ``customer_kept_engines`` (#860) are the engines that hold a customer pin
    (``customer_override``). The handler holds pinned queries out of
    ``assignment`` and puts them back as they are, so these engines stay in
    the plan whatever this check decides: Phase 1 keeps them committed (never
    a removal candidate), Phase 2 treats them as any other kept engine, and the
    floors that ask whether the workload pays for an engine skip them (their
    cost is paid for the pin). A query then leaves one only for an engine that
    scores it higher, and a query that scores higher on one goes there
    (ADR-031 Decision 7) -- except that a query with no real search depth
    never joins a pinned OpenSearch, and still leaves it (the per-query #326
    check, which is not about cost). An engine with no analysis is ignored.

    Thinks like a pragmatic CTO: "Every additional database is operational
    burden. Does this engine EARN its place, or can another engine already
    in the stack handle these queries?"

    Returns a dict with:
      - revised_assignments: list of QueryAssignment dicts (possibly modified)
      - consolidations: list of consolidation decisions made
      - architectural_patterns: list of recommended patterns
      - recommendations: list of human-readable recommendations
      - unique_value_assessment: per-engine analysis of unique vs redundant queries
      - move_ledger: the ledger holding this check's Moves (#616)
    """
    query_assignments = assignment.get("query_assignments", [])
    if ledger is None:
        ledger = ledger_for(query_assignments)
    queries = collector_output.get("queries", {}).get("query_patterns", [])
    query_map = {q["query_id"]: q for q in queries}
    signals = triage.get("signals", [])

    # Load query capabilities (hard architectural requirements)
    if query_capabilities is None:
        query_capabilities = triage_query_capabilities(triage)

    query_signals = _build_query_signals(signals)
    resolved_source_engine = (
        source_database_engine(collector_output) if source_engine is None else source_engine
    )

    # Queries whose FIRST-ASSIGNMENT engine is only a placeholder pick from
    # an unresolved tie (#503): no ADR-031 rule, and no real tiebreak rule
    # either, decided these -- ``QueryAssignment.tied_engines`` is non-empty.
    tie_unresolved_qids: set[str] = {
        qa["query_id"] for qa in query_assignments if qa.get("tied_engines")
    }
    tied_engines_by_qid: dict[str, list[str]] = {
        qa["query_id"]: list(qa["tied_engines"])
        for qa in query_assignments
        if qa.get("tied_engines")
    }

    # Build engine query counts. A tied query counts as native evidence for
    # exactly the ONE engine the first assignment's placeholder already
    # credited it to (``assigned_engine``) -- never for every tied engine,
    # and never for none at all (#503: excluding a tied query from EVERY
    # engine's native set could leave an engine whose only queries were
    # tied uncommitted with no fallback -- crashing the invariant check
    # below when nothing else was eligible either -- or silently drop a
    # tied workload onto a much weaker committed engine when Phase 1 never
    # even evaluated either tied engine). The query still carries
    # ``tied_engines`` and stays visible to the customer either way; this
    # only decides which engine's justification it counts toward in Phase 1.
    engine_queries: dict[str, list[dict]] = defaultdict(list)
    for qa in query_assignments:
        engine_queries[qa["assigned_engine"]].append(qa)

    # Snapshot now, before any pass below mutates ``engine_queries`` (#475
    # review finding 3): each engine's first-assignment workload-justified
    # verdict (``None`` where no floor applies), carried onto every
    # consolidation record for that engine so a full consolidation of one
    # that never earned its place is never presented as a saving.
    workload_justified: dict[str, bool | None] = {
        engine: _workload_ever_justified(engine, list(qas), query_map, resolved_source_engine)
        for engine, qas in engine_queries.items()
    }

    # Build set of queries with mandatory signal overrides (cannot be consolidated)
    mandatory_query_ids: set[str] = {
        qa["query_id"] for qa in query_assignments if _is_signal_override(qa)
    }

    # Engines committed via mandatory signal overrides (e.g., OpenSearch for text_search)
    mandatory_committed_engines: set[str] = set()
    for qa in query_assignments:
        if qa["query_id"] in mandatory_query_ids:
            mandatory_committed_engines.add(qa["assigned_engine"])

    # Queries that never move: the signal overrides above (ADR #296).
    fixed_query_ids = mandatory_query_ids

    # Re-run the resolver once, with a trace attached, so Phase 1 and Phase
    # 2 read the IDENTICAL per-query scores (#502 review: Phase 1 judging
    # engines by the reality check's own, separate scorer, rather than
    # this same trace, is exactly what produced results the scores did not
    # support). Pure and deterministic on the exact artifacts that produced
    # ``query_assignments`` -- see ``tests/e2e/routing_snapshot.py`` for the
    # same technique and its own consistency guard.
    from src.agents.referee.assignment_resolver import AssignmentResolver, RoutingTrace

    # ADR-031 Decision 9: a placeholder engine is never a candidate, absorber or owner.
    analysis_outputs = without_placeholder_engines(analysis_outputs)
    kept_by_customer = frozenset(customer_kept_engines) & set(analysis_outputs)
    _trace = RoutingTrace()
    sources = (
        dict(sources_of_truth)
        if sources_of_truth is not None
        else assignment_sources_of_truth(assignment)
    )
    AssignmentResolver().resolve(
        triage,
        analysis_outputs,
        collector_output,
        routing_trace=_trace,
        sources_of_truth=sources,
    )
    trace_by_query = _trace.by_query
    # The one shared scoring context (#490/#549): the mandatory-engine
    # floor's absorber (``_find_best_absorber_for_query``) scores each
    # candidate engine directly with this (``score_query_for_engine``)
    # instead of reading ``trace_by_query`` -- one scoring call site, same
    # as ``routed_confidence.py``, rather than a second way of reaching the
    # identical number.
    scoring_ctx = _scoring_context(analysis_outputs, triage, collector_output, sources)
    # The resolver's OWN settled-workload snapshot (#503), persisted on the
    # trace rather than recomputed here from
    # the final v1 assignment: the resolver's snapshot is taken before the
    # Aurora fallback, the utility pin and signal overrides fill in more
    # queries, so recomputing it here from the fuller v1 picture could make
    # an otherwise-unchanged tie re-pick between the same two engines for no
    # real reason. Falls back to a v1-based recompute only for an assignment
    # produced before this was persisted (``_trace.settled_counts`` empty
    # with no tie to explain it).
    settled_counts: dict[str, int] = _trace.settled_counts or dict(
        Counter(
            qa["assigned_engine"]
            for qa in query_assignments
            if qa["query_id"] not in tie_unresolved_qids and qa.get("assigned_engine")
        )
    )
    # Each query's engine as it stands before either phase runs -- stable
    # throughout both, since neither phase mutates ``query_assignments``
    # itself (Phase 2 works on a ``deepcopy``). Shared by both phases'
    # group-aware eligibility checks (``_group_eligible_engines``, #502
    # review round 5, finding G) so a co-dependency group's members are
    # always looked up by the SAME current engine, even when they are
    # native to a different engine than the one being evaluated for removal.
    current_engine_by_qid = {qa["query_id"]: qa["assigned_engine"] for qa in query_assignments}

    # -------------------------------------------------------------------
    # Phase 1 (#502 review): settle which engines earn a place, judged on
    # each engine's own native queries alone, order-independent, repeated
    # to a fixed point. No engine is protected for being cheapest or
    # "primary" -- the only unconditional protections are a mandatory
    # signal override (ADR #296) and a query (or co-dependency group) no
    # other committed engine can serve at all, both handled inside
    # ``_evaluate_engine_removal`` itself.
    # -------------------------------------------------------------------
    max_rounds = len(engine_queries) + 2
    # Computed here, before Phase 1, so Phase 1's own destination tie-break
    # (``_removal_tiebreak_key``, #569) resolves a tie the same ADR-031
    # Decision 2 way Phase 2 and the absorber already do -- source-compatible
    # engine first, never left to pick whichever alternate a Python set
    # happened to iterate first.
    source_compatible_engine = SOURCE_ENGINE_TO_AURORA.get(resolved_source_engine)
    committed_engines, unique_value_assessment = _settle_committed_engines(
        engine_queries,
        fixed_query_ids,
        mandatory_committed_engines,
        trace_by_query,
        current_engine_by_qid,
        query_signals,
        query_map,
        query_capabilities,
        max_rounds,
        tied_engines_by_qid,
        source_compatible_engine,
        settled_counts,
        customer_kept_engines=kept_by_customer,
        cost_model=scoring_ctx.cost_model,
        source_engine=resolved_source_engine,
    )

    # -------------------------------------------------------------------
    # Phase 2 (#502 review): place every query on its highest-scoring
    # engine within the settled set, using the SAME resolver scores and
    # the SAME eligibility rule Phase 1 used (ADR-031) -- never a query
    # ends on an engine Phase 1 removed, because placement happens once,
    # fresh, against the final set, not incrementally against a list that
    # can go stale.
    # -------------------------------------------------------------------
    revised, consolidations = _place_settled_queries(
        query_assignments,
        committed_engines,
        fixed_query_ids,
        current_engine_by_qid,
        query_signals,
        query_map,
        query_capabilities,
        trace_by_query,
        source_compatible_engine,
        workload_justified,
        tie_unresolved_qids,
        settled_counts,
        tied_engines_by_qid,
        ledger=ledger,
        closed_destinations=closed_to_pinned_search(
            query_assignments, query_map, kept_by_customer, resolved_source_engine
        ),
        source_engine=resolved_source_engine,
    )

    # Mandatory-engine justification floor (#326, #167); see reality_check_floors.
    _apply_justification_floors(
        revised=revised,
        consolidations=consolidations,
        unique_value_assessment=unique_value_assessment,
        committed_engines=committed_engines,
        engine_queries=engine_queries,
        mandatory_committed_engines=mandatory_committed_engines,
        fixed_query_ids=fixed_query_ids,
        total_queries=len(query_assignments),
        query_map=query_map,
        resolved_source_engine=resolved_source_engine,
        trace_by_query=trace_by_query,
        current_engine_by_qid=current_engine_by_qid,
        query_signals=query_signals,
        query_capabilities=query_capabilities,
        source_compatible_engine=source_compatible_engine,
        settled_counts=settled_counts,
        scoring_ctx=scoring_ctx,
        workload_justified=workload_justified,
        ledger=ledger,
        customer_kept_engines=kept_by_customer,
    )

    # Reconcile before Pass 3/4 (#475 review follow-up): this floor now runs
    # for OpenSearch whenever it is committed, not only when it is
    # "mandatory" (see above), so a query Pass 2 just placed on OpenSearch
    # can be moved AGAIN by this same floor within this one call -- a
    # two-hop move (e.g. aurora_mysql -> opensearch -> dynamodb) that no
    # single consolidation record describes, which left a stale, orphaned
    # "aurora_mysql -> opensearch" record ``moved_queries()``
    # (``reality_check_request.py``) could never resolve back to any
    # query (0 found for its claimed count). ``consolidations_from_moves``
    # fits the records to the net moves the ledger replays to (#218, #616:
    # ``query_assignments`` replayed with this check's Moves is ``revised``).
    # The handler's ``_settle_records`` derives them again later, but only on
    # the ``none``/``bedrock`` paths that reach it before writing; ``external``
    # mode writes its LLM request from this function's own return value
    # first. Deriving here as well is idempotent (the derived records fed
    # back in give the same records) and makes every mode see the same ones.
    consolidations = consolidations_from_moves(
        query_assignments,
        ledger.moves,
        consolidations,
        unique_value_assessment,
        _engine_infra_cost(analysis_outputs),
        engines_in_use=engines_in_use(collector_output),
    )

    # Pass 3: Detect architectural patterns
    patterns = _detect_architectural_patterns(revised, committed_engines, engine_queries)

    # Pass 4: Build recommendations
    recommendations = _build_recommendations(
        revised,
        consolidations,
        patterns,
        engine_queries,
        _engine_infra_cost(analysis_outputs),
        unique_value_assessment,
        engines_in_use=engines_in_use(collector_output),
    )

    return {
        "revised_assignments": revised,
        "consolidations": consolidations,
        "architectural_patterns": patterns,
        "recommendations": recommendations,
        "unique_value_assessment": unique_value_assessment,
        "move_ledger": ledger,
        # The resolver's settled-workload snapshot this check's ties used
        # (ADR-031 Decision 12), for the later steps to reuse (#746).
        "settled_counts": dict(settled_counts),
    }
