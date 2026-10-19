"""Rules the reality check's passes share: engine costs, eligibility and ties.

Phase 1 (``reality_check_settle``), Phase 2 (``reality_check_place``) and the
justification floors (``reality_check_floors``) read the same per-query scores
(``_resolver_score``), the same group-aware eligibility gate
(``_group_eligible_engines``) and the same tie-break (``_pick_winner``), so a
query is never judged one way by one pass and another way by the next.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Container, Iterable, Mapping

from src.agents.referee.cache_overlay import can_own
from src.agents.referee.capability_registry import can_engine_serve_capability
from src.agents.referee.co_dependency import group_engine_scores
from src.agents.referee.query_scoring import GATE_KIND_WRITE
from src.agents.referee.tie_break import (
    TIE_KIND_SOURCE_COMPATIBLE,
    TIE_KIND_STABILITY,
    break_tie,
    pick_unresolved_tie_placeholder,
)
from src.contracts.move_ledger import CandidateScore, ReasonCode
from src.shared import catalog
from src.shared.engine_names import display_engine

# Target engine count — the reality check tries to stay at or below this.
# A third engine must provide genuinely unique, irreplaceable capabilities.
TARGET_ENGINE_COUNT = catalog.threshold("target_engine_count")

# Per-engine operational overhead (monthly base cost + ops burden), read from each
# engine's ``[pricing]`` table in ``src/catalog/engines/*.toml`` (#494). It is not just
# infrastructure: it includes monitoring, backups, failover config, team expertise,
# security patching, etc. Engines with no pricing table are absent here and fall back to
# the catalog default in ``engine_base_cost``.
ENGINE_BASE_COST: dict[str, float] = {
    e.id: e.pricing.base_monthly for e in catalog.engines() if e.pricing is not None
}


def engine_base_cost(engine: str) -> float:
    """``engine``'s fixed monthly cost (``ENGINE_BASE_COST``, else the catalog default)."""
    cost = ENGINE_BASE_COST.get(engine)
    return catalog.pricing(engine).base_monthly if cost is None else cost


def extra_engine_burden(engine: str) -> float:
    """The non-monetary monthly burden of ``engine`` beyond ``TARGET_ENGINE_COUNT``.

    It captures the team needing expertise in N databases, N monitoring dashboards, N
    backup strategies, N failover runbooks, etc.
    """
    return catalog.pricing(engine).extra_engine_burden_monthly


def max_burden_per_unique_query(engine: str) -> float:
    """The burden per uniquely-served query above which ``engine`` is removable."""
    return catalog.pricing(engine).max_burden_per_unique_query


# Set of Aurora engine identifiers: the active relational engines (catalog role).
AURORA_ENGINES = set(catalog.engine_ids(status=catalog.ACTIVE, role="relational"))


def _query_has_join(query: dict) -> bool:
    """Whether ``query`` joins two or more tables (``has_joins``/``join_count``)."""
    return bool(query.get("has_joins")) or (query.get("join_count") or 0) >= 1


# ---------------------------------------------------------------------------
# Differentiation scoring — how much better is this engine vs alternatives?
# ---------------------------------------------------------------------------

# Minimum confidence delta for a query to be "unique" to an engine.
# If the best alternative is within this many points, the query is redundant.
UNIQUE_DELTA_THRESHOLD = catalog.threshold("unique_delta_threshold")

# Engines that may absorb only the queries their specialty serves (#296): an
# engine of this kind is a search/analytics read model, not a general owner, so
# consolidation never routes plain lookups or writes to it. Signals by query. From
# each engine file's ``[absorbs_only]`` (#921).
SPECIALIST_ABSORB_SIGNALS: dict[str, frozenset[str]] = {
    e.id: frozenset(e.absorbs_only.signals) for e in catalog.engines() if e.absorbs_only
}
SPECIALIST_ABSORB_CAPABILITIES: dict[str, frozenset[str]] = {
    e.id: frozenset(e.absorbs_only.capabilities) for e in catalog.engines() if e.absorbs_only
}


