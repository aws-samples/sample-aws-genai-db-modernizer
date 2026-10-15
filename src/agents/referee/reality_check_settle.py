"""Reality check Phase 1: settle which engines earn a place (#502).

An engine is removed when the other committed engines can serve its queries,
judged on the resolver's own per-query scores, one engine per round, until a
round removes nothing.
"""

from __future__ import annotations

import warnings
from collections import defaultdict
from collections.abc import Iterable, Mapping

from src.agents.referee.reality_check_rules import (
    ENGINE_BASE_COST,
    EXTRA_ENGINE_BURDEN_MONTHLY,
    TARGET_ENGINE_COUNT,
    UNIQUE_DELTA_THRESHOLD,
    _group_eligible_engines,
    _moving_group_scores,
    _moving_members,
    _top_scorers,
)
from src.agents.referee.tie_break import break_tie, source_compatible_engine_survives


def _in_group(trace_by_query: dict[str, dict], qid: str) -> bool:
    """Whether the resolver recorded ``qid`` in a co-dependency group."""
    return bool(trace_by_query.get(qid, {}).get("group"))


def _evaluate_engine_removal(
    engine: str,
    native_qas: list[dict],
    other_committed: set[str],
    mandatory_query_ids: set[str],
    trace_by_query: dict[str, dict],
    current_engine_by_qid: dict[str, str],
    query_signals: dict[str, list[str]],
    query_map: dict[str, dict],
    query_capabilities: dict[str, list[str]] | None,
    committed_count: int,
    tied_engines_by_qid: Mapping[str, list[str]] | None = None,
    orphaned_qas: list[dict] | None = None,
    source_compatible_engine: str | None = None,
) -> tuple[bool, dict, float]:
    """Phase 1's per-engine question (#502 review): judged on ``engine``'s
    own native queries alone, using the resolver's own per-query scores
    (``_resolver_score``) -- never the reality check's separate fit
    scorer, and never a query another engine's placement happened to land
    on it this run. No engine is protected for being cheapest or
    "primary": the only queries that keep an engine committed outright
    are a mandatory signal override (ADR #296) or a query no other
    committed engine can serve at all (the shared, group-aware
    eligibility gate ``_group_eligible_engines`` -- #502 review round 5,
    finding G: a co-dependency group with a member on ``engine`` is
    irreplaceable for ``engine`` unless some survivor is eligible for
    EVERY member of that group, not just the one native to ``engine`` --
    never a score comparison). Short of that, the engine is removed
    unless at least one native query scores it ``UNIQUE_DELTA_THRESHOLD``
    points above every engine that could take it, and -- once more than
    ``TARGET_ENGINE_COUNT`` engines are already committed -- that unique
    value is worth the fixed cost of keeping it (#494: unchanged, flat
    ``EXTRA_ENGINE_BURDEN_MONTHLY`` and ``> 100`` cut-off).

    ``orphaned_qas`` (#485 review) are the native queries of engines
    ALREADY removed earlier in this same settling pass -- one flat,
    ever-growing list, not tracked per rescuing engine. The single rule:
    every one of them must still have an eligible home among
    ``other_committed`` (``engine`` excluded, like everything else here),
    whichever engine that turns out to be. Without this, ``engine``'s own
    removability check only ever saw ``native_qas`` (frozen from the first
    assignment), so a query rescued onto ``engine`` -- because ``engine``
    was its only eligible survivor when its own original engine was
    removed -- was invisible here, stranding it if ``engine`` was removed
    too, judged purely on ITS unrelated native queries. A stranded one is
    added to ``irreplaceable_queries`` (sorted, alongside any native ones)
    so ``engine`` is protected the same hard way a mandatory signal
    override is -- but ``total_queries``, ``unique_queries``,
    ``redundant_queries`` and ``unique_ratio`` below are computed from
    ``native_qas`` alone, same as if ``orphaned_qas`` were empty: it is
    not actually ``engine``'s own native workload.

    Also returns a rank key (burden per unique query) ``_settle_committed_engines``
    uses to decide WHICH removable engine to remove first when more than
    one is removable in the same round -- the worst value for the fixed
    operational cost, not evaluation order.

    ``source_compatible_engine`` (#600, ADR-031 Decision 13): when ``engine``
    is the source-compatible engine and every one of its native queries
    scores it equal to or higher than its best other committed engine (an
    exact tie, or a margin too small to count as unique), ``engine`` is not
    removed on standing cost -- unless ``tie_break.
    source_compatible_engine_survives`` finds, for every one of those
    queries, a best other engine cheaper by both thresholds. Without this,
    Aurora's higher standing cost alone removed it from a workload it served
    at least as well, on a $50 difference.
    """

    def _stranded(qas: Iterable[dict]) -> list[str]:
        stranded = []
        for qa in qas:
            qid = qa["query_id"]
            if qid in mandatory_query_ids:
                continue
            if not _group_eligible_engines(
                qid,
                trace_by_query,
                current_engine_by_qid,
                other_committed,
                mandatory_query_ids,
                query_signals,
                query_map,
                query_capabilities,
                tied_engines_by_qid,
            ):
                stranded.append(qid)
        return stranded

    orphan_stranded = _stranded(orphaned_qas or [])

    if not native_qas:
        return (
            not orphan_stranded,
            {
                "total_queries": 0,
                "irreplaceable_queries": sorted(orphan_stranded),
                "unique_queries": [],
                "redundant_queries": [],
                "unique_ratio": 0,
                "avg_delta": 0,
            },
            0.0,
        )

    irreplaceable: list[str] = []
    unique_by_score: list[str] = []
    redundant: list[str] = []
    deltas: list[float] = []
    # #600: for each native query ``engine`` scores >= every alternative on,
    # the best-scoring alternatives (the engines it would otherwise go to).
    best_rivals_by_qid: dict[str, list[str]] = {}
    # Split-group members (#894): ``engine`` cannot keep them as one unit, so
    # they are left out of Decision 13's every-query check as well.
    split: list[str] = []

    for qa in native_qas:
        qid = qa["query_id"]
        if qid in mandatory_query_ids:
            irreplaceable.append(qid)
            continue
        eligible_alts = _group_eligible_engines(
            qid,
            trace_by_query,
            current_engine_by_qid,
            other_committed,
            mandatory_query_ids,
            query_signals,
            query_map,
            query_capabilities,
            tied_engines_by_qid,
        )
        if not eligible_alts:
            irreplaceable.append(qid)
            continue
        # #894: a group member is judged on the group's score, never its own.
        scored = _moving_group_scores(
            _moving_members(qid, trace_by_query, mandatory_query_ids, current_engine_by_qid),
            [engine, *eligible_alts],
            trace_by_query,
        )
        if engine not in scored and _in_group(trace_by_query, qid):
            # ``engine`` cannot serve every moving member (a split group):
            # the group is not held here as one unit, so it has no value on
            # ``engine`` to weigh -- never a 0 the scorer did not give.
            redundant.append(qid)
            split.append(qid)
            continue
        this_score = scored.get(engine, 0)
        alt_scores = {alt: scored[alt] for alt in eligible_alts}
        best_alt = max(alt_scores.values())
        delta = this_score - best_alt
        deltas.append(delta)
        if delta >= 0:
            best_rivals_by_qid[qid] = [
                alt for alt, score in alt_scores.items() if score == best_alt
            ]
        if delta >= UNIQUE_DELTA_THRESHOLD:
            unique_by_score.append(qid)
        else:
            redundant.append(qid)

    avg_delta = sum(deltas) / len(deltas) if deltas else 0
    assessment = {
        "total_queries": len(native_qas),
        "irreplaceable_queries": sorted(set(irreplaceable) | set(orphan_stranded)),
        "unique_queries": unique_by_score,
        "redundant_queries": redundant,
        "unique_ratio": (
            (len(irreplaceable) + len(unique_by_score)) / len(native_qas) if native_qas else 0
        ),
        "avg_delta": round(avg_delta, 1),
    }

    total_burden = ENGINE_BASE_COST.get(engine, 100) + EXTRA_ENGINE_BURDEN_MONTHLY
    rank_key = total_burden / max(len(unique_by_score), 1)

    if irreplaceable or orphan_stranded:
        return False, assessment, 0.0
    if (
        engine == source_compatible_engine
        and best_rivals_by_qid
        and len(best_rivals_by_qid) == len(native_qas) - len(split)
        and source_compatible_engine_survives(
            ENGINE_BASE_COST.get(engine, 100),
            (
                [ENGINE_BASE_COST.get(rival, 100) for rival in rivals]
                for rivals in best_rivals_by_qid.values()
            ),
        )
    ):
        # #600, ADR-031 Decisions 2 and 13: the source-compatible engine
        # scores every query at least as well as any alternative, and no
        # alternative is cheap enough to take its place.
        return False, assessment, 0.0
    if not unique_by_score:
        return True, assessment, rank_key
    if committed_count > TARGET_ENGINE_COUNT and rank_key > 100:
        return True, assessment, rank_key
    return False, assessment, 0.0


