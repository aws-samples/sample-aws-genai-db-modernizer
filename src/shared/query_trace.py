"""The per-query decision trace, built once from the move ledger (#339).

Why a query ended on engine X and not Y: its first assignment with every
engine's score or gate, every later Move across the assignment versions with
its reason, the final engine, the runner-up of the final plan with its gap or
gate, and the engines that scored higher with the step that decided against
each (``src.contracts.query_trace``).

Two readers, one implementation:

- the context graph (``src.graph.queries.query_trace``) reads the query's
  ``MOVED`` edges and calls :func:`build_query_trace`; the trace API and the
  UI's "Why this engine" section show the result;
- synthesis reads the assignment lineage itself (:func:`assignment_lineage`,
  :func:`ledger_moves`) and writes the traces of the queries whose routing has
  a story into ``report.json`` (``query_traces``) for the engineering report.

Every sentence comes from ``src.shared.move_reasons``. A record only: nothing
here changes routing.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any

from pydantic import ValidationError

from src.contracts.move_ledger import CandidateScore, Move, ReasonCode
from src.contracts.query_trace import (
    CloseCall,
    QueryTraceDigest,
    QueryTraceEntry,
    QueryTraceListing,
    QueryTraceResponse,
    TraceBasis,
    TraceCandidate,
    TraceEngineScore,
    TraceOutscored,
    TraceRuleCount,
    TraceRunnerUp,
    TraceStep,
)
from src.shared.engine_names import display_engine
from src.shared.move_reasons import (
    GROUP_OWNER_GATE,
    gate_text,
    render_move_reason,
    render_outscored,
    render_runner_up,
    render_trace_summary,
    rule_label,
)
from src.shared.report_facts import (
    TRACE_FULL_CAP,
    is_close_call,
    is_listed,
    trace_kinds,
    trace_order,
)

# One decimal, like every score the trace shows: a co-dependency group's
# mean (80.1340206185567) is stored as 80.1, never next to a whole-number score.
_PLACES = 1


def _round(score: float | None) -> float | None:
    return None if score is None else round(float(score), _PLACES)


def assignment_lineage(read: Callable[[int], dict | None], effective_version: int) -> list[dict]:
    """The assignment versions the effective one descends from, oldest first.

    Follows ``previous_version`` from ``effective_version`` back to the first
    assignment (``previous_version`` None), so a version from an abandoned
    lineage (an earlier resolution the effective one does not derive from)
    is never part of the history. ``read(v)`` returns version ``v`` or None.
    """
    chain: list[dict] = []
    seen: set[int] = set()
    version: int | None = effective_version
    while isinstance(version, int) and version >= 1 and version not in seen:
        seen.add(version)
        assignment = read(version)
        if not assignment:
            break
        chain.append({**assignment, "version": assignment.get("version") or version})
        version = assignment.get("previous_version")
    return list(reversed(chain))


def version_moves(assignment: dict) -> list[Move]:
    """One version's Moves (``assignment.moves``); an unreadable record is skipped."""
    moves: list[Move] = []
    for raw in assignment.get("moves") or []:
        try:
            moves.append(Move.model_validate(raw))
        except ValidationError:
            continue
    return moves


def ledger_moves(lineage: Iterable[dict]) -> dict[str, list[tuple[int, Move]]]:
    """Every query's Moves across the lineage, as (version, Move), in version and seq order."""
    by_query: dict[str, list[tuple[int, Move]]] = {}
    for assignment in lineage:
        version = int(assignment.get("version") or 0)
        if version < 1:
            continue
        for move in sorted(version_moves(assignment), key=lambda m: m.seq):
            by_query.setdefault(move.query_id, []).append((version, move))
    return by_query


def _candidate(c: CandidateScore) -> TraceCandidate:
    return TraceCandidate(
        engine=c.engine,
        engine_name=display_engine(c.engine),
        score=c.score,
        gate=c.gate,
        gate_text=gate_text(c.gate),
    )