def may_absorb(
    engine: str,
    qid: str,
    query: dict,
    query_signals: dict[str, list[str]],
    query_capabilities: dict[str, list[str]] | None = None,
    source_engine: str | None = None,
) -> bool:
    """Whether consolidation may move query ``qid`` onto ``engine`` (#296).

    The write gate (a cache owns nothing, a non-system-of-record engine owns no
    write), and specialist engines absorb only queries that need their specialty.
    """
    if not can_own(engine, query, source_engine):
        return False
    signals = SPECIALIST_ABSORB_SIGNALS.get(engine)
    if signals is None:
        return True
    caps = SPECIALIST_ABSORB_CAPABILITIES.get(engine, frozenset())
    return bool(
        signals & set(query_signals.get(qid, []))
        or caps & set((query_capabilities or {}).get(qid, []))
    )


def _resolver_score(trace_by_query: dict[str, dict], qid: str, engine: str) -> float | None:
    """``engine``'s score for ``qid``, exactly as ``AssignmentResolver.resolve()``
    used it (``RoutingTrace.by_query[qid]["engines"][engine]``) -- ``None`` when
    the resolver's own hard exclusion/capability/write gate ruled ``engine`` out
    for this query entirely, never a recompute via a different scorer (#502
    review: Phase 1 and Phase 2 must read the identical scores, not Phase 1
    using the reality check's own, separate ``_engine_fit_score``).
    """
    payload = trace_by_query.get(qid)
    if not payload:
        return None
    value = payload.get("engines", {}).get(engine)
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, dict):
        if "gate" in value:
            return None
        used = value.get("used")
        return float(used) if isinstance(used, (int, float)) else None
    return None


def _trace_ladder(trace_by_query: dict[str, dict], qid: str) -> list[CandidateScore]:
    """``qid``'s candidate ladder from the resolver's trace, for a Move's evidence (#616).

    Every analyzed engine, sorted: its score (``_resolver_score``), or the
    resolver's hard gate with no score; an engine that may not own the query
    keeps its score with ``gate="write_gate"``, as the first assignment's
    ladder does (``move_ledger.candidate_ladder``). Reads the trace only.
    """
    ladder: list[CandidateScore] = []
    for engine, value in sorted((trace_by_query.get(qid) or {}).get("engines", {}).items()):
        if isinstance(value, dict) and value.get("gate"):
            used = value.get("used") if value["gate"] == GATE_KIND_WRITE else None
            ladder.append(
                CandidateScore(
                    engine=engine,
                    score=float(used) if isinstance(used, (int, float)) else None,
                    gate=value["gate"],
                )
            )
            continue
        ladder.append(
            CandidateScore(engine=engine, score=_resolver_score(trace_by_query, qid, engine))
        )
    return ladder


def tie_reason_code(
    tie_kind: str,
    tied: list[str],
    settled_counts: Mapping[str, int] | None,
    seed: str,
) -> ReasonCode:
    """The ledger rule (#616) that decided a tie ``break_tie`` resolved.

    For an unresolved tie, which of Decision 12's two placeholder rules picked
    the engine: ``pick_unresolved_tie_placeholder`` is pure, so called with the
    arguments ``break_tie`` passed it, it reports the rule that decided.
    A record only: it never changes the engine ``break_tie`` returned.
    """
    if tie_kind == TIE_KIND_SOURCE_COMPATIBLE:
        return ReasonCode.TIE_SOURCE_COMPATIBLE
    if tie_kind == TIE_KIND_STABILITY:
        return ReasonCode.TIE_STABILITY
    _, by_workload_share = pick_unresolved_tie_placeholder(tied, settled_counts or {}, seed)
    return (
        ReasonCode.TIE_PLACEHOLDER_SETTLED_WORKLOAD
        if by_workload_share
        else ReasonCode.TIE_PLACEHOLDER_HASH
    )


def _move_scores_text(
    from_engine: str, before: float | None, to_engine: str, after: float | None
) -> str:
    """``"<from> scores N, <to> scores M"``: the before and after scores a moved
    query reports (ADR-031 Decision 7, #698, #755). The caller passes the scores
    its pass compared: Phase 2 and the floors read the resolver's trace
    (``_resolver_score`` or the group's recorded group score), the sanity sweep
    calls the shared scorer directly (``score_query_for_engine``, the same
    numbers). ``n/a`` only when a gate ruled the engine out for the query, so
    there was no score to report."""

    def _fmt(value: float | None) -> str:
        return "n/a" if value is None else str(float(value))

    return (
        f"{display_engine(from_engine)} scores {_fmt(before)}, "
        f"{display_engine(to_engine)} scores {_fmt(after)}"
    )


