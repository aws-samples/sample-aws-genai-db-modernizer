"""The move ledger (#616): one function records every engine change of a query.

``apply`` is the single place a routing pass sets a query's engine: it records
a :class:`~src.contracts.move_ledger.Move` (the engine the query left, where it
lands, the scores, the runner-up, the rule and the evidence) and then sets the
engine in the ledger. ``apply_to_assignment`` is the same call for a pass that
works on query assignments (a dict, or a ``QueryAssignment`` model): it records
the Move and then sets the record's ``assigned_engine`` (and ``tied_engines``)
from it. ``replay`` folds a list of Moves back into each query's engine and
tied engines, so a version's Moves can be checked against the version itself
(``unexplained`` / ``check_replay``).

This module is the only writer of ``assigned_engine`` and ``tied_engines`` (ADR-031 Amendment 3,
guarded by ``tests/unit/agents/referee/test_only_the_ledger_writes_engines.py``).
The first assignment (``AssignmentResolver.resolve``), the reality check (Phase
1 settle and Phase 2 placement, the justification floors, the model flags, the
sanity sweep and the customer-override restore), the cache owner normalisation
(``cache_overlay.normalize_cache_owners``) and the customer gate
(``assignment_overrides.apply_assignment_overrides``) record Moves, and so does
the post-schema router (#489 Track L PR4): its reroute version
(``post_schema_flag``, ``flag.capability_gap``) and its rollback version (the
reverse Moves, ``rollback.redesign_failed``, #770) in
``LocalOrchestrator._rerouted_assignment`` / ``_rollback_moves``. No writer is
left outside the ledger.

A query no engine in the customer's engine scope can serve (#1329, ADR-031 Decision 2
Amendment 15) is a Move to ``UNSERVABLE`` (``apply_unservable``), never onto an engine:
the first assignment records one for each query only an engine outside the scope could
take, and the cache owner normalisation for a legacy owner whose fallback is outside it
(``unservable_in_assignment``, which also takes the record out of
``query_assignments``). ``state_of`` gives such a query the ``UNSERVABLE`` state, so a
version's Moves replay to it.

The helpers below only read values a pass already computed (the scores it
compared, the gates it applied); none of them scores anything or decides an
engine.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any

from src.agents.referee.query_scoring import GATE_KIND_WRITE
from src.contracts.move_ledger import UNSERVABLE as UNSERVABLE
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
    "apply_unservable",
    "unservable_in_assignment",
    "best_other",
    "candidate_ladder",
    "check_replay",
    "group_key",
    "ledger_continuing",
    "ledger_for",
    "replay",
    "state_of",
    "unexplained",
]


class MoveLedger:
    """The engines of one assignment being built, and the Moves that set them.

    ``engines`` maps a query id to its current engine and ``tied`` to its
    current unresolved tie (sorted; empty when there is none); a query with no
    Move yet has no ``engines`` entry. Read them, never write them: ``apply``
    is the only writer.
    """

    def __init__(
        self,
        engines: Mapping[str, str] | None = None,
        moves: Iterable[Move] = (),
        tied: Mapping[str, Iterable[str]] | None = None,
    ) -> None:
        self.engines: dict[str, str] = dict(engines or {})
        self.moves: list[Move] = list(moves)
        self.tied: dict[str, list[str]] = {q: sorted(t) for q, t in (tied or {}).items() if t}

    def engine_of(self, query_id: str) -> str | None:
        """The query's current engine, ``None`` before its first Move."""
        return self.engines.get(query_id)

    def tied_of(self, query_id: str) -> list[str]:
        """The query's current unresolved tie (sorted), empty when there is none."""
        return list(self.tied.get(query_id, []))


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
    if move.tied_engines:
        ledger.tied[query_id] = list(move.tied_engines)
    else:
        ledger.tied.pop(query_id, None)
    return move


