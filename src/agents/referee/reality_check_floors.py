"""Reality check justification floors: a mandatory engine must earn its cost.

A signal override keeps an engine through Phase 1 and Phase 2. The floors are
the last gate: OpenSearch needs real search depth plus traffic or result size
(#326), and any other mandatory engine must serve a share of the workload in
proportion to its fixed cost (#167). Queries that fail a floor move to their
best remaining engine (``_find_best_absorber_for_query``).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from src.agents.referee.move_ledger import MoveLedger, apply_to_assignment, best_other, ledger_for
from src.agents.referee.reality_check_records import _queries, engine_changes
from src.agents.referee.reality_check_rules import (
    AURORA_ENGINES,
    _group_eligible_engines,
    _move_scores_text,
    _moving_group_scores,
    _moving_members,
    _query_has_join,
    _resolver_score,
    _tie_unresolved_note,
    _trace_ladder,
    engine_base_cost,
    may_absorb,
    tie_reason_code,
)
from src.agents.referee.tie_break import break_tie
from src.contracts.move_ledger import EngineScore, MoveEvidence, MovePass, ReasonCode
from src.shared import catalog
from src.shared.engine_names import SOURCE_ENGINE_DISPLAY_NAMES, display_engine
from src.tools.sql.query_facts import query_facts

if TYPE_CHECKING:
    from src.agents.referee.query_scoring import ScoringContext

# ---------------------------------------------------------------------------
# Cost-per-query floor for a mandatory engine (#167)
#
# A signal override makes an engine "mandatory" (Pass 0 never asks whether it
# has unique value), but that protection should not be permanent: if the
# engine's fixed monthly cost is high and the share of the workload it serves
# is tiny, it has not earned its place either, unless a capability (#338) or
# a dedicated justification (#326, OpenSearch's own floor) says otherwise.
# ---------------------------------------------------------------------------

# An engine's fixed monthly cost (ENGINE_BASE_COST) must be at least this much
# to be worth questioning at all -- DynamoDB ($0) and ElastiCache ($50) are
# cheap enough that a small share is not disproportionate.
MANDATORY_ENGINE_COST_FLOOR = catalog.threshold("mandatory_engine_cost_floor")

# Share of in-scope queries (0-1) below which a mandatory engine's fixed cost
# counts as disproportionate to the workload it serves.
MANDATORY_ENGINE_MAX_QUERY_SHARE = catalog.threshold("mandatory_engine_max_query_share")


def _mandatory_cost_share_reason(engine: str, qas: list[dict], total_queries: int) -> str | None:
    """None if ``engine``'s fixed cost is proportionate to its workload share (#167).

    Otherwise, the reason a mandatory signal override does not excuse it from
    the operational-burden review.
    """
    cost = engine_base_cost(engine)
    share = len(qas) / total_queries if total_queries else 0.0
    if cost >= MANDATORY_ENGINE_COST_FLOOR and share < MANDATORY_ENGINE_MAX_QUERY_SHARE:
        return (
            f"{display_engine(engine)} serves {len(qas)} of {total_queries} queries "
            f"({share:.1%}) at a fixed ${cost:.0f}/mo, too much for the workload "
            "share it serves"
        )
    return None


# ---------------------------------------------------------------------------
# OpenSearch justification floor (#326)
#
# A text_search signal override alone keeps OpenSearch "mandatory" everywhere
# else in the reality check, which is exactly how 4 LIKE/prefix admin-search queries
# at under 1 call/s kept a $150+/mo standing domain in a maintainer's run: the
# signal made the engine mandatory, so Pass 0 never asked whether it earns its
# fixed cost. This floor runs before Pass 0 so a signal override is never
# enough on its own -- OpenSearch also needs real search depth and either
# traffic to justify its cost, or a search pattern the source engine's own
# text_search_basic cannot already serve. Mirrors the ElastiCache hot-read
# floor (#304, cache_overlay.HOT_READ_MIN_CALLS_PER_SECOND).
# ---------------------------------------------------------------------------

# Minimum combined traffic (calls/s) across every query OpenSearch would own,
# before its fixed monthly cost (ENGINE_BASE_COST["opensearch"]) is worth a
# standing domain. 1.0 is the ElastiCache hot-read floor's own value
# (cache_overlay.HOT_READ_MIN_CALLS_PER_SECOND): below one call/s, a cached
# entry (or a search index) is read a few times before the next miss pays the
# same cost as never having it, so one call/s is the lowest traffic worth a
# dedicated store at all. On the wordpress reference sample, the 4 queries
# OpenSearch was recommended for totalled well under 1 call/s combined.
OPENSEARCH_MIN_CALLS_PER_SECOND = catalog.engine_threshold("opensearch", "min_calls_per_second")

# Source database engines whose own text search (MySQL FULLTEXT via
# MATCH...AGAINST, Postgres tsvector/pg_trgm) already covers these patterns
# for free -- the Aurora engines' native, basic full-text support. A
# dedicated OpenSearch domain must clear a higher traffic bar than
# OPENSEARCH_MIN_CALLS_PER_SECOND to be worth it anyway.
NATIVE_TEXT_SEARCH_SOURCE_ENGINES = frozenset({"mysql", "mariadb", "postgresql", "postgres"})

# The traffic floor a native-text-search source engine's queries must clear
# before OpenSearch is justified despite the source already covering the
# pattern. 10x the base floor: the source engine's own index is free (already
# paid for by the primary cluster), so a second, dedicated search engine only
# earns its $150+/mo on top of that if the traffic is an order of magnitude
# higher, not merely above the floor that justifies a search engine starting
# from nothing.
OPENSEARCH_NATIVE_OVERRIDE_MIN_CALLS_PER_SECOND = catalog.engine_threshold(
    "opensearch", "native_override_min_calls_per_second"
)

# A query returning this many rows on average, even below the traffic floor,
# signals a corpus large enough that a dedicated index can still earn its
# cost (e.g. a low-traffic but very large full-text search over millions of
# rows). This is a result-size proxy, not a corpus-size one: the collector
# gives no table-level document-count stat, only rows_returned_avg (how much
# a query's own result set averages), which is the only per-query signal
# available -- a query that returns a small page from a huge corpus looks
# the same as one that returns everything from a small one.
OPENSEARCH_MIN_CORPUS_ROWS_AVG = catalog.engine_threshold("opensearch", "min_corpus_rows_avg")


def _query_has_search_depth(query_text: str | None, source_engine: str | None = None) -> bool:
    """Real search depth in one query (#326 finding 4): the engine-neutral
    ``search_depth`` query fact (``src.tools.sql.query_facts``, #491) -- a
    search predicate plus relevance ranking, fuzzy matching, or a facet
    (GROUP BY alongside the same predicate) -- never a bare GROUP BY, a plain
    LIKE/prefix filter, a plain join, or to_tsvector()/pg_trgm
    document/extension setup with no match operator.
    """
    return query_facts(query_text, source_engine).search_depth


def closed_to_pinned_search(
    query_assignments: list[dict],
    query_map: dict[str, dict],
    kept_by_customer: Iterable[str],
    source_engine: str | None = None,
) -> dict[str, set[str]]:
    """The queries that may not join an OpenSearch a customer pin keeps (#860).

    The pin pays OpenSearch's standing cost, not a query's sync scope: a query
    with no real search depth fails the per-query #326 check whatever the
    cost, so Phase 2 never pulls it onto the pinned read model (this floor
    would only move it off again).
    """
    engine = "opensearch"
    if engine not in set(kept_by_customer):
        return {}
    return {
        engine: {
            qa["query_id"]
            for qa in query_assignments
            if qa["assigned_engine"] != engine
            and not _query_has_search_depth(
                query_map.get(qa["query_id"], {}).get("query_text"), source_engine
            )
        }
    }


def opensearch_justification(
    qas: list[dict],
    query_map: dict[str, dict],
    source_engine: str | None,
    *,
    standing_cost_paid: bool = False,
) -> dict[str, tuple[bool, str]]:
    """Per-query verdict on whether OpenSearch earns its place for each query
    it owns (#326, review of #375 finding B1).

    A query with no real search depth never earns a place on its own,
    regardless of traffic -- it is judged, and reported, independently of
    whatever else OpenSearch owns (finding 4: one shallow query must not
    sink a real search sitting next to it, and the reverse: one deep query
    must not shield a shallow one). The traffic/corpus floor is then judged
    only across the queries that *do* show real depth, so a key lookup
    alongside a genuine `@@ plainto_tsquery` search at real traffic no
    longer drags that search's own floor down (or the lookup's lack of
    traffic up).

    ``standing_cost_paid`` (#860): a customer pin keeps OpenSearch in the plan,
    so its standing domain is paid whatever the traffic; the traffic/corpus
    floor, which asks whether the workload pays for that domain, is skipped
    and every query with real search depth is justified. The per-query depth
    check still applies: a query with none gains nothing from the read model.

    Returns ``{query_id: (justified, reason)}``; ``reason`` always explains
    the decision so the deliverables can state why.
    """
    texts = {qa["query_id"]: query_map.get(qa["query_id"], {}).get("query_text") for qa in qas}
    deep_qids = {qid for qid, t in texts.items() if _query_has_search_depth(t, source_engine)}

    shallow_reason = (
        "no search predicate with relevance ranking, fuzzy matching or a facet in this query"
    )
    verdicts: dict[str, tuple[bool, str]] = {
        qa["query_id"]: (False, shallow_reason) for qa in qas if qa["query_id"] not in deep_qids
    }
    if not deep_qids:
        return verdicts
    if standing_cost_paid:
        paid = (
            f"{len(deep_qids)} queries need relevance ranking/fuzzy matching/facets, on an "
            "OpenSearch domain the customer's own choice keeps"
        )
        verdicts.update({qid: (True, paid) for qid in deep_qids})
        return verdicts

    deep_rows = {qid: query_map.get(qid, {}) for qid in deep_qids}
    total_cps = sum(float(q.get("calls_per_second") or 0) for q in deep_rows.values())
    total_rows_avg = sum(float(q.get("rows_returned_avg") or 0) for q in deep_rows.values())
    native = (source_engine or "").lower() in NATIVE_TEXT_SEARCH_SOURCE_ENGINES
    floor = (
        OPENSEARCH_NATIVE_OVERRIDE_MIN_CALLS_PER_SECOND
        if native
        else OPENSEARCH_MIN_CALLS_PER_SECOND
    )

    if total_cps < floor and total_rows_avg < OPENSEARCH_MIN_CORPUS_ROWS_AVG:
        source_name = SOURCE_ENGINE_DISPLAY_NAMES.get(
            (source_engine or "").lower(), source_engine or ""
        )
        native_note = (
            f"{source_name} already supports this search pattern natively (text_search_basic), so "
            if native
            else ""
        )
        reason = (
            f"{native_note}combined traffic {total_cps:.2f} calls/s across "
            f"{len(deep_qids)} queries with real search depth is below the {floor:g} calls/s "
            f"floor (and the {total_rows_avg:.0f}-row average result size is below the "
            f"{OPENSEARCH_MIN_CORPUS_ROWS_AVG:g}-row floor) for a "
            f"${engine_base_cost('opensearch'):.0f}/mo standing domain"
        )
        for qid in deep_qids:
            verdicts[qid] = (False, reason)
        return verdicts

    reason = (
        f"{len(deep_qids)} queries need relevance ranking/fuzzy matching/facets at "
        f"{total_cps:.2f} calls/s (average result size {total_rows_avg:.0f} rows), clearing "
        f"the {floor:g} calls/s floor or the {OPENSEARCH_MIN_CORPUS_ROWS_AVG:g}-row floor"
    )
    for qid in deep_qids:
        verdicts[qid] = (True, reason)
    return verdicts


def _workload_ever_justified(
    engine: str,
    first_assignment_qas: list[dict],
    query_map: dict[str, dict],
    source_engine: str | None,
) -> bool | None:
    """Whether ``engine``'s FIRST-ASSIGNMENT workload ever earned its place (#475 review).

    ``None`` when no justification floor applies to ``engine`` at all (only
    OpenSearch has one, #326) -- the caller must not claim or deny a saving
    based on this when it's ``None``. ``True`` when at least one of the
    engine's own first-assignment queries clears ``opensearch_justification``
    (real search depth plus traffic or corpus size); ``False`` when none do.

    Judged once, here, against the engine's queries *before* any reality
    check pass moves anything -- a later pass fully consolidating this
    engine away is removing something that was never justified by the
    workload in the first place, not something that earned its place and
    then stopped being worth its cost. ``_build_recommendations`` reads this
    (carried on each consolidation record) to avoid presenting that removal
    as an infrastructure saving.
    """
    if engine != "opensearch" or not first_assignment_qas:
        return None
    verdicts = opensearch_justification(first_assignment_qas, query_map, source_engine)
    return any(justified for justified, _ in verdicts.values())


@dataclass(frozen=True)
class _FloorMove:
    """What one query's floor move records in the ledger (#616)."""

    tied: list[str]
    score_before: float | None
    score_after: float | None
    runner_up: EngineScore | None
    group_scores: Mapping[str, float] = field(default_factory=dict)
    group_key: str | None = None
    tie_rule: ReasonCode | None = None


def _apply_justification_floors(
    *,
    revised: list[dict],
    consolidations: list[dict],
    unique_value_assessment: dict[str, dict],
    committed_engines: set[str],
    engine_queries: dict[str, list[dict]],
    mandatory_committed_engines: set[str],
    fixed_query_ids: set[str],
    total_queries: int,
    query_map: dict[str, dict],
    resolved_source_engine: str | None,
    trace_by_query: dict[str, dict],
    current_engine_by_qid: dict[str, str],
    query_signals: dict[str, list[str]],
    query_capabilities: dict[str, list[str]] | None,
    source_compatible_engine: str | None,
    settled_counts: Mapping[str, int],
    scoring_ctx: ScoringContext,
    workload_justified: dict[str, bool | None],
    ledger: MoveLedger | None = None,
    customer_kept_engines: frozenset[str] = frozenset(),
) -> None:
    """Run the justification floors on ``revised`` after Phase 2.

    An engine in ``customer_kept_engines`` holds a customer pin, so it stays in
    the plan and its standing cost is paid whatever its workload (#860, the
    same reason the sanity sweep keeps it, #844): the floors that ask whether
    the workload pays for the engine -- the cost-share floor (#167) and
    OpenSearch's traffic/corpus floor (#326) -- do not run for it. OpenSearch's
    per-query search-depth check does: a query with no real search depth still
    leaves a pinned OpenSearch (``opensearch_justification``'s
    ``standing_cost_paid``).

    Mutates ``revised``, ``consolidations``, ``unique_value_assessment``,
    ``committed_engines`` and ``engine_queries`` in place. Every query a floor
    moves goes through ``move_ledger.apply_to_assignment`` on ``ledger`` (#616;
    a fresh one from ``revised`` when ``None``): pass ``floor``, rule
    ``floor.opensearch`` or ``floor.cost_share``, with the same before and
    after scores the query's reason reports.
    """
    if ledger is None:
        ledger = ledger_for(revised)
    # -------------------------------------------------------------------
    # Mandatory-engine justification floor (#326, #167)
    #
    # A signal override makes an engine "mandatory" through Phase 1 and
    # Phase 2, which never move its queries -- that is how a $150+/mo
    # OpenSearch domain kept 4 LIKE/prefix admin queries at under 1 call/s in
    # a maintainer's run. This is the last gate:
    # a mandatory engine that still owns only its mandatory queries, and does
    # not meet its justification floor, is not exempt forever. OpenSearch
    # gets the dedicated floor from #326 (traffic, search depth, native
    # source support); any other mandatory engine is judged on cost share
    # alone (#167).
    #
    # OpenSearch's own floor runs whenever it is committed at all, not only
    # when a signal override made it "mandatory" (#475 review, finding 4
    # follow-up, measured on wordpress): once OpenSearch's own matched-pattern
    # score (finding 4's fix) lets it win ownership through Pass 0/2's
    # score-based consolidation alone -- no signal override involved -- this
    # loop would otherwise never run for it at all, reopening the exact gap
    # #475 closed by keeping that score low in the first place. A query that
    # reaches OpenSearch on fit score alone must still clear the same #326
    # bar as one that reaches it through the signal override; every other
    # mandatory engine's cost-share floor (#167) is unaffected and still only
    # runs for an actual signal override.
    # -------------------------------------------------------------------
    # A pinned engine's cost is paid (#860): only OpenSearch's depth check stays.
    floor_scope = (mandatory_committed_engines - customer_kept_engines) | (
        {"opensearch"} & committed_engines
    )
    for engine in sorted(floor_scope & committed_engines):
        # Grouped by actual CURRENT ownership, not by any one signal (#475
        # review, suggestion 1) -- deliberately different from the
        # resolver's own ``opensearch_justification`` call
        # (``assignment_resolver._build_signal_overrides``), which groups by
        # the signal's own candidate queries before any assignment exists.
        # This floor asks a later question: does the engine's workload AS IT
        # STANDS NOW (after absorption may have added or Pass 0/2 may have
        # removed queries) still earn its place -- so it has to look at
        # everything the engine currently owns, not just how it first got
        # there.
        current_qas = [qa for qa in revised if qa["assigned_engine"] == engine]
        if not current_qas:
            continue

        # verdicts: per-query (justified, reason) for opensearch (finding B1 --
        # one deep query must not be sunk by a shallow one sitting next to it,
        # and vice versa); a single engine-wide (justified, reason) for any
        # other mandatory engine, applied to every one of its queries.
        if engine == "opensearch":
            verdicts = opensearch_justification(
                current_qas,
                query_map,
                resolved_source_engine,
                standing_cost_paid=engine in customer_kept_engines,
            )
        else:
            cost_reason = _mandatory_cost_share_reason(engine, current_qas, total_queries)
            verdicts = {
                qa["query_id"]: (cost_reason is None, cost_reason or "") for qa in current_qas
            }
        to_move_qas = [qa for qa in current_qas if not verdicts[qa["query_id"]][0]]
        if not to_move_qas:
            continue

        floor_candidates = {e for e in committed_engines if e != engine}
        if not floor_candidates:
            continue  # nowhere to send them (#335): leave the engine as owner

        # (query assignment, fit reason, whether the reason already carries the
        # before and after scores -- true for a group move, false for an individual one)
        floor_placement: dict[str, list[tuple[dict, str, bool]]] = defaultdict(list)
        # The Move each placed query records (#616): its tie, scores and runner-up.
        floor_moves: dict[str, _FloorMove] = {}
        floor_unplaced: list[dict] = []
        to_move_by_id = {qa["query_id"]: qa for qa in to_move_qas}
        handled_floor_groups: set[str] = set()
        for qa in to_move_qas:
            qid = qa["query_id"]
            group_info = trace_by_query.get(qid, {}).get("group")
            if group_info:
                # #502 review round 6, finding 3: route a grouped query
                # through ``_group_eligible_engines`` too, so a
                # co-dependency group fails the floor and moves as ONE
                # unit here, the same rule Phase 1 and Phase 2 already
                # apply -- never split by picking each member's own best
                # absorber independently.
                group_key = group_info["key"]
                if group_key in handled_floor_groups:
                    continue
                handled_floor_groups.add(group_key)
                # The members this check holds (a customer-pinned one is
                # held out, #860/#882); the floor moves the mandatory ones
                # too, so none stays put here (#894: the same member set
                # Phase 1 and 2 use, minus their mandatory exclusion).
                members = _moving_members(qid, trace_by_query, (), current_engine_by_qid)
                member_qas = [to_move_by_id[m] for m in members if m in to_move_by_id]
                if len(member_qas) != len(members):
                    # Not every member of this group is failing the floor
                    # on ``engine`` right now -- moving only part of a
                    # group is never allowed, so leave the whole group
                    # where it is rather than split it.
                    floor_unplaced.extend(member_qas)
                    continue
                # ADR-031 Decision 4, Amendment 4 (#727): only an engine
                # that can serve every member may take the group. The floor
                # moves the mandatory members themselves, so they count here
                # (Phase 1 and 2 leave a mandatory member out because it
                # stays put).
                # A tied member counts every tied engine as current
                # (ADR-031 Decision 12, paragraph 4), as Phase 1 and 2 do.
                eligible = _group_eligible_engines(
                    qid,
                    trace_by_query,
                    current_engine_by_qid,
                    floor_candidates,
                    fixed_query_ids - set(members),
                    query_signals,
                    query_map,
                    query_capabilities,
                    {
                        m: list(to_move_by_id[m]["tied_engines"])
                        for m in members
                        if to_move_by_id[m].get("tied_engines")
                    },
                )
                # The group's mean over its moving members (#894): the same
                # numbers Phase 1 and 2 judge it on (Decision 7), never one
                # member's own score.
                group_scores = _moving_group_scores(members, sorted(eligible), trace_by_query)
                if not group_scores:
                    floor_unplaced.extend(member_qas)
                    continue
                top_group_score = max(group_scores.values())
                tied_group = sorted(e for e, s in group_scores.items() if s == top_group_score)
                tie_note = ""
                tie_kind: str | None = None
                if len(tied_group) == 1:
                    winner = tied_group[0]
                else:
                    # No ADR-031 rule prefers one engine over another by name
                    # or by how many queries it already has (#503): the same
                    # ``break_tie`` the resolver and ``_pick_winner`` use.
                    # Sorted member ids (#503): a stable seed, not whichever
                    # member happens to come first in ``trace_by_query``'s
                    # own iteration order.
                    winner, tie_kind = break_tie(
                        tied_group,
                        set(),
                        source_compatible_engine,
                        settled_counts,
                        ",".join(members),
                    )
                    tie_note = (
                        _tie_unresolved_note(tied_group, winner)
                        if tie_kind == "tie_unresolved"
                        else " (tie: source-compatible engine)"
                    )
                winner_fit = round(group_scores[winner])
                # The group's score on the engine it leaves (the Move's
                # ``score_before``): its mean there, none when that engine
                # cannot serve every member.
                before = _moving_group_scores(members, [engine], trace_by_query).get(engine)
                reason = (
                    f"fit={winner_fit}, co-dependency group moves together; "
                    f"{_move_scores_text(engine, before, winner, group_scores[winner])}"
                    f"{tie_note}"
                )
                # #503: a tie this floor pass resolves (or decides for real)
                # is recorded, same as Phase 2 (set with the Move, below).
                group_move = _FloorMove(
                    tied=tied_group if tie_kind == "tie_unresolved" else [],
                    score_before=before,
                    score_after=group_scores[winner],
                    # The group's winner is the top group score, so the runner-up
                    # is the next one by the same ranking.
                    runner_up=best_other(group_scores, winner),
                    group_scores=group_scores,
                    group_key=group_key,
                    tie_rule=(
                        tie_reason_code(tie_kind, tied_group, settled_counts, ",".join(members))
                        if tie_kind is not None
                        else None
                    ),
                )
                for member_qa in member_qas:
                    floor_placement[winner].append((member_qa, reason, True))
                    floor_moves[member_qa["query_id"]] = group_move
                continue

            absorber = _find_best_absorber_for_query(
                qa,
                floor_candidates,
                engine,
                query_signals,
                query_map,
                engine_queries,
                set(),
                "",
                scoring_ctx,
                query_capabilities,
                source_compatible_engine,
                settled_counts,
            )
            if absorber is None:
                floor_unplaced.append(qa)
                continue
            target = absorber["target_engine"]
            floor_moves[qid] = _FloorMove(
                tied=absorber.get("tied", []),
                score_before=_resolver_score(trace_by_query, qid, engine),
                score_after=_resolver_score(trace_by_query, qid, target),
                runner_up=absorber.get("runner_up"),
                tie_rule=absorber.get("tie_rule"),
            )
            floor_placement[target].append((qa, absorber["reason"], False))

        moved_ids = {qa["query_id"] for placed in floor_placement.values() for qa, _, _ in placed}
        if not moved_ids:
            # #502 review round 6, finding 2: every query that failed the
            # floor also had no eligible destination (``floor_unplaced``
            # covers all of ``to_move_qas``) -- record WHY the engine is
            # kept rather than silently leaving it unchanged with no trace
            # of this gate having run at all.
            if floor_unplaced:
                assessment = unique_value_assessment.setdefault(engine, {})
                assessment["floor_unplaced_queries"] = sorted(
                    qa["query_id"] for qa in floor_unplaced
                )
                n = len(floor_unplaced)
                assessment["floor_unplaced_reason"] = (
                    f"kept: no eligible engine can serve {n} "
                    f"quer{'y' if n == 1 else 'ies'} that failed "
                    f"{display_engine(engine)}'s justification floor"
                )
            continue

        # This engine's floor Moves start here; its records count them (#616).
        first_move = len(ledger.moves)
        for target_engine, placed in floor_placement.items():
            for qa, fit_reason, has_scores in placed:
                _, this_reason = verdicts[qa["query_id"]]
                # ADR-031 Decision 7 (#698): before and after scores, from the
                # resolver's own first-assignment scores. A grouped move
                # already carries its group scores in ``fit_reason``.
                if not has_scores:
                    scores_text = _move_scores_text(
                        engine,
                        _resolver_score(trace_by_query, qa["query_id"], engine),
                        target_engine,
                        _resolver_score(trace_by_query, qa["query_id"], target_engine),
                    )
                    fit_reason = f"{fit_reason}; {scores_text}"
                for rqa in revised:
                    if rqa["query_id"] == qa["query_id"]:
                        floor_move = floor_moves[qa["query_id"]]
                        apply_to_assignment(
                            ledger,
                            rqa,
                            target_engine,
                            pass_=MovePass.FLOOR,
                            reason_code=(
                                ReasonCode.FLOOR_OPENSEARCH
                                if engine == "opensearch"
                                else ReasonCode.FLOOR_COST_SHARE
                            ),
                            score_before=floor_move.score_before,
                            score_after=floor_move.score_after,
                            runner_up=floor_move.runner_up,
                            evidence=MoveEvidence(
                                candidates=_trace_ladder(trace_by_query, qa["query_id"]),
                                group_scores=dict(floor_move.group_scores),
                                tie_rule=floor_move.tie_rule,
                            ),
                            group_key=floor_move.group_key,
                            tied_engines=floor_move.tied,
                        )
                        rqa["assignment_reason"] = (
                            f"reality check: consolidated from {display_engine(engine)} → "
                            f"{display_engine(target_engine)} (justification floor: "
                            f"{this_reason}; {fit_reason})"
                        )
                        rqa.pop("signal_override", None)

        is_full = len(moved_ids) == len(current_qas)
        # One combined reason for the consolidation record: the distinct
        # per-query reasons among the queries this pass actually moved, not
        # every reason seen across the whole (possibly larger, partly kept)
        # engine -- a query that stayed never contributes text here.
        moved_reasons = sorted(
            {verdicts[qa["query_id"]][1] for qa in to_move_qas if qa["query_id"] in moved_ids}
        )
        floor_reason = "; ".join(moved_reasons)
        floor_entry_reason = (
            f"{display_engine(engine)} does not meet its justification floor: {floor_reason}"
        )
        floor_changes = engine_changes(ledger.moves[first_move:])
        for target_engine in floor_placement:
            moved_here = len(floor_changes.get((engine, target_engine), []))
            # moved_queries() (reality_check_request.py) recomputes membership
            # from (from_engine, to_engine) alone, so two consolidation records
            # for the same pair would double-count the same queries. Merge into
            # an existing record for this pair instead of adding a second one
            # -- but only append this pass's own reason, scoped to the queries
            # it actually moved, never re-describing a record (or a share of
            # one) this pass had no part in (review of #375, finding 5).
            existing = next(
                (
                    c
                    for c in consolidations
                    if c["from_engine"] == engine and c["to_engine"] == target_engine
                ),
                None,
            )
            if existing is not None:
                existing["query_count"] += moved_here
                existing["reason"] = (
                    f"{existing['reason']} | {_queries(moved_here)} of these also move "
                    f"because {floor_entry_reason}"
                )
                continue
            consolidations.append(
                {
                    "from_engine": engine,
                    "to_engine": target_engine,
                    "query_count": moved_here,
                    "reason": floor_entry_reason,
                    # The saving is set by consolidations_from_moves (#793).
                    "saved_cost_estimate": 0,
                    "action": "full" if is_full else "partial",
                    "queries_retained": (
                        [qa["query_id"] for qa in floor_unplaced] if floor_unplaced else []
                    ),
                    "retention_reason": (
                        "Queries require capabilities no other committed engine provides"
                        if floor_unplaced
                        else None
                    ),
                    "workload_justified": workload_justified.get(engine),
                }
            )
        assessment = unique_value_assessment.setdefault(engine, {})
        assessment["cost_justification"] = floor_reason
        assessment["cost_justification_query_ids"] = sorted(moved_ids)
        if is_full:
            committed_engines.discard(engine)
            engine_queries[engine] = [
                qa for qa in engine_queries.get(engine, []) if qa["query_id"] not in moved_ids
            ]


def _find_best_absorber_for_query(
    qa: dict,
    committed_engines: set[str],
    source_engine: str,
    query_signals: dict[str, list[str]],
    query_map: dict[str, dict],
    engine_queries: dict[str, list[dict]],
    mandatory_committed_engines: set[str],
    primary_engine: str,
    ctx: ScoringContext,
    query_capabilities: dict[str, list[str]] | None = None,
    source_compatible_engine: str | None = None,
    settled_counts: Mapping[str, int] | None = None,
) -> dict | None:
    """Find the best committed engine to absorb a single query.

    Ranks by: the resolver's own score (highest wins,
    ``query_scoring.score_query_for_engine`` against the one shared ``ctx``
    -- #502 review round 5, finding N3: the mandatory-engine justification
    floor, the only caller of this function, used the reality check's own
    ``_engine_fit_score`` here, a second scorer disagreeing with the one
    Phase 1 and Phase 2 already use; #490/#549 then removed every other
    separate scorer in the reality check too, so every reality-check decision now
    reads the one shared score), with mandatory secondary
    engines getting a tiebreaker bonus (data already flowing via sync -- a
    real operational reason, not a preference of convenience).

    A tie that survives even that -- same fit score, same mandatory-
    secondary status -- is never decided by table overlap, how many
    queries the engine already has, or its name (#503): it goes through
    ``break_tie``, the same ADR-031 Decision 2 tie-break the resolver's
    ``break_owner_tie`` and ``reality_check_rules._pick_winner`` use, so the
    source-compatible engine wins first and anything still tied is
    reported as ``tie_unresolved`` in the returned reason, not silently
    picked.

    Respects the serviceability gate (``query_scoring.score_query_for_engine``'s
    own hard capability gate against ``ctx.query_capabilities``): only
    considers engines that can serve the query's hard requirements.

    No score floor (#502 review): a query being consolidated moves to its
    highest-scoring remaining engine that can serve it, even if that scores
    it a little lower than the engine it is leaving -- accepting a slightly
    worse fit in exchange for fewer engines is the point of this CTO review.
    """
    from src.agents.referee.query_scoring import score_query_for_engine

    qid = qa["query_id"]
    query = query_map.get(qid, qa)
    query_tables = query_map.get(qid, {}).get("tables_accessed", [])
    candidates: list[dict] = []
    # Every candidate's shared-scorer score, for the Move's runner-up (#616).
    candidate_scores: dict[str, float] = {}

    def _runner_up(winner_engine: str) -> EngineScore | None:
        """The best candidate other than the winner, by the winner's own ranking
        (rounded fit, then mandatory-secondary). A record only: among candidates
        of equal rank, the first by engine id is named so the record is stable."""
        others = [c for c in candidates if c["target_engine"] != winner_engine]
        if not others:
            return None
        best = min(others, key=lambda c: (_rank(c), c["target_engine"]))
        return EngineScore(
            engine=best["target_engine"], score=candidate_scores[best["target_engine"]]
        )

    for target_engine in sorted(committed_engines):
        if target_engine == source_engine:
            continue

        # Write gate (#296): a cache never owns a query, and an engine that is not
        # a system of record never owns a write; a specialist (OpenSearch) absorbs
        # only the queries its specialty serves
        if not may_absorb(target_engine, qid, query, query_signals, query_capabilities):
            continue

        # Review of #375, finding B2: a join consolidated away from a
        # non-Aurora engine (DocumentDB, OpenSearch) never lands on DynamoDB,
        # unless a denormalised design is explicitly proposed for it -- no
        # such proposal mechanism exists yet, so this is unconditional for
        # now. A join that was already on DynamoDB, or arrived there from
        # Aurora (a key-scoped 2-table join a v1 fit score chose on its own
        # merits), is unaffected; this only blocks a *consolidation* onto it.
        if (
            target_engine == "dynamodb"
            and source_engine not in AURORA_ENGINES
            and _query_has_join(query_map.get(qid, {}))
        ):
            continue

        # #502 review round 5, finding N3 / #490/#549: ranked by the
        # resolver's own score, not a second, separate scorer -- ``None``
        # means ``score_query_for_engine``'s own hard exclusion/capability
        # gate already ruled this engine out for this query (``.gate_kind``
        # names which, if a caller needs it), so there is nothing to absorb
        # it with.
        #
        # #490 review (round 2): this used to also drop a candidate scoring
        # exactly 0 unless the reality check's own, now-deleted
        # ``_can_engine_serve_query`` passed -- reachable (not dead code: a
        # round-1 review response claimed otherwise and was corrected) via
        # that function's *signal-gated* branch, which returned ``False``
        # outright whenever a triage signal demanded a capability the old
        # ``ENGINE_CAPABILITIES`` table didn't list for the candidate,
        # skipping its own basic-CRUD fallback entirely. That second check
        # is not restored, for each capability it could gate on:
        #   - ``complex_joins``: still excluded -- ``score_query_for_engine``
        #     gates it the same way today, through the real per-query
        #     ``complex_joins`` capability (``capability_registry``), not a
        #     private table.
        #   - ``referential_integrity``, ``transactions`` (dynamodb/
        #     documentdb), ``text_search`` (aurora, via ``text_pattern_match``):
        #     no longer excluded. None is a structural impossibility
        #     (ADR-031 Decision 2 Step 2); ``transactions`` hard-excluding
        #     DynamoDB was itself an ADR-031 Decision 3 violation in the old
        #     code (DynamoDB's transaction support is scored by shape, never
        #     excluded). Pinned by ``test_resolver_reality_check_agreement.
        #     py``'s ``TestAbsorptionBehaviorChanges`` for every case above,
        #     plus the general "a 0 that clears the real gate is a
        #     candidate, not an exclusion" rule.
        result = score_query_for_engine(query, target_engine, ctx)
        if result.score is None:
            continue
        fit = round(result.score)

        # Check table overlap with target
        target_tables = set()
        for tqa in engine_queries.get(target_engine, []):
            target_tables.update(query_map.get(tqa["query_id"], {}).get("tables_accessed", []))
        overlap = set(query_tables) & target_tables

        is_mandatory_secondary = (
            target_engine in mandatory_committed_engines and target_engine != primary_engine
        )

        reason = (
            f"fit={fit}, data synced for {', '.join(sorted(overlap))}"
            if overlap
            else f"fit={fit}, {display_engine(target_engine)} can serve this pattern"
        )

        candidate_scores[target_engine] = result.score
        candidates.append(
            {
                "target_engine": target_engine,
                "fit_score": fit,
                "is_mandatory_secondary": is_mandatory_secondary,
                "reason": reason,
            }
        )

    if not candidates:
        return None

    # Highest fit score wins, mandatory-secondary next (data already
    # synced, a real reason -- #502 review round 5, finding N3). A tie that
    # survives both is an ADR-031 Decision 2 tie (#503): resolved by
    # ``break_tie``, never by table overlap, how many queries the engine
    # already has, or its name.
    def _rank(c: dict) -> tuple[int, bool]:
        return (-c["fit_score"], not c["is_mandatory_secondary"])

    best_rank = min(_rank(c) for c in candidates)
    tied = [c for c in candidates if _rank(c) == best_rank]
    if len(tied) == 1:
        return {
            **tied[0],
            # The tie the move would leave (``tied``, not ``tied_engines``: this is
            # a candidate, not a query record; the floor's Move sets the query's).
            "tied": [],
            "runner_up": _runner_up(tied[0]["target_engine"]),
            "tie_rule": None,
        }

    tied_engine_names = [c["target_engine"] for c in tied]
    winner_engine, tie_kind = break_tie(
        tied_engine_names, set(), source_compatible_engine, settled_counts, qid
    )
    winner = next(c for c in tied if c["target_engine"] == winner_engine)
    tie_note = (
        _tie_unresolved_note(tied_engine_names, winner_engine)
        if tie_kind == "tie_unresolved"
        else " (tie: source-compatible engine)"
    )
    # #503: a tie the absorber itself resolves (or decides for real) is
    # recorded, same as Phase 1/2.
    return {
        **winner,
        "reason": f"{winner['reason']}{tie_note}",
        "tied": sorted(tied_engine_names) if tie_kind == "tie_unresolved" else [],
        "runner_up": _runner_up(winner_engine),
        "tie_rule": tie_reason_code(tie_kind, tied_engine_names, settled_counts, qid),
    }