def _query_eligible_engines(
    qid: str,
    qa: dict,
    current_engine: str | Iterable[str],
    candidates: Iterable[str],
    trace_by_query: dict[str, dict],
    query_signals: dict[str, list[str]],
    query_map: dict[str, dict],
    query_capabilities: dict[str, list[str]] | None,
    source_engine: str | None = None,
) -> list[str]:
    """``candidates`` that may actually own ``qid`` -- the ONE eligibility
    rule Phase 1 (which engines earn a place) and Phase 2 (where every
    query lands) both apply, so a query can never be judged eligible for
    one phase's purposes and ineligible for the other's (#502 review:
    this gap is exactly how a query could end up stranded on an engine
    Phase 1 had already removed).

    ``current_engine`` is normally the single engine the query is on today.
    For a query still flagged ``tied_engines`` (#503), callers pass the
    whole tied set instead of the one, arbitrary
    placeholder engine: judging eligibility from a single tied engine let
    the placeholder's own, otherwise-inconsequential pick decide the
    result through this gate -- a join query tied 70/70 between DocumentDB
    and DynamoDB became eligible for DynamoDB or not purely depending on
    which of the two the placeholder happened to be recorded as, silently
    turning a non-decision into one. Treating every tied engine as
    "current" means the gates below answer the same way regardless of
    which placeholder a prior run happened to pick.

    Four gates, all of which must pass:
    - the resolver's own score exists at all (``_resolver_score`` is not
      ``None``) -- the resolver's own hard exclusion/capability/write gate
      ruled the engine out for this query, so there is nothing for Phase 2
      to place it with even if the checks below would otherwise allow it;
    - the ownership/write gate and specialist-absorb restriction
      (``may_absorb``) -- a specialist engine (OpenSearch) only ever
      receives a query that matches its own specialty;
    - the hard capability gate (``can_engine_serve_capability``);
    - the join-on-DynamoDB block (review of #375, finding B2): a join
      query consolidated away from a non-Aurora engine never lands on
      DynamoDB. A join already on DynamoDB, or arrived there from Aurora,
      is unaffected -- this only blocks a *consolidation* onto it, so
      ``current_engine`` (where the query sits right now) decides whether
      the block applies, not just which engine is being considered.
    """
    required_caps = (query_capabilities or {}).get(qid, [])
    query = query_map.get(qid, qa)
    current_set = {current_engine} if isinstance(current_engine, str) else set(current_engine)
    # Named for what it gates, not what it checks (#502 review round 5):
    # this is true exactly when considering ``e`` for ``qid`` would be a
    # *consolidation onto* ``e`` away from an engine that is neither
    # DynamoDB nor Aurora -- the join-on-DynamoDB block below only applies
    # to that case, never to a join query already on DynamoDB or Aurora.
    # With a tied set, this is False (not "leaving") as soon as ANY tied
    # engine is dynamodb/Aurora -- the best case for dynamodb's own
    # eligibility, so an arbitrary OTHER placeholder pick can't block it.
    leaving_non_aurora_non_dynamodb_engine = not (current_set & ({"dynamodb"} | AURORA_ENGINES))

    def ok(e: str) -> bool:
        if _resolver_score(trace_by_query, qid, e) is None:
            return False
        # The engine already (legitimately) owning this query is always
        # eligible to keep it: ``may_absorb``'s specialist-absorb
        # restriction is about CONSOLIDATING a query onto an engine, never
        # about an engine continuing to hold what it already has (#502
        # review: applying it unconditionally stripped OpenSearch of its
        # own already-matched query whenever no triage signal/capability
        # happened to be attached to it, moving it away for no score
        # reason at all).
        if e not in current_set and not may_absorb(
            e, qid, query, query_signals, query_capabilities, source_engine
        ):
            return False
        if not can_engine_serve_capability(e, required_caps):
            return False
        if (
            e == "dynamodb"
            and leaving_non_aurora_non_dynamodb_engine
            and _query_has_join(query_map.get(qid, {}))
        ):
            return False
        return True

    return [e for e in candidates if ok(e)]


