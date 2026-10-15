"""Model flags are evidence; the shared scorer decides (ADR-031 Decision 11, #647).

A model may flag queries it judges an engine unable to serve
(:class:`~src.contracts.model_flag.ModelFlag`): the queries, the engine they are
on, a reason category and a short explanation. It never names a destination.
This module turns an accepted flag into a routing change the same way for every
caller:

1. the flagged engine becomes unable to serve those queries, for those queries
   only (``ScoringContext.flag_gaps``, the third gate of
   :func:`~src.agents.referee.query_scoring.score_query_for_engine`);
2. the queries are re-routed by the shared scorer to the best remaining engine
   that may own them (the same serviceability and eligibility checks the
   sanity sweep uses), with exact ties through ``tie_break.break_tie``; a
   co-dependency group moves as one unit (Decision 4);
3. when no other engine can serve them, they stay, and the record says so.

Every flag yields one :class:`FlagOutcome` with the before score (the flagged
engine's own, ignoring the flag) and the after score (the destination's).
Nothing in a flag's text is read to pick an engine: the explanation and the
reason category are recorded, never scored.

:func:`route_flagged_unit` is the shared step. :func:`apply_consolidation_flags`
applies reality-check flags (the consolidation validator, both LLM modes).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from src.agents.referee.cache_overlay import CACHE_OVERLAY_ENGINES
from src.agents.referee.consolidation_validator import (
    _eligible_destinations,
    _group_serviceable_scores,
    _ignoring_flags,
    _serviceable_scores,
    _tied_candidate_pool,
    _unit_score,
    scorer_ladder,
)
from src.agents.referee.move_ledger import (
    MoveLedger,
    apply_to_assignment,
    best_other,
    group_key,
    ledger_for,
)
from src.agents.referee.query_scoring import ScoringContext
from src.agents.referee.reality_check_request import moved_queries
from src.agents.referee.reality_check_rules import tie_reason_code
from src.agents.referee.tie_break import TIE_KIND_UNRESOLVED, break_tie
from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA
from src.contracts.model_flag import FlagReason, ModelFlag, parse_model_flags
from src.contracts.move_ledger import EngineScore, MoveActor, MoveEvidence, MovePass, ReasonCode
from src.shared.engine_names import display_engine
from src.shared.flag_reasons import FLAG_REASON_TEXT

logger = logging.getLogger(__name__)

__all__ = ["apply_consolidation_flags", "parse_model_flags", "route_flagged_unit"]

# Customer-facing wording for each reason category (never the raw enum value),
# shared with the deliverables (src.shared.flag_reasons).
FLAG_REASON_LABELS: dict[FlagReason, str] = {
    reason: FLAG_REASON_TEXT[reason.value] for reason in FlagReason
}

OUTCOME_MOVED = "moved"
OUTCOME_KEPT = "kept"
OUTCOME_IGNORED = "ignored"


@dataclass(frozen=True)
class UnitRouting:
    """Where the shared scorer sends one flagged unit (a query or a group)."""

    destination: str | None
    score_before: float | None
    score_after: float | None
    tie_kind: str | None = None
    tied: tuple[str, ...] = ()
    blocked_reason: str | None = None
    # The best remaining engine not picked, and every eligible score compared
    # (a co-dependency group's are member means), for the Move (#616).
    runner_up: EngineScore | None = None
    scores: tuple[tuple[str, float], ...] = ()
    tie_rule: ReasonCode | None = None


def route_flagged_unit(
    members: list[str],
    flagged_engine: str,
    candidates: Iterable[str],
    scoring_context: ScoringContext,
    query_map: Mapping[str, Mapping],
    *,
    query_signals: Mapping[str, list[str]] | None = None,
    source_engine: str = "",
    settled: Mapping[str, int] | None = None,
    tied_engines: list[str] | None = None,
) -> UnitRouting:
    """The shared step: re-route ``members`` off ``flagged_engine`` by score.

    ``scoring_context`` must already carry the flag's gap (``with_flag_gaps``);
    ``flagged_engine`` is also dropped from ``candidates`` so it can never be
    the answer. Candidates are scored with the shared scorer, filtered through
    the same eligibility rule the reality check's placement uses (ownership,
    the specialist-absorb restriction, the join-onto-DynamoDB block), and the
    best one wins; an exact tie goes through ``break_tie``. ``score_before`` is
    the flagged engine's own score ignoring the flag.
    """
    score_before = _unit_score(members, flagged_engine, query_map, _ignoring_flags(scoring_context))
    remaining = sorted(
        {e for e in candidates if e != flagged_engine and e not in CACHE_OVERLAY_ENGINES}
    )
    tied_pool = [e for e in (tied_engines or []) if e != flagged_engine]
    pool = _tied_candidate_pool(tied_pool or None, remaining)
    if len(members) > 1:
        scores, reasons = _group_serviceable_scores(members, pool, query_map, scoring_context)
    else:
        query = query_map.get(members[0]) or {"query_id": members[0]}
        scores, reasons = _serviceable_scores(query, pool, scoring_context)
    if not scores:
        why = "; ".join(reasons) if reasons else "no other engine is in the architecture"
        return UnitRouting(None, score_before, None, blocked_reason=why)
    eligible = _eligible_destinations(
        members,
        flagged_engine,
        tied_pool or None,
        scores,
        query_signals or {},
        query_map,
        scoring_context.query_capabilities,
    )
    if not eligible:
        return UnitRouting(
            None,
            score_before,
            None,
            blocked_reason=(
                "no remaining engine may own it under the reality check's placement rule"
            ),
        )
    scores = {e: s for e, s in scores.items() if e in eligible}
    top = max(scores.values())
    tied = sorted(e for e, s in scores.items() if s == top)
    if len(tied) > 1:
        source_compatible = SOURCE_ENGINE_TO_AURORA.get((source_engine or "").lower())
        winner, tie_kind = break_tie(
            tied, set(), source_compatible, settled_counts=settled or {}, query_id=",".join(members)
        )
    else:
        (winner,) = tied
        tie_kind = None
    return UnitRouting(
        winner,
        score_before,
        scores[winner],
        tie_kind=tie_kind,
        tied=tuple(tied) if tie_kind == TIE_KIND_UNRESOLVED else (),
        tie_rule=(
            tie_reason_code(tie_kind, tied, settled, ",".join(members))
            if tie_kind is not None
            else None
        ),
        runner_up=best_other(scores, winner),
        scores=tuple(sorted(scores.items())),
    )


def _fmt_score(value: float | None) -> str:
    return "n/a" if value is None else f"{value:g}"


@dataclass
class FlagOutcome:
    """What one flag did: the record ``RealityCheckOutputContract.flag_decisions`` keeps."""

    query_ids: list[str]
    flagged_engine: str
    reason_category: str
    explanation: str
    outcome: str
    destination_engine: str | None = None
    score_before: float | None = None
    score_after: float | None = None
    reason: str = ""
    moved_with_group: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "query_ids": list(self.query_ids),
            "flagged_engine": self.flagged_engine,
            "reason_category": self.reason_category,
            "explanation": self.explanation,
            "outcome": self.outcome,
            "destination_engine": self.destination_engine,
            "score_before": self.score_before,
            "score_after": self.score_after,
            "reason": self.reason,
            "moved_with_group": list(self.moved_with_group),
        }


def _moved_reason(flagged: str, label: str, routing: UnitRouting, grouped: bool) -> str:
    dest = routing.destination or ""
    basis = "co-dependency group moves together" if grouped else "best-scoring remaining engine"
    return (
        f"reality check: flagged on {display_engine(flagged)} ({label}); moved to "
        f"{display_engine(dest)}, the {basis} (score {_fmt_score(routing.score_after)} "
        f"against {_fmt_score(routing.score_before)} on {display_engine(flagged)})"
    )


def _kept_reason(flagged: str, label: str, routing: UnitRouting) -> str:
    return (
        f"Flagged on {display_engine(flagged)} ({label}), but no other engine can take it: "
        f"{routing.blocked_reason}; it stays on {display_engine(flagged)}"
    )


def apply_consolidation_flags(
    flags: list[ModelFlag],
    revised_assignments: list[dict],
    consolidations: list[dict],
    original_assignments: list[dict] | None,
    scoring_context: ScoringContext,
    query_map: Mapping[str, Mapping],
    candidate_engines: Iterable[str],
    *,
    query_signals: Mapping[str, list[str]] | None = None,
    source_engine: str = "",
    settled: Mapping[str, int] | None = None,
    co_dependency_groups: list[list[str]] | None = None,
    ledger: MoveLedger | None = None,
) -> tuple[list[dict], list[dict], list[dict], dict[str, set[str]]]:
    """Apply reality-check flags on consolidated queries (both LLM modes).

    A flag counts only for a query a consolidation moved ONTO ``flagged_engine``
    (it sits there now and came from the consolidation's ``from_engine``): the
    review covered exactly those moves. Anything else is recorded as
    ``ignored``. Each accepted query (with its co-dependency group members on
    the same engine) goes through :func:`route_flagged_unit` against
    ``candidate_engines`` (the engines this reality check kept: a flag never
    brings back a removed engine, maintainer decision 2026-10-14).

    Mutates ``revised_assignments`` in place and returns
    ``(revised_assignments, consolidations, decisions, gaps)``: ``decisions``
    holds one :meth:`FlagOutcome.as_dict` per flagged unit; ``gaps`` maps every
    accepted flagged query to its flagged engine, for later steps (the sanity
    sweep) to keep honoring.

    Each moved query is recorded on ``ledger`` (#616;
    ``move_ledger.apply_to_assignment``, pass ``model_flag``, rule
    ``flag.capability_gap``, actor ``model_flag``) with the same before and
    after scores as its ``flag_decisions`` record. A fresh one from
    ``revised_assignments`` when ``None``.
    """
    if ledger is None:
        ledger = ledger_for(revised_assignments)
    candidates = sorted(set(candidate_engines))
    by_id = {qa["query_id"]: qa for qa in revised_assignments}
    original_engine = {qa["query_id"]: qa["assigned_engine"] for qa in (original_assignments or [])}
    moved_onto: dict[str, set[str]] = {}
    for c in consolidations:
        for qa in moved_queries(c, revised_assignments, original_assignments):
            moved_onto.setdefault(c["to_engine"], set()).add(qa["query_id"])
    group_of: dict[str, list[str]] = {}
    for group in co_dependency_groups or []:
        if len(group) >= 2:
            for qid in group:
                group_of[qid] = sorted(group)

    decisions: list[dict] = []
    gaps: dict[str, set[str]] = {}
    accepted: list[tuple[list[str], ModelFlag]] = []
    for flag in flags:
        ok_ids: list[str] = []
        for qid in dict.fromkeys(flag.query_ids):
            current = by_id.get(qid)
            if current is None or current["assigned_engine"] != flag.flagged_engine:
                why = "it is not on that engine"
            elif qid not in moved_onto.get(flag.flagged_engine, set()):
                why = "no consolidation moved it onto that engine"
            else:
                ok_ids.append(qid)
                continue
            logger.warning(
                "Ignored a model flag for %r on %r: %s", qid[:80], flag.flagged_engine, why
            )
            decisions.append(
                FlagOutcome(
                    [qid],
                    flag.flagged_engine,
                    flag.reason_category.value,
                    flag.explanation,
                    OUTCOME_IGNORED,
                    reason=f"Flag ignored: {why}",
                ).as_dict()
            )
        if ok_ids:
            accepted.append((ok_ids, flag))
            for qid in ok_ids:
                gaps.setdefault(qid, set()).add(flag.flagged_engine)

    flagged_context = scoring_context.with_flag_gaps(gaps)
    done: set[str] = set()
    for ok_ids, flag in accepted:
        engine = flag.flagged_engine
        label = FLAG_REASON_LABELS[flag.reason_category]
        for qid in ok_ids:
            if qid in done:
                continue
            members = sorted(
                {qid}
                | {
                    m
                    for m in group_of.get(qid, [])
                    if m in by_id and by_id[m]["assigned_engine"] == engine
                }
            )
            done.update(members)
            tied = next(
                (by_id[m].get("tied_engines") for m in members if by_id[m].get("tied_engines")),
                None,
            )
            routing = route_flagged_unit(
                members,
                engine,
                candidates,
                flagged_context,
                query_map,
                query_signals=query_signals,
                source_engine=source_engine,
                settled=settled,
                tied_engines=tied,
            )
            flagged_members = [m for m in members if m in gaps]
            outcome = FlagOutcome(
                flagged_members,
                engine,
                flag.reason_category.value,
                flag.explanation,
                OUTCOME_KEPT,
                score_before=routing.score_before,
                moved_with_group=[m for m in members if m not in gaps],
            )
            if routing.destination is None:
                outcome.reason = _kept_reason(engine, label, routing)
                decisions.append(outcome.as_dict())
                continue
            outcome.outcome = OUTCOME_MOVED
            outcome.destination_engine = routing.destination
            outcome.score_after = routing.score_after
            outcome.reason = _moved_reason(engine, label, routing, len(members) > 1)
            grouped = len(members) > 1
            for m in members:
                apply_to_assignment(
                    ledger,
                    by_id[m],
                    routing.destination,
                    pass_=MovePass.MODEL_FLAG,
                    reason_code=ReasonCode.FLAG_CAPABILITY_GAP,
                    score_before=routing.score_before,
                    score_after=routing.score_after,
                    runner_up=routing.runner_up,
                    evidence=MoveEvidence(
                        candidates=scorer_ladder(
                            query_map.get(m) or {"query_id": m}, flagged_context
                        ),
                        group_scores=dict(routing.scores) if grouped else {},
                        tie_rule=routing.tie_rule,
                    ),
                    group_key=group_key(group_of[m]) if grouped else None,
                    tied_engines=list(routing.tied),
                    actor=MoveActor.MODEL_FLAG,
                )
                by_id[m]["assignment_reason"] = outcome.reason
            decisions.append(outcome.as_dict())

    # query_id -> (the flagged engine it left, where it went)
    moved_off = {
        qid: (d["flagged_engine"], d["destination_engine"])
        for d in decisions
        if d["outcome"] == OUTCOME_MOVED
        for qid in d["query_ids"] + d["moved_with_group"]
    }
    return (
        revised_assignments,
        _update_consolidation_records(consolidations, moved_off, original_engine),
        decisions,
        gaps,
    )


def _update_consolidation_records(
    consolidations: list[dict],
    moved_off: Mapping[str, tuple[str, str]],
    original_engine: Mapping[str, str],
) -> list[dict]:
    """Take re-routed queries out of the consolidation that moved them.

    A query that went back to the consolidation's own source is listed as
    retained there; one that went anywhere else simply leaves this record (the
    record reconciliation later adds the record for its actual move). The
    source engine stays, so a partial record claims no saving.
    """
    if not moved_off:
        return consolidations
    updated: list[dict] = []
    for c in consolidations:
        src, dst = c["from_engine"], c["to_engine"]
        off = sorted(
            qid
            for qid, (left, _went) in moved_off.items()
            if left == dst and original_engine.get(qid) == src
        )
        if not off:
            updated.append(c)
            continue
        remaining = c["query_count"] - len(off)
        if remaining <= 0:
            continue
        back = [q for q in off if moved_off[q][1] == src]
        src_name, dst_name = display_engine(src), display_engine(dst)
        record = {
            **c,
            "query_count": remaining,
            "saved_cost_estimate": 0,
            "action": "partial",
            "reason": (
                f"Partial consolidation: {remaining} of {c['query_count']} {src_name} "
                f"queries moved to {dst_name}; {len(off)} flagged on {dst_name} "
                "re-routed by score"
            ),
        }
        if back:
            record["queries_retained"] = sorted(set(c.get("queries_retained") or []) | set(back))
            record["retention_reason"] = (
                f"Flagged as unable to run on {dst_name}; {src_name} scores best "
                "among the remaining engines"
            )
        updated.append(record)
    return updated