def _step(version: int, move: Move, number: int) -> TraceStep:
    return TraceStep(
        version=version,
        seq=move.seq,
        step=number,
        pass_=move.pass_,
        actor=move.actor,
        reason_code=move.reason_code,
        rule=rule_label(move.reason_code),
        from_engine=move.from_engine,
        from_engine_name=display_engine(move.from_engine) if move.from_engine else None,
        to_engine=move.to_engine,
        to_engine_name=display_engine(move.to_engine),
        score_before=move.score_before,
        score_after=move.score_after,
        runner_up=(
            TraceEngineScore(
                engine=move.runner_up.engine,
                engine_name=display_engine(move.runner_up.engine),
                score=move.runner_up.score,
            )
            if move.runner_up
            else None
        ),
        tied_engines=list(move.tied_engines),
        tied_engine_names=[display_engine(e) for e in move.tied_engines],
        group_key=move.group_key,
        candidates=[_candidate(c) for c in move.evidence.candidates],
        explanation=render_move_reason(move),
    )


_TIE_RULES = frozenset(
    {
        ReasonCode.TIE_SOURCE_COMPATIBLE,
        ReasonCode.TIE_STABILITY,
        ReasonCode.TIE_PLACEHOLDER_SETTLED_WORKLOAD,
        ReasonCode.TIE_PLACEHOLDER_HASH,
        ReasonCode.TIE_CLEARED,
    }
)


def _is_settled_tie(trace: QueryTraceResponse, moves: list[tuple[int, Move]]) -> bool:
    """A close call that is an exact tie a routing rule settled (#730): the
    runner-up scores the same as the final engine and a tie rule decided the
    query (a tie Move or a Move's ``evidence.tie_rule``), not just a gap that
    rounds to 0."""
    runner = trace.runner_up
    return (
        runner is not None
        and runner.gap == 0
        and runner.score == runner.final_score
        and _latest_tie_rule(moves) is not None
    )


def _deciding_step(final_engine: str, moves: list[tuple[int, Move]]) -> tuple[int, Move]:
    """The Move that put the query on its final engine (not a later tie-only change)."""
    for version, move in reversed(moves):
        if move.to_engine == final_engine and move.from_engine != move.to_engine:
            return version, move
    return moves[-1]


def _latest_tie_rule(moves: list[tuple[int, Move]]) -> ReasonCode | None:
    """The rule of the query's latest tie decision, if a tie was ever involved."""
    for _, move in reversed(moves):
        if move.reason_code in _TIE_RULES:
            return move.reason_code
        if move.evidence.tie_rule is not None:
            return move.evidence.tie_rule
    return None


def _comparison(
    final_engine: str, moves: list[tuple[int, Move]]
) -> tuple[dict[str, float], dict[str, CandidateScore], float | None, bool]:
    """The scores the deciding pass compared, the latest ladder, the final score, the basis.

    A co-dependency group's Move compared the group's mean scores
    (``evidence.group_scores``), so the trace compares those too; any other
    Move compared the query's own ladder (eligible rungs only).
    """
    _, decider = _deciding_step(final_engine, moves)
    ladder_list: list[CandidateScore] = decider.evidence.candidates or next(
        (m.evidence.candidates for _, m in reversed(moves) if m.evidence.candidates), []
    )
    ladder = {c.engine: c for c in ladder_list}
    if decider.evidence.group_scores:
        scores = dict(decider.evidence.group_scores)
        return scores, ladder, scores.get(final_engine), True
    scores = {c.engine: float(c.score) for c in ladder_list if not c.gate and c.score is not None}
    final = ladder.get(final_engine)
    final_score = (
        final.score
        if final is not None and not final.gate
        else (decider.score_after if decider.to_engine == final_engine else None)
    )
    return scores, ladder, final_score, False