def _current_for_eligibility(
    qid: str,
    current_engine_by_qid: dict[str, str],
    tied_engines_by_qid: Mapping[str, list[str]] | None,
) -> str | list[str]:
    """What ``_query_eligible_engines`` should treat as ``qid``'s current
    engine(s): its whole ``tied_engines`` set when it has one (#503), else
    the one real engine it is on today."""
    if tied_engines_by_qid:
        tied = tied_engines_by_qid.get(qid)
        if tied:
            return tied
    return current_engine_by_qid.get(qid, "")


def _group_eligible_engines(
    qid: str,
    trace_by_query: dict[str, dict],
    current_engine_by_qid: dict[str, str],
    candidates: Iterable[str],
    mandatory_query_ids: set[str],
    query_signals: dict[str, list[str]],
    query_map: dict[str, dict],
    query_capabilities: dict[str, list[str]] | None,
    tied_engines_by_qid: Mapping[str, list[str]] | None = None,
    source_engine: str | None = None,
) -> list[str]:
    """``candidates`` eligible to hold ``qid`` -- group-aware (#502 review
    round 5, finding G).

    A co-dependency group moves as one unit, so an engine is eligible for a
    grouped query only if it is eligible for EVERY non-mandatory member of
    that group, including members currently sitting on a different engine,
    not just ``qid`` itself (``_query_eligible_engines`` on its own only
    ever asks about one query). An engine that cannot take the whole group
    is irreplaceable for it -- never split a group to make an engine look
    safe to remove, or to decide where it lands.

    A mandatory member (a signal override, ADR #296) never travels with its
    group, so it is excluded from the "every member" requirement here, the
    same way Phase 2 already excludes it when it applies the group's move.
    Neither does a member this check does not hold (a customer pin, #882).
    The member set is ``_moving_members``, the one Phase 1's and Phase 2's
    group scores use too (#894).

    ``tied_engines_by_qid`` (#503) is forwarded to ``_query_eligible_engines``
    for every member still flagged with a
    tie, so eligibility never depends on which arbitrary engine a prior
    run's placeholder happened to pick.

    This is the ONE eligibility entry point both Phase 1
    (``_evaluate_engine_removal``, ``_settle_committed_engines``) and
    Phase 2 (``_place_settled_queries``) use, for grouped and ungrouped
    queries alike -- ``_query_eligible_engines`` itself is the single-query
    primitive this calls, never a second, divergent rule.
    """
    candidates = list(candidates)
    current_engine = _current_for_eligibility(qid, current_engine_by_qid, tied_engines_by_qid)
    group_info = trace_by_query.get(qid, {}).get("group")
    if not group_info:
        return _query_eligible_engines(
            qid,
            query_map.get(qid, {"query_id": qid}),
            current_engine,
            candidates,
            trace_by_query,
            query_signals,
            query_map,
            query_capabilities,
            source_engine=source_engine,
        )
    members = _moving_members(qid, trace_by_query, mandatory_query_ids, current_engine_by_qid)
    return [
        e
        for e in candidates
        if all(
            e
            in _query_eligible_engines(
                m_qid,
                query_map.get(m_qid, {"query_id": m_qid}),
                _current_for_eligibility(m_qid, current_engine_by_qid, tied_engines_by_qid),
                {e},
                trace_by_query,
                query_signals,
                query_map,
                query_capabilities,
                source_engine=source_engine,
            )
            for m_qid in members
        )
    ]


def _moving_members(
    qid: str,
    trace_by_query: dict[str, dict],
    staying_query_ids: Container[str],
    held_query_ids: Container[str],
) -> list[str]:
    """``qid``'s co-dependency group members that move with it, sorted; just
    ``[qid]`` when it is in no group.

    The one member set every reality-check pass judges a group on (#894,
    ADR-031 Decisions 4 and 7): Phase 1's eligibility and removal scores,
    Phase 2's placement and the justification floor. A member in
    ``staying_query_ids`` (a mandatory signal override, ADR #296) stays put,
    and a member missing from ``held_query_ids`` (the queries this check
    holds; a customer-pinned query is held out of it, #860/#882) is not this
    check's to move, so neither counts.
    """
    group_info = trace_by_query.get(qid, {}).get("group")
    if not group_info:
        return [qid]
    return sorted(
        mqid
        for mqid, mpayload in trace_by_query.items()
        if (mg := mpayload.get("group")) and mg["key"] == group_info["key"]
        if mqid not in staying_query_ids and mqid in held_query_ids
    )