def _round_evaluation_order(snapshot: set[str]) -> list[str]:
    """The order a round evaluates each committed engine's removability in.

    Alphabetical by default -- a plain, deterministic choice, not a rule
    any engine's removal depends on (every engine is judged against the
    SAME frozen snapshot regardless of the order it is checked in; see
    ``_settle_committed_engines``). A separate, overridable function so a
    test can force a different evaluation order and confirm the settled
    engine set does not change (#502 review item 7: order-independence
    should hold for evaluation order, not just input order).
    """
    return sorted(snapshot)


def _removal_tiebreak_key(
    engine: str,
    native_qas: list[dict],
    survivors: set[str],
    mandatory_query_ids: set[str],
    trace_by_query: dict[str, dict],
    current_engine_by_qid: dict[str, str],
    query_signals: dict[str, list[str]],
    query_map: dict[str, dict],
    query_capabilities: dict[str, list[str]] | None,
    tied_engines_by_qid: Mapping[str, list[str]] | None = None,
    source_compatible_engine: str | None = None,
    settled_counts: Mapping[str, int] | None = None,
) -> tuple[int, float]:
    """Data-driven tiebreaker between two candidates tied on burden-per-unique-
    query (#502 review round 5, finding N1; round 6 corrected the second
    measure) -- no engine name involved.

    Simulates ``engine``'s removal: for every one of its native,
    non-mandatory queries, finds the highest-scoring eligible survivor
    (group-aware, same rule as everywhere else) it would actually land on.
    Returns ``(n_destinations, total_fit_lost)`` -- sorting candidates
    ascending on this prefers fewer distinct destination engines first (the
    more consolidated outcome), then the LEAST total fit lost (``engine``'s
    own score minus the destination's score, summed over every one of its
    queries, not just the ones that cleared ``UNIQUE_DELTA_THRESHOLD`` --
    an engine with a pile of barely-redundant queries loses more by being
    removed than one with the same unique-query count and nothing else).
    Two candidates that still tie on BOTH measures are genuinely
    indistinguishable by any data this review has identified; the caller
    reports that rather than picking one.

    Finding a query's destination among ``alts`` used to be a bare
    ``max(alts, key=score)`` over ``alts`` -- a list built from the
    ``survivors`` *set*, so when two or more alternates scored this query
    identically, which one "won" depended on Python's set iteration order:
    ``PYTHONHASHSEED`` and input order, neither an ADR-031 rule (#569).
    Scores are now collected into a ``dict`` and the top scorers read back
    through ``_top_scorers`` (sorted -- a stable key), and an actual tie
    among them is resolved the same ADR-031 Decision 2 way every other
    routing tie is: ``tie_break.break_tie``, fed this call's own
    ``source_compatible_engine``/``settled_counts`` and the query id, never
    by whichever alternate a set happened to produce first.
    """
    destinations: set[str] = set()
    total_fit_lost = 0.0
    for qa in native_qas:
        qid = qa["query_id"]
        if qid in mandatory_query_ids:
            continue
        alts = _group_eligible_engines(
            qid,
            trace_by_query,
            current_engine_by_qid,
            survivors,
            mandatory_query_ids,
            query_signals,
            query_map,
            query_capabilities,
            tied_engines_by_qid,
        )
        if not alts:
            continue
        # #894: a group member is scored with its group, so the group has one destination.
        members = _moving_members(qid, trace_by_query, mandatory_query_ids, current_engine_by_qid)
        scored = _moving_group_scores(members, [engine, *alts], trace_by_query)
        alt_scores = {alt: scored[alt] for alt in alts}
        top_alts = _top_scorers(alt_scores)
        best_alt = (
            top_alts[0]
            if len(top_alts) == 1
            else break_tie(
                top_alts,
                set(),
                source_compatible_engine,
                settled_counts,
                # The group's stable seed (sorted member ids), as every pass uses.
                ",".join(members),
            )[0]
        )
        destinations.add(best_alt)
        if engine in scored or not _in_group(trace_by_query, qid):
            # A split group (``engine`` cannot serve every moving member)
            # loses no fit it had here as one unit, so it adds none.
            total_fit_lost += scored.get(engine, 0) - alt_scores[best_alt]
    return len(destinations), total_fit_lost