def _runner_up(
    final_engine: str,
    moves: list[tuple[int, Move]],
    plan: set[str],
) -> tuple[TraceRunnerUp | None, list[TraceOutscored]]:
    """The runner-up (an engine of the final plan) and the engines that scored higher.

    The runner-up is the best-scoring engine the final assignment still uses
    that could take the query and did not score above the final engine, on
    the scale the deciding pass compared (the group's means for a
    co-dependency Move), so its gap is never negative. When every other
    engine of the plan is gated, the gated one with the best kept score.
    Every engine that scored higher (removed, consolidated or overridden) is
    listed apart with the step that decided against it. A record only.
    """
    scores, ladder, final_score, group = _comparison(final_engine, moves)
    basis: TraceBasis = "group_mean" if group else "query"
    tie_rule = _latest_tie_rule(moves)
    decider_version, decider = _deciding_step(final_engine, moves)

    outscored: list[TraceOutscored] = []
    if final_score is not None:
        higher = sorted(
            ((e, sc) for e, sc in scores.items() if e != final_engine and sc > final_score),
            key=lambda es: (-es[1], es[0]),
        )
        for better, better_score in higher:
            left = next(
                (
                    (v, m)
                    for v, m in reversed(moves)
                    if m.from_engine == better and m.to_engine != better
                ),
                None,
            )
            step_version, step = left or (decider_version, decider)
            in_plan = better in plan
            outscored.append(
                TraceOutscored(
                    engine=better,
                    engine_name=display_engine(better),
                    score=round(better_score, _PLACES),
                    final_score=round(final_score, _PLACES),
                    basis=basis,
                    in_plan=in_plan,
                    decided_by_version=step_version,
                    decided_by_seq=step.seq,
                    decided_by_step=next(
                        (
                            i
                            for i, (v, m) in enumerate(moves, start=1)
                            if v == step_version and m.seq == step.seq
                        ),
                        1,
                    ),
                    decided_by_rule=step.reason_code,
                    left_it=left is not None,
                    explanation=render_outscored(
                        final_engine,
                        final_score,
                        better,
                        better_score,
                        step,
                        step_version,
                        left is not None,
                        in_plan,
                        group,
                    ),
                )
            )

    eligible = [
        (e, sc)
        for e, sc in scores.items()
        if e != final_engine and e in plan and (final_score is None or sc <= final_score)
    ]
    engine: str | None = None
    score: float | None = None
    gate: str | None = None
    if eligible:
        top = max(sc for _, sc in eligible)
        engine, score = min(e for e, sc in eligible if sc == top), top
    elif not any(e != final_engine and e in plan for e in scores):
        # Nobody else in the plan could take it: the closest engine that could
        # not, with its gate. For a group, an engine missing from the group's
        # means could not own every member (its own score is kept).
        blocked = [
            c
            for e, c in ladder.items()
            if e != final_engine and e in plan and (c.gate or (group and e not in scores))
        ]
        if blocked:
            best = min(blocked, key=lambda c: (-(c.score if c.score is not None else -1), c.engine))
            # On a group's scale a blocked engine has no mean (it is missing
            # from the group's scores): its own per-query score would sit next
            # to the final engine's group mean on another scale (#818 review).
            engine, score = best.engine, (None if group else best.score)
            gate = best.gate or GROUP_OWNER_GATE
    if engine is None:
        return None, outscored
    gap = (
        round(final_score - score, 1)
        if final_score is not None and score is not None and not gate
        else None
    )
    runner = TraceRunnerUp(
        engine=engine,
        engine_name=display_engine(engine),
        score=_round(score),
        final_score=_round(final_score),
        basis=basis,
        gap=gap,
        gate=gate,
        explanation=render_runner_up(
            final_engine, final_score, engine, score, gate, tie_rule, group
        ),
    )
    return runner, outscored


def build_query_trace(
    query_id: str,
    moves: list[tuple[int, Move]],
    final_engine: str | None,
    plan: set[str],
    effective_version: int | None = None,
) -> QueryTraceResponse:
    """The query's decision trace from its Moves (``(version, Move)``, in order).

    ``final_engine`` is the effective assignment's engine for the query (the
    last Move's engine when unknown) and ``plan`` the engines the effective
    assignment uses. With no Moves (a job written before the move ledger)
    ``has_ledger`` is False and only the final engine is set.
    """
    if not moves:
        return QueryTraceResponse(
            query_id=query_id,
            has_ledger=False,
            final_engine=final_engine,
            final_engine_name=display_engine(final_engine) if final_engine else None,
            effective_version=effective_version,
            summary=(
                f"Ends on {display_engine(final_engine)}; this assessment predates the "
                "per-query decision record."
                if final_engine
                else ""
            ),
        )
    steps = [_step(v, m, i) for i, (v, m) in enumerate(moves, start=1)]
    last_version, last = moves[-1]
    final = final_engine or last.to_engine
    final_score = last.score_after if last.to_engine == final else None
    runner_up, outscored = _runner_up(final, moves, plan or {final})
    _, _, compared, group = _comparison(final, moves)
    return QueryTraceResponse(
        query_id=query_id,
        has_ledger=True,
        first_assignment=steps[0],
        moves=steps[1:],
        final_engine=final,
        final_engine_name=display_engine(final),
        final_version=last_version,
        effective_version=effective_version,
        final_score=final_score,
        final_group_mean=_round(compared) if group else None,
        tied_engines=list(last.tied_engines),
        tied_engine_names=[display_engine(e) for e in last.tied_engines],
        runner_up=runner_up,
        outscored_by=outscored,
        summary=render_trace_summary(
            final,
            final_score,
            len(steps),
            runner_up.explanation if runner_up else None,
            last,
        ),
    )