def apply_to_assignment(
    ledger: MoveLedger,
    qa: Any,
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
    keeps its state in query assignments: a dict (the reality check, the cache
    owner normalisation) or a ``QueryAssignment`` model (the customer gate).
    ``qa``'s ``assigned_engine`` should
    be the ledger's current engine for the query: a mismatch means something
    set the engine without a Move. It is logged, and the Move still records
    the ledger's engine as ``from_engine``, so replaying the Moves stays
    consistent and the dict ends on ``to_engine``. ``tied_engines`` is the tie the move
    leaves (empty for a decided move); ``None`` keeps the dict's current tie,
    for a pass that does not decide one (the customer-override restore). A
    Move that changes the tie records the earlier one in
    ``evidence.previous_tied_engines``.
    """
    qid = _get(qa, "query_id")
    if to_engine == UNSERVABLE:
        # A query with no engine leaves query_assignments (#1329): never an engine id
        # written into ``assigned_engine``.
        raise ValueError(
            f"move_ledger.apply_to_assignment: {qid!r} is unservable; use "
            "unservable_in_assignment, which takes it out of query_assignments"
        )
    if ledger.engine_of(qid) != _get(qa, "assigned_engine"):
        logger.warning(
            "move_ledger: query %r is on %r but the ledger has %r (an engine set without a Move)",
            qid[:80],
            _get(qa, "assigned_engine"),
            ledger.engine_of(qid),
        )
    current_tied = sorted(_get(qa, "tied_engines") or [])
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
    # The one write of a query's engine and tie (ADR-031 Amendment 3). With
    # ``tied_engines=None`` the record keeps its tie, which is what the Move
    # recorded (``tied`` above is the record's current tie then).
    if isinstance(qa, MutableMapping):
        qa["assigned_engine"] = move.to_engine
        if tied_engines is not None:
            qa["tied_engines"] = list(move.tied_engines)
    else:
        qa.assigned_engine = move.to_engine
        if tied_engines is not None:
            qa.tied_engines = list(move.tied_engines)
    return move


def apply_unservable(
    ledger: MoveLedger,
    query_id: str,
    *,
    reason_code: ReasonCode,
    closest_engine: str | None,
    evidence: MoveEvidence | None = None,
    group_key: str | None = None,
    actor: MoveActor = MoveActor.RULE,
) -> Move:
    """Record that no engine in the customer's engine scope can serve ``query_id``.

    A Move to ``UNSERVABLE`` (pass ``unservable``, #1329, ADR-031 Decision 2 Amendment
    15): the query is never forced onto an engine. ``reason_code`` is the fallback rule
    that found no engine (``fallback.no_score``, ``fallback.no_group_owner``,
    ``fallback.no_owner``); ``closest_engine``, the engine that rule names, which the
    scope leaves out, is recorded in ``evidence.closest_engine`` for context only. The
    Move clears any tie: an unservable query never joins one.
    """
    recorded = (evidence or MoveEvidence()).model_copy(update={"closest_engine": closest_engine})
    return apply(
        ledger,
        query_id,
        UNSERVABLE,
        pass_=MovePass.UNSERVABLE,
        reason_code=reason_code,
        evidence=recorded,
        group_key=group_key,
        actor=actor,
    )


def unservable_in_assignment(
    ledger: MoveLedger,
    assignment: MutableMapping[str, Any],
    qa: Mapping[str, Any],
    record: Mapping[str, Any],
    **move: Any,
) -> Move:
    """:func:`apply_unservable` for a query-assignment dict of ``assignment`` (#1329).

    Records the Move, then takes ``qa`` out of ``assignment["query_assignments"]`` and
    adds ``record`` (an ``UnservableQueryAssignment`` as JSON) to
    ``assignment["unservable_queries"]``: the one place a query leaves
    ``query_assignments`` for the unservable state.
    """
    qid = str(_get(qa, "query_id"))
    made = apply_unservable(ledger, qid, **move)
    assignment["query_assignments"] = [
        q for q in assignment.get("query_assignments") or [] if _get(q, "query_id") != qid
    ]
    kept = [u for u in assignment.get("unservable_queries") or [] if _get(u, "query_id") != qid]
    assignment["unservable_queries"] = [*kept, dict(record)]
    return made


def _get(qa: Any, key: str) -> Any:
    """A query assignment's field, from a dict or a ``QueryAssignment`` model."""
    return qa.get(key) if isinstance(qa, Mapping) else getattr(qa, key, None)


def ledger_for(query_assignments: Iterable[Any]) -> MoveLedger:
    """An empty ledger starting from each query's current engine.

    For a pass that derives a version from an earlier one: its Moves start from
    the engines the queries are on, and only its own Moves are recorded.
    """
    qas = list(query_assignments)
    return MoveLedger(
        {_get(qa, "query_id"): _get(qa, "assigned_engine") for qa in qas},
        tied={_get(qa, "query_id"): _get(qa, "tied_engines") or [] for qa in qas},
    )


def ledger_continuing(assignment: Mapping) -> MoveLedger:
    """A ledger that continues an assignment version's own Moves.

    For a change made in memory to a version that is not written as a new one
    (synthesis's and the edit surface's cache owner normalisation): the Moves
    follow the version's own in ``seq``, so the version's ``moves`` plus the
    new ones still replay to what is in memory.
    """
    moves = [
        m if isinstance(m, Move) else Move.model_validate(m)
        for m in (assignment.get("moves") or [])
    ]
    return MoveLedger(
        {
            _get(qa, "query_id"): _get(qa, "assigned_engine")
            for qa in assignment.get("query_assignments") or []
        },
        sorted(moves, key=lambda m: m.seq),
        tied={
            _get(qa, "query_id"): _get(qa, "tied_engines") or []
            for qa in assignment.get("query_assignments") or []
        },
    )


def state_of(
    query_assignments: Iterable[Any], unservable: Iterable[Any] = ()
) -> dict[str, tuple[str, list[str]]]:
    """Each query's ``(engine, tied_engines)``, the shape :func:`replay` returns.

    ``unservable`` (``Assignment.unservable_queries``, #1329) adds each such query as
    ``(UNSERVABLE, [])``, the state its Move to ``UNSERVABLE`` replays to.
    """
    state = {
        _get(qa, "query_id"): (
            _get(qa, "assigned_engine"),
            sorted(_get(qa, "tied_engines") or []),
        )
        for qa in query_assignments
    }
    for u in unservable:
        state[_get(u, "query_id")] = (UNSERVABLE, [])
    return state


def unexplained(
    moves: Iterable[Move],
    start: Mapping[str, tuple[str, list[str]]],
    end: Mapping[str, tuple[str, list[str]]],
) -> list[str]:
    """The queries whose ``end`` state does not follow from ``start`` plus ``moves``."""
    replayed = {
        qid: (engine, sorted(tied)) for qid, (engine, tied) in replay(moves, start=start).items()
    }
    return sorted(q for q in end if replayed.get(q) != (end[q][0], sorted(end[q][1])))


def check_replay(
    moves: Iterable[Move],
    start: Mapping[str, tuple[str, list[str]]],
    end: Mapping[str, tuple[str, list[str]]],
    writer: str,
) -> None:
    """Raise when ``end`` does not replay from ``start`` plus ``moves`` (#616).

    ``start`` and ``end`` are :func:`state_of` the version read and the
    version about to be written. Called by every writer of an assignment
    version before it writes: an engine or tie set without a Move (a write
    that bypassed :func:`apply`) fails here instead of reaching a version
    nothing can explain.
    """
    bad = unexplained(list(moves), start, end)
    if bad:
        raise RuntimeError(
            f"{writer}: {len(bad)} queries changed engine or tie without a Move "
            f"(the version does not replay from its input plus its moves): {bad[:5]}"
        )


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
                CandidateScore(engine=engine, score=scores.get(engine), gate=GATE_KIND_WRITE)
            )
        else:
            ladder.append(CandidateScore(engine=engine, score=scores.get(engine)))
    return ladder