def _moving_group_scores(
    members: Iterable[str],
    engines: Iterable[str],
    trace_by_query: dict[str, dict],
) -> dict[str, float]:
    """The group's score on each of ``engines`` that serves every one of
    ``members`` (``_moving_members``): the mean of their resolver scores
    (``co_dependency.group_engine_scores``, ADR-031 Decision 4, Amendment 4;
    call-rate weighted for a unit holding a write group, Amendment 9),
    rounded to 2 decimals as the routing trace records a group's mean. An
    engine that strands a member has no entry. For one query (ungrouped, or
    the only member that moves) this is its own resolver score, unrounded.
    Phase 1, Phase 2 and the justification floor all read a group's scores
    from here, so they judge it on the same numbers (Decision 7).
    """
    members = list(members)
    # A write unit's call-rate weights, as the resolver recorded them (#992).
    weights = {
        m: w
        for m in members
        if (w := ((trace_by_query.get(m, {}).get("group") or {}).get("weights") or {}).get(m))
        is not None
    }
    scores = group_engine_scores(
        members, engines, lambda mqid, e: _resolver_score(trace_by_query, mqid, e), weights
    )
    if len(members) == 1:
        # One query's own resolver score, read raw as everywhere else.
        return scores
    return {e: round(score, 2) for e, score in scores.items()}


def _pick_winner(
    scores: dict[str, float],
    current_engines: set[str],
    source_compatible_engine: str | None,
    settled_counts: Mapping[str, int] | None = None,
    query_id: str = "",
) -> tuple[str, str | None]:
    """Highest score wins. Returns ``(winner, tie_kind)``; ``tie_kind`` is
    ``None`` when one engine strictly scores highest (no tie to resolve),
    otherwise the result of ``break_tie`` (shared with the resolver's
    ``break_owner_tie`` and the floors' absorber tie-break, #503) -- see
    its docstring for the three-rule order (source-compatible, then
    stability, then an honestly-reported ``tie_unresolved``, decided by the
    maintainer's placeholder when it comes to that -- ``settled_counts``/
    ``query_id`` feed that placeholder only).
    """
    tied = _top_scorers(scores)
    if len(tied) == 1:
        return tied[0], None
    return break_tie(tied, current_engines, source_compatible_engine, settled_counts, query_id)


def _top_scorers(scores: dict[str, float]) -> list[str]:
    """Engines tied at the top of ``scores`` (length 1 when there is no tie)."""
    top = max(scores.values())
    return sorted(e for e, s in scores.items() if s == top)


def _tie_unresolved_note(tied: Iterable[str], winner: str) -> str:
    """Customer-facing fragment for an unresolved tie (#503 review item 4):
    display names only -- no raw engine code, no internal ADR-031
    reference, and the placeholder pick is never called "non-preferential"
    (it is named for what it is: a placeholder pending the customer's
    choice, not a decided rule)."""
    others = ", ".join(display_engine(e) for e in sorted(set(tied)) if e != winner)
    return (
        f" (scores tie with {others}; no routing rule decides between them; "
        f"listed under {display_engine(winner)} pending your choice)"
    )


def _build_query_signals(signals: list[dict]) -> dict[str, list[str]]:
    """Map query_id → triage signal names."""
    query_signals: dict[str, list[str]] = defaultdict(list)
    for signal in signals:
        for qid in signal.get("query_ids", []):
            query_signals[qid].append(signal.get("signal", ""))
    return query_signals


def _is_signal_override(qa: dict) -> bool:
    """True when a triage signal forced this query to its engine (#151).

    Assignments written before ``signal_override`` existed carry the signal
    only in ``assignment_reason``; fall back to that for legacy artifacts.
    """
    if "signal_override" in qa:
        return bool(qa["signal_override"])
    return str(qa.get("assignment_reason", "")).startswith("signal override")
