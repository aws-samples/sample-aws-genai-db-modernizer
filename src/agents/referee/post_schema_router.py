"""Post-Schema Router — Deterministic routing of unsupported queries.

After schema design, some queries are reported as unsupported by their assigned
engine, in one of two ways, both from a model:

- a PE reviewer's routing flag (``PEReviewResult.routing_flags``, the
  :class:`~src.contracts.model_flag.ModelFlag` contract), recorded in the
  engine's design trace (``design_trace.json``);
- a designer's ``unsupported_patterns`` entry in the schema output.

Either one is evidence that the reporting engine cannot serve those queries,
never a destination (ADR-031 Decision 11, as amended 2026-10-14, #629). The
router gates the reporting engine for each query and lets the shared scorer
pick the best remaining active engine (``model_flags.route_flagged_unit``:
``score_query_for_engine``, the placement eligibility rule and
``tie_break.break_tie``). There is no engine preference list.

This is NOT an LLM agent — it's pure deterministic Python code.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping
from typing import Literal

from src.agents.referee.cache_overlay import CACHE_OVERLAY_ENGINES
from src.agents.referee.consolidation_validator import scorer_ladder
from src.agents.referee.model_flags import FLAG_REASON_LABELS, parse_model_flags, route_flagged_unit
from src.agents.referee.move_ledger import group_key
from src.agents.referee.query_scoring import ScoringContext
from src.agents.referee.tie_break import group_tie
from src.agents.schema_design.design_trace import parse_design_trace
from src.contracts.model_flag import MAX_EXPLANATION_CHARS, FlagReason, ModelFlag
from src.contracts.move_ledger import MoveEvidence
from src.contracts.post_schema_router_output import (
    HeldQuery,
    QueryRouting,
    RouterOutput,
    TerminalQuery,
)
from src.contracts.schema_design_trace import DesignTrace
from src.shared.engine_names import display_engine
from src.shared.query_citations import clip

logger = logging.getLogger(__name__)


def routing_flags_from_trace(design_trace: DesignTrace | Mapping) -> list[dict]:
    """The PE reviewer's routing flags recorded in a ``design_trace.json``.

    A single design writes them at the top level; a grouped design (DynamoDB)
    has one trace per group under ``groups``. A trace read as a dict is checked as
    the contract first (#1258): a trace of the wrong shape raises
    ``DesignTraceShapeError``, never reads as no flags.
    """
    trace: DesignTrace = (
        design_trace
        if isinstance(design_trace, DesignTrace)
        else parse_design_trace(design_trace, artifact="design_trace.json")
    )
    flags = list(trace.routing_flags or [])
    for group in trace.groups or []:
        flags.extend(group.routing_flags or [])
    return flags


def _extract_unsupported_query_ids(schema_output: dict, engine: str) -> list[dict]:
    """Extract query IDs from unsupported_patterns in a schema output.

    Handles all three engine contracts:
    - DynamoDB: UnsupportedPattern has query_ids field
    - DocumentDB: UnsupportedPattern has source_query_ids field
    - OpenSearch: UnsupportedPattern has query_ids field (newly added)
    """
    unsupported = schema_output.get("unsupported_patterns", [])
    results = []
    for pattern in unsupported:
        query_ids = pattern.get("query_ids") or pattern.get("source_query_ids") or []
        reason = pattern.get("reason") or pattern.get("recommendation") or "unsupported pattern"
        if query_ids:
            results.append({"query_ids": query_ids, "reason": reason})
    return results


def _unsupported_as_flags(schema_output: dict, engine: str) -> list[ModelFlag]:
    """A designer's unsupported patterns, read as flags on ``engine``."""
    return [
        ModelFlag(
            query_ids=[str(q) for q in entry["query_ids"]],
            flagged_engine=engine,  # type: ignore[arg-type]  # checked by the caller
            reason_category=FlagReason.MISSING_FEATURE,
            # Cut on a label boundary: the gate may have written a query's label
            # (#1321), and a cut inside it would leave an unpaired backtick.
            explanation=clip(str(entry["reason"]), MAX_EXPLANATION_CHARS) or "unsupported pattern",
        )
        for entry in _extract_unsupported_query_ids(schema_output, engine)
    ]


def _engine_flags(engine: str, raw_flags: Iterable[object], schema_output: dict) -> list[ModelFlag]:
    """Every valid flag on ``engine``: the PE reviewer's first, then the designer's."""
    pe_flags, _rejected = parse_model_flags(raw_flags)
    kept: list[ModelFlag] = []
    for flag in pe_flags:
        if flag.flagged_engine != engine:
            logger.warning(
                "Ignored a PE routing flag from %s on %r: a reviewer flags only its own engine",
                engine,
                flag.flagged_engine,
            )
            continue
        kept.append(flag)
    try:
        kept.extend(_unsupported_as_flags(schema_output, engine))
    except ValueError:  # an engine id outside the flag contract
        logger.warning("Ignored unsupported patterns from unknown engine %r", engine)
    return kept


def _terminal(
    flagged: list[tuple[str, ModelFlag]],
    engine: str,
    cause: Literal["no_kept_engine", "depth_limit"],
) -> list[TerminalQuery]:
    """The ``RouterOutput.terminal`` records of the queries flagged on ``engine`` whose
    unit stays there (#888). A unit member nobody flagged gets none: the record
    claims a review found the engine cannot serve it."""
    return [
        TerminalQuery(
            query_id=m,
            flagged_engine=engine,
            reason_category=flag.reason_category.value,
            cause=cause,
        )
        for m, flag in flagged
    ]


def _held(flagged: list[tuple[str, ModelFlag]], engine: str) -> list[HeldQuery]:
    """The ``RouterOutput.held`` records of the queries flagged on ``engine`` that a
    customer pin keeps there (#1033); an unflagged unit member gets none."""
    return [
        HeldQuery(query_id=m, flagged_engine=engine, reason_category=flag.reason_category.value)
        for m, flag in flagged
    ]


def _fmt(value: float | None) -> str:
    return "n/a" if value is None else f"{value:g}"


def route_unsupported_queries(
    schema_outputs: dict[str, dict],
    active_engines: list[str],
    routing_flags_by_engine: dict[str, list] | None = None,
    scoring_context: ScoringContext | None = None,
    query_map: Mapping[str, Mapping] | None = None,
    cascade_depth: int = 0,
    max_depth: int = 2,
    already_routed: set[str] | None = None,
    *,
    assigned_engines: Mapping[str, str],
    customer_pinned: set[str] | None = None,
    co_dependency_groups: list[list[str]] | None = None,
    query_signals: Mapping[str, list[str]] | None = None,
    source_engine: str = "",
    settled: Mapping[str, int] | None = None,
    tied_engines: Mapping[str, list[str]] | None = None,
    write_groups: Mapping[str, list[str]] | None = None,
    unit_weights: Mapping[str, float] | None = None,
    pinned_counts: Mapping[str, list[str]] | None = None,
) -> RouterOutput:
    """Route unsupported queries to the best remaining engine by score.

    Args:
        schema_outputs: {engine: schema_output_dict} for each engine
        active_engines: the candidate engines: the engines the approved
            assignment keeps (in-scope queries after the reality check). A
            removed engine is never a destination (maintainer decision,
            2026-10-14, #629)
        routing_flags_by_engine: {engine: raw PE routing flags} from each
            engine's design trace (``routing_flags_from_trace``); each must
            match :class:`ModelFlag` and flag its own engine
        scoring_context: the shared scorer's context
            (``ScoringContext.from_pipeline``); an empty one scores every
            engine 0, so ties decide
        query_map: {query_id: collector query pattern}, for scoring and the
            hard exclusion gate
        cascade_depth: current depth (0 = first pass)
        max_depth: maximum cascade depth before declaring terminal
        already_routed: query IDs already routed in prior passes
        assigned_engines: {query_id: engine} from the assignment schema design
            worked on. A flag (or unsupported pattern) counts only for a query
            on the reporting engine -- a reviewer judges only its own engine's
            queries, the same rule the reality check applies (#712)
        customer_pinned: query ids the customer re-routed at the review
            (``customer_override``). Held out, the way the reality check holds
            them out: a later flag never moves a customer's own choice (#713
            review round 3)
        co_dependency_groups: the assignment's groups; members on the same
            engine as a flagged query move with it as one unit (Decision 4)
        query_signals / source_engine / settled: the eligibility rule's triage
            signals, the source database engine and the settled workload, for
            the placement rule and the shared tie-break
        tied_engines: {query_id: its unresolved tie} from the same assignment.
            A flagged query (or group, using ``tie_break.group_tie``) is
            re-routed among its other tied engines first and keeps the tie
            minus the flagged engine when the scorer leaves it unresolved,
            exactly as the reality check's model-flag path does (ADR-031
            Decisions 11 Amendment 2 and 12, #811)
        write_groups: ``Assignment.write_groups`` (#992). A table's writes stay on
            one engine (ADR-031 Amendment 9): a flagged unit whose write group
            holds a customer-pinned write stays where it is, the flag a note on
            it (``RouterOutput.held_queries``), since the pinned write never moves
        unit_weights: ``write_groups.assignment_unit_weights``: the call-rate
            weights a unit holding a write group is judged on (#992)
        pinned_counts: ``waived_counts.pinned_counts_by_table``: per table, the
            customer-pinned key-scoped counts its writes' placement lets their
            engine serve (#1151). Each holds the table's writes like a pinned write
            does: moving them would end the count's waiver, and a rule never moves a
            customer's pin (ADR-028)

    Returns:
        RouterOutput with routing decisions and terminal queries
    """
    already_routed = already_routed or set()
    customer_pinned = customer_pinned or set()
    routing_flags_by_engine = routing_flags_by_engine or {}
    scoring_context = scoring_context or ScoringContext.from_pipeline({})
    query_map = query_map or {}
    tied_engines = tied_engines or {}

    routings: list[QueryRouting] = []
    terminal: list[TerminalQuery] = []
    terminal_ids: list[str] = []
    held: list[str] = []
    held_records: list[HeldQuery] = []
    # query -> the other writes of the tables it writes (#992).
    write_mates: dict[str, set[str]] = defaultdict(set)
    for members in (write_groups or {}).values():
        for member in members:
            write_mates[member].update(members)
    # A pinned waived count is a mate of its table's writes (#1151): it holds them.
    for table, counts in (pinned_counts or {}).items():
        for member in (write_groups or {}).get(table, []):
            write_mates[member].update(counts)
    candidates = [e for e in active_engines if e not in CACHE_OVERLAY_ENGINES]

    group_of: dict[str, list[str]] = {}
    for group in co_dependency_groups or []:
        if len(group) >= 2:
            for member in group:
                group_of[member] = sorted(group)

    # Every (query, engine) a model flagged on the engine the query is on.
    pending: list[tuple[str, str, ModelFlag]] = []
    failed_engines: dict[str, set[str]] = defaultdict(set)
    for engine, output in schema_outputs.items():
        # A cache engine owns none of its queries: one its design cannot serve is
        # still served by its owner, so it is not re-routed. The synthesis safety
        # net drops its cache overlay instead (#296).
        if engine in CACHE_OVERLAY_ENGINES:
            continue
        for flag in _engine_flags(engine, routing_flags_by_engine.get(engine, []), output):
            for qid in flag.query_ids:
                if qid in already_routed:
                    continue
                if qid in customer_pinned:
                    logger.info(
                        "Held out a flag from %s on %r: the customer chose this engine",
                        engine,
                        str(qid)[:80],
                    )
                    continue
                if assigned_engines.get(qid) != engine:
                    logger.warning(
                        "Ignored a flag from %s on %r: the query is not on that engine",
                        engine,
                        str(qid)[:80],
                    )
                    continue
                failed_engines[qid].add(engine)
                pending.append((qid, engine, flag))

    # Each (query, engine) flag, so a unit's records name only the queries a review flagged.
    flag_of: dict[tuple[str, str], ModelFlag] = {}
    for qid, engine, flag in pending:
        flag_of.setdefault((qid, engine), flag)

    gated = scoring_context.with_flag_gaps(failed_engines)
    seen: set[str] = set()
    for qid, engine, flag in pending:
        if qid in seen:
            continue
        members = sorted(
            {qid}
            | {
                m
                for m in group_of.get(qid, [])
                if assigned_engines.get(m) == engine
                and m not in already_routed
                and m not in customer_pinned
            }
        )
        seen.update(members)
        flagged = [(m, flag_of.get((m, engine), flag)) for m in members if (m, engine) in flag_of]
        # A write group with a pinned write stays together on the pinned engine:
        # moving the rest would split the table's writes (ADR-031 Amendment 9).
        pinned_mates = {
            m for member in members for m in write_mates.get(member, ()) if m in customer_pinned
        }
        if pinned_mates:
            logger.info(
                "Held a flag from %s on %d queries: their table's writes include a write the "
                "customer pinned there",
                engine,
                len(members),
            )
            held.extend(m for m in members if m not in held)
            held_records.extend(_held(flagged, engine))
            continue
        if cascade_depth >= max_depth:
            terminal.extend(_terminal(flagged, engine, "depth_limit"))
            terminal_ids.extend(m for m in members if m not in terminal_ids)
            continue
        failed = set().union(*(failed_engines.get(m, set()) for m in members))
        remaining = [e for e in candidates if e not in failed]
        # The unit's tie (#833): the engines every tied member is tied on,
        # the reality check's rule (``tie_break.group_tie``).
        tie = group_tie(members, tied_engines.get) or None
        routing = route_flagged_unit(
            members,
            engine,
            remaining,
            gated,
            query_map,
            query_signals=query_signals,
            source_engine=source_engine,
            settled=settled,
            tied_engines=tie,
            weights=unit_weights,
        )
        if routing.destination is None:
            terminal.extend(_terminal(flagged, engine, "no_kept_engine"))
            terminal_ids.extend(m for m in members if m not in terminal_ids)
            continue
        label = FLAG_REASON_LABELS[flag.reason_category]
        basis = (
            "with its co-dependency group, the group's best-scoring remaining engine"
            if len(members) > 1
            else "the best-scoring remaining engine"
        )
        reason = (
            f"flagged on {display_engine(engine)} ({label}); moved to "
            f"{display_engine(routing.destination)}, {basis} (score "
            f"{_fmt(routing.score_after)} against {_fmt(routing.score_before)} on "
            f"{display_engine(engine)})"
        )
        grouped = len(members) > 1
        for member in members:
            routings.append(
                QueryRouting(
                    query_id=member,
                    from_engine=engine,
                    to_engine=routing.destination,
                    reason=reason,
                    cascade_depth=cascade_depth,
                    reason_category=flag.reason_category.value,
                    explanation=flag.explanation,
                    score_before=routing.score_before,
                    score_after=routing.score_after,
                    # What the post_schema_flag Move records (#616), the same
                    # record the reality check's model_flag Move keeps.
                    runner_up=routing.runner_up,
                    evidence=MoveEvidence(
                        candidates=scorer_ladder(
                            query_map.get(member) or {"query_id": member}, gated
                        ),
                        group_scores=dict(routing.scores) if grouped else {},
                        tie_rule=routing.tie_rule,
                    ),
                    group_key=group_key(group_of[member]) if grouped else None,
                    tied_engines=list(routing.tied),
                )
            )

    return RouterOutput(
        job_id="",  # Caller sets this
        routings=routings,
        terminal_queries=terminal_ids,
        terminal=terminal,
        held_queries=held,
        held=held_records,
        cascade_depth=cascade_depth,
    )