# How much of a query's own SQL identifies it in a listing.
EXCERPT_CHARS = 120


def query_excerpt(text: str | None, limit: int = EXCERPT_CHARS) -> str | None:
    """The start of a query's SQL, whitespace collapsed, cut at ``limit`` characters."""
    if not text:
        return None
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "\u2026"


def query_trace_digest(
    effective: dict,
    read: Callable[[int], dict | None],
    effective_version: int,
    query_texts: Mapping[str, str] | None = None,
) -> QueryTraceDigest:
    """The decision traces synthesis writes into report.json (``query_traces``).

    ``effective`` is the effective assignment (version ``effective_version``),
    ``read(v)`` reads any version, for the lineage, and ``query_texts`` maps a
    query id to its collected SQL (for the excerpt that identifies it). Every
    query of the effective assignment is traced. The ones ``trace_kinds``
    lists (moved after the first assignment, tied at some step, overridden,
    pinned by a listed rule) are ordered most notable first (``trace_order``);
    the first ``TRACE_FULL_CAP`` keep their full trace, the rest only why they
    are listed. The others are counted by their first assignment's rule.
    Every query whose runner-up ``is_close_call`` is recorded in
    ``close_calls`` too, listed or not (#730).
    """
    qas = [qa for qa in effective.get("query_assignments") or [] if qa.get("query_id")]
    by_query = ledger_moves(assignment_lineage(read, effective_version))
    plan = {qa["assigned_engine"] for qa in qas if qa.get("assigned_engine")}
    texts = query_texts or {}
    listed: list[tuple[dict[str, Any], QueryTraceEntry]] = []
    rules: dict[ReasonCode, int] = {}
    close: list[CloseCall] = []
    for qa in qas:
        qid = str(qa["query_id"])
        trace = build_query_trace(
            qid, by_query.get(qid, []), qa.get("assigned_engine"), plan, effective_version
        )
        dumped = trace.model_dump(mode="json", by_alias=True)
        kinds = trace_kinds(dumped)
        if trace.runner_up is not None and trace.final_engine and is_close_call(dumped):
            close.append(
                CloseCall(
                    query_id=qid,
                    engine=trace.final_engine,
                    alternative=trace.runner_up.engine,
                    score=trace.runner_up.final_score,
                    alternative_score=trace.runner_up.score,
                    gap=float(trace.runner_up.gap or 0.0),
                    basis=trace.runner_up.basis,
                    settled_tie=_is_settled_tie(trace, by_query.get(qid, [])),
                )
            )
        if is_listed(kinds):
            entry = QueryTraceEntry(
                **trace.model_dump(by_alias=True),
                source_tables=[str(t) for t in qa.get("source_tables") or []],
                query_excerpt=query_excerpt(texts.get(qid)),
            )
            listed.append(({"query_id": qid, **kinds}, entry))
        elif trace.first_assignment is not None:
            code = trace.first_assignment.reason_code
            rules[code] = rules.get(code, 0) + 1
    listed.sort(key=lambda pair: trace_order(pair[0]))
    return QueryTraceDigest(
        has_ledger=bool(by_query),
        query_count=len(qas),
        effective_version=effective_version,
        queries=[entry for _, entry in listed[:TRACE_FULL_CAP]],
        more_listed=[QueryTraceListing(**row) for row, _ in listed[TRACE_FULL_CAP:]],
        unlisted_rules=[
            TraceRuleCount(rule=code, label=rule_label(code), query_count=n)
            for code, n in sorted(rules.items(), key=lambda kv: (-kv[1], kv[0].value))
        ],
        close_calls=sorted(close, key=lambda c: (c.engine, c.alternative, c.gap, c.query_id)),
    )
