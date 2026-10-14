"""The move ledger (#616): one function records every engine change of a query.

``apply`` is the single place a routing pass sets a query's engine: it records
a :class:`~src.contracts.move_ledger.Move` (the engine the query left, where it
lands, the scores, the runner-up, the rule and the evidence) and then sets the
engine in the ledger. ``apply_to_assignment`` is the same call for a pass that
works on query-assignment dicts (the reality check): it records the Move and
then sets the dict's ``assigned_engine`` (and ``tied_engines``) from it.
``replay`` folds a list of Moves back into each query's engine and tied
engines, so a version's Moves can be checked against the version itself.

The first assignment (``AssignmentResolver.resolve``) and the reality check
(Phase 1 settle and Phase 2 placement, the justification floors, the model
flags, the sanity sweep and the customer-override restore) record Moves. Not
yet routed through ``apply`` (#616, #489 Track L): the reality check handler's
``normalize_cache_owners`` (ElastiCache owners in an assignment written before
#296; Track L PR3), the customer gate and the post-schema router.

The helpers below only read values a pass already computed (the scores it
compared, the gates it applied); none of them scores anything or decides an
engine.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Mapping

from src.contracts.move_ledger import (
    CandidateScore,
    EngineScore,
    Move,
    MoveActor,
    MoveEvidence,
    MovePass,
    ReasonCode,
)

logger = logging.getLogger(__name__)

__all__ = [
    "MoveLedger",
    "apply",
    "apply_to_assignment",
    "best_other",
    "candidate_ladder",
    "group_key",
    "ledger_for",
    "replay",
]


class MoveLedger:
    """The engines of one assignment being built, and the Moves that set them.

    ``engines`` maps a query id to its current engine; a query with no Move
    yet has no entry. Read it, never write it: ``apply`` is the only writer.
    """

    def __init__(
        self,
        engines: Mapping[str, str] | None = None,
        moves: Iterable[Move] = (),
    ) -> None:
        self.engines: dict[str, str] = dict(engines or {})
        self.moves: list[Move] = list(moves)

    def engine_of(self, query_id: str) -> str | None:
        """The query's current engine, ``None`` before its first Move."""
        return self.engines.get(query_id)


def apply(
    ledger: MoveLedger,
    query_id: str,
    to_engine: str,
    *,
    pass_: MovePass,
    reason_code: ReasonCode,
    score_after: float | None = None,
    score_before: float | None = None,
    runner_up: EngineScore | None = None,
    evidence: MoveEvidence | None = None,
    group_key: str | None = None,
    tied_engines: Iterable[str] = (),
    actor: MoveActor = MoveActor.RULE,
) -> Move:
    """Record one Move and set ``query_id``'s engine to ``to_engine``.

    ``from_engine`` is always the ledger's current engine for the query
    (``None`` for its first assignment), never something a caller passes, so
    the record cannot disagree with the state it changed. A first assignment
    has no ``score_before``.
    """
    if not to_engine:
        raise ValueError(f"move_ledger.apply: empty to_engine for query {query_id!r}")
    from_engine = ledger.engine_of(query_id)
    if from_engine is None and score_before is not None:
        raise ValueError(
            f"move_ledger.apply: query {query_id!r} has no engine yet, so no score_before"
        )
    seq = ledger.moves[-1].seq + 1 if ledger.moves else 1
    move = Move(
        seq=seq,
        query_id=query_id,
        pass_=pass_,
        from_engine=from_engine,
        to_engine=to_engine,
        score_before=score_before,
        score_after=score_after,
        runner_up=runner_up,
        reason_code=reason_code,
        evidence=evidence or MoveEvidence(),
        group_key=group_key,
        tied_engines=sorted(tied_engines),
        actor=actor,
    )
    ledger.moves.append(move)
    ledger.engines[query_id] = to_engine
    return move


