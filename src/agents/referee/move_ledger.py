"""The move ledger (#616): one function records every engine change of a query.

``apply`` is the single place a routing pass sets a query's engine: it records
a :class:`~src.contracts.move_ledger.Move` (the engine the query left, where it
lands, the scores, the runner-up, the rule and the evidence) and then sets the
engine in the ledger. ``replay`` folds a list of Moves back into each query's
engine and tied engines, so a version's Moves can be checked against the
version itself.

Today only the first assignment (``AssignmentResolver.resolve``) records Moves.
The reality check, customer overrides and the post-schema router are routed
through ``apply`` in later changes (#616, #489 Track L).

The helpers below only read values a pass already computed (the scores it
compared, the gates it applied); none of them scores anything or decides an
engine.
"""

from __future__ import annotations

import hashlib
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

__all__ = [
    "MoveLedger",
    "apply",
    "best_other",
    "candidate_ladder",
    "group_key",
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


def replay(moves: Iterable[Move]) -> dict[str, tuple[str, list[str]]]:
    """Each query's ``(engine, tied_engines)`` after its Moves, applied in ``seq`` order."""
    state: dict[str, tuple[str, list[str]]] = {}
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