def _settle_committed_engines(
    engine_queries: dict[str, list[dict]],
    mandatory_query_ids: set[str],
    mandatory_committed_engines: set[str],
    trace_by_query: dict[str, dict],
    current_engine_by_qid: dict[str, str],
    query_signals: dict[str, list[str]],
    query_map: dict[str, dict],
    query_capabilities: dict[str, list[str]] | None,
    max_rounds: int,
    tied_engines_by_qid: Mapping[str, list[str]] | None = None,
    source_compatible_engine: str | None = None,
    settled_counts: Mapping[str, int] | None = None,
    *,
    customer_kept_engines: Iterable[str] = (),
) -> tuple[set[str], dict[str, dict]]:
    """Phase 1 (#502 review): settle which engines earn a place, independent
    of the order they happen to be evaluated in.

    ``customer_kept_engines`` (#860) hold a customer-pinned query, which the
    handler keeps out of ``engine_queries`` and puts back as it is: they are
    in the plan whatever this pass decides, so they start committed (with or
    without a native query here) and are never a removal candidate. They are
    still assessed like every other engine. Removing one would only move its
    other queries to an engine that scores them lower, for no saving.

    Every engine in a round is judged against the SAME frozen snapshot of
    the committed set at the round's start, using the resolver's own
    per-query scores -- the verdict for any engine never depends on which
    other engine was checked first. Only ONE engine is actually removed per
    round: the removable candidate with the worst burden-per-unique-query
    (the least economically justified, a data-driven key, not evaluation
    order), and only if removing it alone leaves every one of its native
    queries -- and every co-dependency group with a member on it, as a
    whole unit (``_group_eligible_engines``, #502 review round 5, finding
    G) -- with an eligible home among everyone else still committed.
    Removing just one at a time, then re-evaluating everyone fresh against
    the smaller set, is what review round 3 found round-1's "remove every
    candidate in the round at once" batching got wrong: a cheap, high-volume
    engine could be charged the ``TARGET_ENGINE_COUNT`` burden only because
    another engine, being removed in the very same round, was still being
    counted toward the total. Repeats until a round removes nothing, capped
    at ``max_rounds`` (a warning, not a crash, if that cap is ever hit).

    When two or more candidates tie on burden-per-unique-query, which one
    actually gets removed is NOT decided by engine name (#502 review round
    5, finding N1: that is an arbitrary rule, and changing the input order
    of two equal-cost engines proved it mattered). ``_removal_tiebreak_key``
    resolves the tie by simulating each removal and comparing the result;
    if two candidates are still indistinguishable after that, this function
    removes neither of them this round -- both stay committed, flagged
    ``tie_unresolved`` in the assessment, and a different, non-tied,
    lower-ranked candidate (if any) is tried instead.

    ``source_compatible_engine``/``settled_counts`` are forwarded to
    ``_removal_tiebreak_key`` (#569) so that when ITS OWN simulation finds a
    query with two or more equally-scored destination engines, that
    destination tie goes through the same shared ``tie_break.break_tie``
    every other routing tie does, instead of ``max()`` over whatever order
    the eligible-engines set happens to iterate in -- which depended on
    ``PYTHONHASHSEED`` and input order, not on anything ADR-031 recognizes.
    """
    kept_by_customer = frozenset(customer_kept_engines)
    committed = {e for e in engine_queries if engine_queries.get(e)} | kept_by_customer
    assessment: dict[str, dict] = {}
    # #485 review: the native, non-mandatory queries of every engine
    # ALREADY removed earlier in this same settling pass -- one flat,
    # ever-growing list, not tracked per rescuing engine. Every engine's
    # removability check (``_evaluate_engine_removal``) re-verifies that
    # all of them still have a home among whichever engines remain, so a
    # chain of rescues (an engine that absorbed one of these, later
    # removed itself) is handled the same way a single rescue is, with no
    # per-destination bookkeeping: a query is appended exactly once, when
    # its own original engine is removed, never again afterward.
    orphaned_qas: list[dict] = []
    round_num = 0
    converged = False
    while round_num < max_rounds:
        round_num += 1
        snapshot = set(committed)
        candidates: list[tuple[float, str]] = []
        for engine in _round_evaluation_order(snapshot):
            other = snapshot - {engine}
            removable, info, rank_key = _evaluate_engine_removal(
                engine,
                engine_queries.get(engine, []),
                other,
                mandatory_query_ids,
                trace_by_query,
                current_engine_by_qid,
                query_signals,
                query_map,
                query_capabilities,
                len(snapshot),
                tied_engines_by_qid,
                orphaned_qas,
                source_compatible_engine,
            )
            # #502 review: no engine is "primary" anymore -- every engine is
            # judged the same way. The contract field stays (a breaking
            # contract change is its own proposal, per CONTRIBUTING.md) but
            # is always False now rather than naming a protected engine.
            info["is_primary"] = False
            info["is_mandatory"] = engine in mandatory_committed_engines
            assessment[engine] = info
            if removable and engine not in kept_by_customer:
                candidates.append((rank_key, engine))
        if not candidates:
            converged = True
            break

        # Remove exactly one engine this round: the worst-ranked (highest
        # burden-per-unique-query) candidate that is actually safe on its
        # own. A tie in rank is resolved by data (``_removal_tiebreak_key``),
        # never by engine name; a tie that survives that too is reported,
        # not broken (both candidates stay committed this round).
        by_rank: dict[float, list[str]] = defaultdict(list)
        for rank_key, engine_name in candidates:
            by_rank[rank_key].append(engine_name)

        removed_one = False
        for rank_key in sorted(by_rank, reverse=True):
            tied = by_rank[rank_key]
            survivors_by_candidate = {c: snapshot - {c} for c in tied}
            if len(tied) > 1:
                extra = {
                    c: _removal_tiebreak_key(
                        c,
                        engine_queries.get(c, []),
                        survivors_by_candidate[c],
                        mandatory_query_ids,
                        trace_by_query,
                        current_engine_by_qid,
                        query_signals,
                        query_map,
                        query_capabilities,
                        tied_engines_by_qid,
                        source_compatible_engine,
                        settled_counts,
                    )
                    for c in tied
                }
                best = min(extra.values())
                decisive = sorted(c for c in tied if extra[c] == best)
                if len(decisive) > 1:
                    for c in decisive:
                        assessment[c]["tie_unresolved"] = True
                    continue
            else:
                decisive = tied
            candidate = decisive[0]
            survivors = survivors_by_candidate[candidate]
            native_qas = engine_queries.get(candidate, [])
            # #485 review: removing `candidate` must leave a home for its
            # own native queries AND for every query already orphaned by
            # an earlier removal this pass -- the same single rule
            # `_evaluate_engine_removal` just applied when it judged
            # `candidate` removable against this same `survivors` set.
            if all(
                _group_eligible_engines(
                    qa["query_id"],
                    trace_by_query,
                    current_engine_by_qid,
                    survivors,
                    mandatory_query_ids,
                    query_signals,
                    query_map,
                    query_capabilities,
                    tied_engines_by_qid,
                )
                for qa in native_qas + orphaned_qas
                if qa["query_id"] not in mandatory_query_ids
            ):
                committed.discard(candidate)
                removed_one = True
                # `candidate`'s own native queries are now orphaned too --
                # appended exactly once, here, never re-added on a later
                # removal (they are already in the list by then).
                orphaned_qas.extend(
                    qa for qa in native_qas if qa["query_id"] not in mandatory_query_ids
                )
                break
        if not removed_one:
            converged = True
            break

    if not converged:
        warnings.warn(
            f"_settle_committed_engines did not converge within {max_rounds} rounds "
            f"(stopped with {sorted(committed)} still committed) -- stopping instead of "
            "looping forever; this should not happen outside a pathological input.",
            stacklevel=2,
        )
    return committed, assessment