def apply_to_assignment(
    ledger: MoveLedger,
    qa: dict,
    to_engine: str,
    *,
    pass_: MovePass,
    reason_code: ReasonCode,
    score_after: float | None = None,
    score_before: float | None = None,
    runner_up: EngineScore | None = None,
    evidence: MoveEvidence | None = None,
    group_key: str | None = None,
    tied_engines: Iterable[str] | None = None,
    actor: MoveActor = MoveActor.RULE,
) -> Move:
    """Record one Move for a query-assignment dict, then set the dict from it.

    The same as :func:`apply` (the keywords are passed through), for a pass that
    keeps its state in query-assignment dicts. ``qa["assigned_engine"]`` should
    be the ledger's current engine for the query: a mismatch means something
    set the engine without a Move. It is logged, and the Move still records
    the ledger's engine as ``from_engine``, so replaying the Moves stays
    consistent and the dict ends on ``to_engine``. ``tied_engines`` is the tie the move
    leaves (empty for a decided move); ``None`` keeps the dict's current tie,
    for a pass that does not decide one (the customer-override restore). A
    Move that changes the tie records the earlier one in
    ``evidence.previous_tied_engines``.
    """
    qid = qa["query_id"]
    if ledger.engine_of(qid) != qa.get("assigned_engine"):
        logger.warning(
            "move_ledger: query %r is on %r but the ledger has %r (an engine set without a Move)",
            qid[:80],
            qa.get("assigned_engine"),
            ledger.engine_of(qid),
        )
    current_tied = sorted(qa.get("tied_engines") or [])
    tied = current_tied if tied_engines is None else list(tied_engines)
    if sorted(tied) != current_tied:
        # The tie changes with this Move: keep the earlier one in evidence.
        evidence = (evidence or MoveEvidence()).model_copy(
            update={"previous_tied_engines": current_tied}
        )
    move = apply(
        ledger,
        qid,
        to_engine,
        pass_=pass_,
        reason_code=reason_code,
        score_after=score_after,
        score_before=score_before,
        runner_up=runner_up,
        evidence=evidence,
        group_key=group_key,
        tied_engines=tied,
        actor=actor,
    )
    qa["assigned_engine"] = move.to_engine
    if tied_engines is not None:
        qa["tied_engines"] = list(move.tied_engines)
    return move


def ledger_for(query_assignments: Iterable[Mapping]) -> MoveLedger:
    """An empty ledger starting from each query's current engine.

    For a pass that derives a version from an earlier one: its Moves start from
    the engines the queries are on, and only its own Moves are recorded.
    """
    return MoveLedger({qa["query_id"]: qa["assigned_engine"] for qa in query_assignments})


def replay(
    moves: Iterable[Move],
    start: Mapping[str, tuple[str, list[str]]] | None = None,
) -> dict[str, tuple[str, list[str]]]:
    """Each query's ``(engine, tied_engines)`` after its Moves, applied in ``seq`` order.

    ``start`` is the state the Moves apply to: a derived version's Moves replay
    on top of its input version (``replay(v2.moves, start=replay(v1.moves))``).
    """
    state: dict[str, tuple[str, list[str]]] = {
        qid: (engine, list(tied)) for qid, (engine, tied) in (start or {}).items()
    }
    for move in sorted(moves, key=lambda m: m.seq):
        state[move.query_id] = (move.to_engine, list(move.tied_engines))
    return state


def group_key(members: Iterable[str]) -> str:
    """A co-dependency group's stable 12-hex key, hashed from its sorted member ids."""
    return hashlib.sha256(",".join(sorted(members)).encode()).hexdigest()[:12]


def best_other(scores: Mapping[str, float], winner: str) -> EngineScore | None:
    """The highest-scoring engine in ``scores`` other than ``winner`` (the runner-up).

    A record only, never a decision: when several engines share the runner-up
    score, the first by engine id is named so the record is stable.
    """
    others = [(e, s) for e, s in scores.items() if e != winner]
    if not others:
        return None
    top = max(s for _, s in others)
    engine = min(e for e, s in others if s == top)
    return EngineScore(engine=engine, score=top)


def candidate_ladder(
    engines: Iterable[str],
    scores: Mapping[str, float],
    gates: Mapping[str, str],
    write_gated: Iterable[str] = (),
) -> list[CandidateScore]:
    """Every engine's score or gate for one query, sorted by engine id.

    ``gates`` holds the scorer's hard gate kind per excluded engine (its score
    is ``None``); ``write_gated`` the engines that may not own the query (their
    score is kept, with ``gate="write_gate"``).
    """
    write_gated_set = set(write_gated)
    ladder: list[CandidateScore] = []
    for engine in sorted(engines):
        gate = gates.get(engine)
        if gate:
            ladder.append(CandidateScore(engine=engine, score=None, gate=gate))
        elif engine in write_gated_set:
            ladder.append(
                CandidateScore(engine=engine, score=scores.get(engine), gate="write_gate")
            )
        else:
            ladder.append(CandidateScore(engine=engine, score=scores.get(engine)))
    return ladder
