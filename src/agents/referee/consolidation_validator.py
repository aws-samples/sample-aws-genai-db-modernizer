"""Consolidation Validator — LLM-based validation of deterministic consolidation decisions.

After the deterministic reality check decides to consolidate queries from one engine
to another, this module asks an LLM to validate: "Can the target engine actually serve
these query patterns, or will they fail during schema design?"

This prevents the flip-flop UX: "You don't need DocumentDB" → schema design fails →
"Actually you do need DocumentDB." The LLM catches soft failures that regex/scoring miss.

Architecture:
- Production: Strands + Bedrock (same pattern as executive summary)
- Local: boto3 direct call (optional, skipped if unavailable)
- Graceful degradation: if no LLM available, consolidation proceeds unchanged
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import replace
from functools import cache

from pydantic import ValidationError

from src.agents.prompt_framing import SYSTEM_PROMPT_DATA_DIRECTIVE, frame_untrusted
from src.agents.referee.assignment_resolver import RoutingTrace
from src.agents.referee.aurora_choice import AURORA_ENGINES
from src.agents.referee.cache_overlay import can_own
from src.agents.referee.move_ledger import (
    MoveLedger,
    apply_to_assignment,
    best_other,
    group_key,
    ledger_for,
)
from src.agents.referee.query_scoring import ScoringContext, score_query_for_engine
from src.agents.referee.reality_check import (
    _current_for_eligibility,
    _group_eligible_engines,
    _is_signal_override,
    _query_eligible_engines,
)
from src.agents.referee.reality_check_request import moved_queries, moved_query_record
from src.agents.referee.reality_check_rules import _move_scores_text, tie_reason_code
from src.agents.referee.tie_break import break_tie, settled_counts
from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA
from src.contracts.model_flag import ModelFlag, parse_model_flags
from src.contracts.move_ledger import CandidateScore, MoveEvidence, MovePass, ReasonCode
from src.contracts.reality_check_model_response import (
    RealityCheckModelResponse,
    parse_reality_check_response,
)
from src.shared.engine_names import display_engine
from src.shared.models import (
    ROLE_SPECS,
    Role,
    build_bedrock_model,
    converse_request_fields,
    resolve_model_id,
)
from src.shared.skill_sections import skill_sections, skill_subsections

logger = logging.getLogger(__name__)

# The sanity sweep's own ``SweepDecision.blocking_rule`` values (#589), named
# here once so a caller that needs the full set
# (``src.tools.validation.customer_text_guard``'s snake_case-reason-code
# detector, #587 round 4) imports it instead of retyping the strings -- a new
# blocking rule added below is then picked up automatically, with no second
# edit anywhere else. ``BLOCKING_RULE_MIXED`` is reported in place of two or
# more DIFFERENT rules blocking the same engine (see the sweep below); it
# never comes from a single query/group's own gate.
BLOCKING_RULE_SIGNAL_OVERRIDE = "signal_override"
BLOCKING_RULE_NO_SERVICEABLE_ENGINE = "no_serviceable_engine"
BLOCKING_RULE_INELIGIBLE_DESTINATION = "ineligible_destination"
BLOCKING_RULE_MIXED = "mixed"
BLOCKING_RULES: tuple[str, ...] = (
    BLOCKING_RULE_SIGNAL_OVERRIDE,
    BLOCKING_RULE_NO_SERVICEABLE_ENGINE,
    BLOCKING_RULE_INELIGIBLE_DESTINATION,
    BLOCKING_RULE_MIXED,
)

# The review instruction and the engine capability reference live in one skill
# file that ``/reality-check`` (Claude Code) also reads, so a moved query gets
# the same instruction in both modes (#628). ``ENGINE_CONTEXT`` maps an engine
# id to its capability text from that file; every engine has an entry. Both
# are loaded on first use, so a deterministic run (``--llm-mode none``) never
# depends on the prompt file.
CONSOLIDATION_REVIEW_SKILL = "consolidation-review.md"


@cache
def engine_context() -> dict[str, str]:
    """``{engine: capability text}`` from the skill's Engine Capabilities section."""
    return dict(skill_subsections(CONSOLIDATION_REVIEW_SKILL, "Engine Capabilities"))


@cache
def validator_system_prompt() -> str:
    """The validator's system prompt: the skill's instruction, flag criteria and response."""
    review = skill_sections(CONSOLIDATION_REVIEW_SKILL)
    return (
        "You are a database migration expert reviewing engine consolidation decisions.\n\n"
        + review["Instruction"]
        + "\n\nFLAG WHEN:\n"
        + review["Flag When"]
        + "\n\nDO NOT FLAG:\n"
        + review["Do Not Flag"]
        + "\n\nRESPONSE:\n"
        + review["Response"]
        + "\n\nRespond with JSON only.\n\n"
        + SYSTEM_PROMPT_DATA_DIRECTIVE
    )


def __getattr__(name: str) -> object:
    """``ENGINE_CONTEXT`` and ``VALIDATOR_SYSTEM_PROMPT``, loaded on first access."""
    if name == "ENGINE_CONTEXT":
        return engine_context()
    if name == "VALIDATOR_SYSTEM_PROMPT":
        return validator_system_prompt()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Maximum queries to send in one validation call (token budget)
MAX_QUERIES_PER_CALL = 30

# Output budget for one validation call: room for every query of a batch to be
# flagged with a short reason, in compact JSON. Owned by the VALIDATOR role
# (src/shared/models.py), which raises it on families that think by default.
VALIDATOR_MAX_TOKENS = ROLE_SPECS[Role.VALIDATOR].max_tokens


class ValidationFailed(Exception):
    """An LLM validation call ran but gave no usable verdict (error or bad JSON)."""


def validate_consolidations(
    consolidations: list[dict],
    revised_assignments: list[dict],
    query_map: dict[str, dict],
    query_signals: dict[str, list[str]],
    original_assignments: list[dict] | None = None,
    incomplete: list[dict] | None = None,
    rejected: list[str] | None = None,
) -> list[ModelFlag]:
    """Validate consolidation decisions using an LLM.

    For each consolidation, asks the LLM whether the target engine can
    actually serve the moved queries. Returns the model's flags
    (:class:`~src.contracts.model_flag.ModelFlag`, the same contract the
    external response uses): queries it judges the target unable to serve.
    Where a flagged query goes is decided later, by the shared scorer
    (``src.agents.referee.model_flags``).

    Args:
        consolidations: List of consolidation dicts from run_reality_check()
        revised_assignments: The revised query assignments after consolidation
        query_map: {query_id: query_pattern_dict} from collector
        query_signals: {query_id: [signal_names]} from triage
        original_assignments: the assignments before consolidation; with them a
            moved query is found by its engine change (see ``moved_queries``)
        incomplete: when given, one entry is appended per batch that got no
            verdict (``from_engine``, ``to_engine``, ``batch`` as "start-end"
            1-based of ``total``, ``error``); its queries keep the move
        rejected: when given, one note is appended per response entry that
            fails the ``ModelFlag`` contract; that entry is dropped and the
            valid flags beside it are kept (the headless response's handling)

    Returns:
        One flag per flagged query, ``flagged_engine`` the consolidation's
        target, each one of the consolidation's moved queries. Empty list if
        all consolidations are valid or LLM is unavailable.
    """
    if not consolidations:
        return []

    corrections: list[ModelFlag] = []
    reviewed: dict[tuple[str, str], set[str]] = {}

    for consolidation in consolidations:
        from_engine = consolidation["from_engine"]
        to_engine = consolidation["to_engine"]
        seen = reviewed.setdefault((from_engine, to_engine), set())

        # The same records the external request lists for this consolidation
        # (#285); a query listed under an earlier record of the same move is not
        # sent twice.
        query_details = [
            moved_query_record(qa["query_id"], query_map, query_signals)
            for qa in moved_queries(consolidation, revised_assignments, original_assignments)
            if qa["query_id"] not in seen
        ]
        seen.update(q["query_id"] for q in query_details)
        total = len(query_details)

        # Every moved query is reviewed, MAX_QUERIES_PER_CALL per call
        for start in range(0, total, MAX_QUERIES_PER_CALL):
            batch = query_details[start : start + MAX_QUERIES_PER_CALL]
            batch_range = f"{start + 1}-{start + len(batch)}"
            try:
                raw_flags = _call_llm_validator(
                    from_engine=from_engine,
                    to_engine=to_engine,
                    queries=batch,
                    batch=(batch_range, total),
                )
            except ValidationFailed as exc:
                logger.warning(
                    "Consolidation %s -> %s: validation of queries %s of %d failed (%s); "
                    "those moves stand unreviewed",
                    from_engine,
                    to_engine,
                    batch_range,
                    total,
                    exc,
                )
                if incomplete is not None:
                    incomplete.append(
                        {
                            "from_engine": from_engine,
                            "to_engine": to_engine,
                            "batch": batch_range,
                            "total": total,
                            "error": str(exc)[:300],
                        }
                    )
                continue
            flagged, bad = parse_model_flags(raw_flags)
            if rejected is not None:
                rejected.extend(bad)
            batch_ids = {q["query_id"] for q in batch}
            for flag in flagged:
                if flag.flagged_engine != to_engine:
                    logger.warning(
                        "Consolidation %s -> %s: dropped a flag on %r, which is not "
                        "the engine under review",
                        from_engine,
                        to_engine,
                        flag.flagged_engine,
                    )
                    continue
                kept_ids = []
                for qid in flag.query_ids:
                    if qid not in batch_ids:
                        logger.warning(
                            "Consolidation %s -> %s: dropped a flag for %r, "
                            "which is not one of the queries under review",
                            from_engine,
                            to_engine,
                            str(qid)[:80],
                        )
                        continue
                    kept_ids.append(qid)
                # One flag stays one flag (as in the headless response), so a
                # flag on several queries is applied as one unit in both modes.
                if kept_ids:
                    corrections.append(flag.model_copy(update={"query_ids": kept_ids}))

    return corrections


# ---------------------------------------------------------------------------
# Sanity sweep — catch orphan engines after all consolidations and corrections
# ---------------------------------------------------------------------------

# Engines with this many or fewer queries (and no hard capability requirements)
# are candidates for the sanity sweep redirect.
ORPHAN_ENGINE_THRESHOLD = 5


def _format_gate(engine: str, gate_kind: str | None, required_capabilities: Iterable[str]) -> str:
    """A short, display-named reason an engine cannot serve a query (#579),
    from :class:`~src.agents.referee.query_scoring.EngineScore`'s
    structured ``gate_kind`` -- never its raw ``gate_reason`` text, which
    names the engine by its internal id, not its customer-facing display
    name. ``required_capabilities`` names the SPECIFIC missing capability
    (round 2 review suggestion) rather than a generic "a capability".
    """
    name = display_engine(engine)
    if gate_kind == "capability":
        caps = ", ".join(sorted(required_capabilities)) or "a capability this query needs"
        return f"{name} lacks {caps}"
    if gate_kind == "hard_exclusion":
        return f"{name} is hard-excluded for this query"
    if gate_kind == "model_flag":
        return f"{name} was flagged as unable to serve this query"
    return f"{name} cannot serve this query"


def _serviceable_scores(
    query: Mapping,
    candidates: Iterable[str],
    scoring_context: ScoringContext,
) -> tuple[dict[str, float], list[str]]:
    """``candidates`` that can serve ``query`` and their scores (the shared
    ADR-031 scorer, :func:`~src.agents.referee.query_scoring.
    score_query_for_engine`), plus a display-named reason for EVERY
    candidate that cannot (round 2 review: several different candidates
    can be excluded for different reasons, and the kept note names all of
    them, not just the first).
    """
    query_id = str(query.get("query_id") or "")
    required_caps = scoring_context.query_capabilities.get(query_id, [])
    scores: dict[str, float] = {}
    reasons: list[str] = []
    for candidate in candidates:
        result = score_query_for_engine(query, candidate, scoring_context)
        if result.score is None:
            reasons.append(_format_gate(candidate, result.gate_kind, required_caps))
            continue
        scores[candidate] = result.score
    return scores, reasons


def _unit_score(
    members: list[str],
    engine: str,
    query_map: Mapping[str, Mapping],
    scoring_context: ScoringContext,
) -> float | None:
    """The unit's score on ``engine`` (the mean over members), ``None`` if any is gated."""
    scores: list[float] = []
    for mqid in members:
        result = score_query_for_engine(
            query_map.get(mqid) or {"query_id": mqid}, engine, scoring_context
        )
        if result.score is None:
            return None
        scores.append(result.score)
    return sum(scores) / len(scores) if scores else None


def _ignoring_flags(scoring_context: ScoringContext) -> ScoringContext:
    """``scoring_context`` without its model-flag gaps: an engine's own score for a
    query the model flagged there (a moved query's before score, #647, #755)."""
    return replace(scoring_context, flag_gaps={})


def scorer_ladder(query: Mapping, scoring_context: ScoringContext) -> list[CandidateScore]:
    """``query``'s candidate ladder by the shared scorer, for a Move's evidence (#616).

    Every analyzed engine, sorted: its score, or the scorer's gate kind with no
    score; an engine that may not own the query keeps its score with
    ``gate="write_gate"``, as the first assignment's ladder does.
    """
    ladder: list[CandidateScore] = []
    for engine in sorted(scoring_context.analysis_outputs):
        result = score_query_for_engine(query, engine, scoring_context)
        if result.score is None:
            ladder.append(
                CandidateScore(engine=engine, score=None, gate=result.gate_kind or "gate")
            )
        elif not can_own(engine, query):
            ladder.append(CandidateScore(engine=engine, score=result.score, gate="write_gate"))
        else:
            ladder.append(CandidateScore(engine=engine, score=result.score))
    return ladder


def _group_serviceable_scores(
    member_qids: list[str],
    candidates: Iterable[str],
    query_map: Mapping[str, Mapping],
    scoring_context: ScoringContext,
) -> tuple[dict[str, float], list[str]]:
    """A co-dependency group's combined score on each candidate: the mean of
    every member's own score there (the maintainer's "a group's engine
    comes from the members' combined score") -- but only once EVERY member
    can actually be served by that candidate. One member gated there
    excludes the candidate for the WHOLE group, the same way one query with
    no serviceable committed engine blocks a whole orphan engine; it is
    never scored as a 0 that could let the group "win" an engine that
    structurally cannot serve one of its own members.
    """
    scores: dict[str, float] = {}
    reasons: list[str] = []
    for candidate in candidates:
        member_scores: list[float] = []
        blocked_reason: str | None = None
        for mqid in member_qids:
            query = query_map.get(mqid) or {"query_id": mqid}
            required_caps = scoring_context.query_capabilities.get(mqid, [])
            result = score_query_for_engine(query, candidate, scoring_context)
            if result.score is None:
                blocked_reason = _format_gate(candidate, result.gate_kind, required_caps)
                break
            member_scores.append(result.score)
        if blocked_reason is not None:
            reasons.append(blocked_reason)
            continue
        scores[candidate] = sum(member_scores) / len(member_scores)
    return scores, reasons


def _tied_candidate_pool(
    original_tied: list[str] | None, safe_destinations: list[str]
) -> list[str]:
    """ADR-031 Decision 12, the same restriction
    ``reality_check_place._place_settled_queries._candidate_pool`` applies: a
    query (or group) that already carries ``tied_engines`` is re-resolved
    among just the originally tied engines that are still safe
    destinations FIRST -- never opened up to the full safe-destination set
    while one of them survives. Only when NONE of them do is the full set
    used, the same "survivors or everything" fallback ``_candidate_pool``
    uses.
    """
    if not original_tied:
        return safe_destinations
    survivors = [e for e in original_tied if e in safe_destinations]
    return survivors or safe_destinations


def _signal_override_block(members: list[str], by_id: dict[str, dict]) -> str | None:
    """A display-named "nowhere else to go" reason when any of ``members``
    carries a mandatory signal override (round 2 review, Important 2): the
    rest of the reality check treats a signal-override query as mandatory
    for its engine (``mandatory_query_ids`` in Phase 1/2) -- this sweep must
    not undo that protection just because the engine it is mandatory for
    happens to also be tiny. ``None`` when no member is a signal override.
    """
    for mqid in members:
        qa = by_id[mqid]
        if _is_signal_override(qa):
            signal = qa.get("signal_override")
            signal_name = signal if isinstance(signal, str) and signal else "a signal override"
            return f"{mqid} is mandatory for the {signal_name} signal override"
    return None


def _eligible_destinations(
    members: list[str],
    engine: str,
    original_tied: list[str] | None,
    scores: dict[str, float],
    query_signals: Mapping[str, list[str]],
    query_map: Mapping[str, Mapping],
    query_capabilities: Mapping[str, list[str]],
) -> list[str]:
    """``scores``' own candidates (already proven serviceable by the shared
    scorer) filtered through the SAME eligibility rule Phase 2 uses
    (``reality_check_rules._query_eligible_engines``/``_group_eligible_engines``,
    #579 round 3): the write/ownership gate and specialist-absorb
    restriction (``may_absorb``, which calls ``can_own``), and the
    join-off-a-non-Aurora-engine-never-onto-DynamoDB block. Without this,
    the sweep could put a query (or group) somewhere Phase 2 itself would
    refuse to -- an INSERT onto OpenSearch, or a join onto DynamoDB.

    A synthetic trace, built only from ``scores`` -- never a second,
    independent model, since those scores are the shared scorer's own
    verdict -- satisfies these functions' own first gate ("the resolver's
    own score exists at all"), which is otherwise redundant with the
    serviceability check the caller already ran: the actual values do not
    matter, only that an engine already proven serviceable has SOME
    non-gated entry to read.
    """
    qid = members[0]
    is_group = len(members) > 1
    current_engine_by_qid: dict[str, str] = {m: engine for m in members}
    tied_engines_by_qid: dict[str, list[str]] = (
        {m: list(original_tied) for m in members} if original_tied else {}
    )
    trace_by_query: dict[str, dict] = {
        m: {"engines": dict(scores), **({"group": {"key": "sanity-sweep"}} if is_group else {})}
        for m in members
    }
    # Concrete-dict copies: ``_query_eligible_engines``/``_group_eligible_
    # engines`` are typed against ``dict`` (invariant, so a ``Mapping``
    # argument does not satisfy it statically even though every real
    # caller passes a plain dict already) -- never a behavior difference,
    # only what mypy needs to see.
    signals_dict: dict[str, list[str]] = dict(query_signals)
    query_map_dict: dict[str, dict] = {k: dict(v) for k, v in query_map.items()}
    capabilities_dict: dict[str, list[str]] = dict(query_capabilities)
    if is_group:
        return _group_eligible_engines(
            qid,
            trace_by_query,
            current_engine_by_qid,
            list(scores),
            set(),  # no mandatory member reaches here: _signal_override_block excludes one
            signals_dict,
            query_map_dict,
            capabilities_dict,
            tied_engines_by_qid,
        )
    return _query_eligible_engines(
        qid,
        query_map_dict.get(qid) or {"query_id": qid},
        _current_for_eligibility(qid, current_engine_by_qid, tied_engines_by_qid),
        list(scores),
        trace_by_query,
        signals_dict,
        query_map_dict,
        capabilities_dict,
    )


def sanity_sweep(
    revised_assignments: list[dict],
    consolidations: list[dict],
    scoring_context: ScoringContext,
    query_map: Mapping[str, Mapping],
    source_engine: str = "",
    co_dependency_groups: list[list[str]] | None = None,
    routing_trace: RoutingTrace | None = None,
    query_signals: Mapping[str, list[str]] | None = None,
    ledger: MoveLedger | None = None,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Final pass: remove a tiny orphan engine the other committed engines
    can serve (ADR-031 Decision 7: "when an engine serves only a few
    queries, check whether the other engines can serve them, and if so
    remove the engine; never move other queries onto it").

    After all consolidations and LLM corrections, some engines may survive
    with very few queries (e.g., 3 trivial queries that got bounced back).
    For each engine at or under :data:`ORPHAN_ENGINE_THRESHOLD`, every one
    of its queries (or co-dependency group, moved as one unit -- see below)
    is scored against every OTHER engine that has already earned its place
    -- Aurora (always eligible: it is the consolidation target ADR-031
    Decision 7 has in mind), or any engine with MORE than the orphan
    threshold -- using the exact same shared scorer the rest of the
    pipeline uses (:func:`~src.agents.referee.query_scoring.
    score_query_for_engine`) -- never a raw query count (#579), and never a
    second, independent fit model (ADR-031 Decision 7). A second tiny
    engine is never a candidate destination either: two mutually
    sub-threshold engines could otherwise absorb each other depending only
    on which happened to be evaluated first, trading the old raw-count bias
    for an iteration-order one.

    Four things keep a query (or its whole group) from moving at all, and
    each one keeps the WHOLE orphan engine, not just that one query (round
    2/3 review): a mandatory signal override (nowhere else to go, the same
    way the rest of the reality check treats it); no committed engine that
    can serve it (a hard exclusion or missing capability); for a
    co-dependency group, any one member failing either of those on a given
    candidate, which excludes that candidate for every member; or no
    candidate passing the SAME eligibility rule Phase 2 applies to every
    move (``reality_check_rules._query_eligible_engines``/``_group_eligible_
    engines``, round 3) -- the write/ownership gate and specialist-absorb
    restriction (``may_absorb``), and the join-off-a-non-Aurora-engine-
    never-onto-DynamoDB block. A query the shared scorer calls serviceable
    can still fail this: serviceability is "can this engine run the query
    at all," eligibility is "would Phase 2 ever actually place it there."
    Otherwise the engine is removed and each query/group moves to its own
    best-scoring, eligible, serviceable committed engine. A query (or group) that
    already carries ``tied_engines`` is re-resolved among just the
    originally tied engines that are still safe destinations first (ADR-031
    Decision 12); otherwise every safe destination competes. An exact tie
    is broken by the shared ``tie_break.break_tie`` -- there is no
    "current engine" stability candidate here (the orphan engine being
    removed is never itself a safe destination, so passing it as one would
    never do anything) -- and an unresolved tie records the surviving tied
    set on ``tied_engines`` and, when ``routing_trace`` is given, calls its
    ``record_tie`` the same way the resolver's own tie-break does. A
    resolved tie or a plain move always OVERWRITES ``tied_engines`` from
    the fresh verdict, so a query never keeps a stale tie naming the engine
    this sweep just removed.

    This prevents the "DocumentDB for 3 SHOW FIELDS commands" problem.

    Args:
        revised_assignments: current query assignments (post-corrections)
        consolidations: current consolidation records
        scoring_context: the shared ADR-031 scorer's context
            (:meth:`~src.agents.referee.query_scoring.ScoringContext.
            from_pipeline`), built from the same triage, analysis and
            collector artifacts the rest of the reality check uses.
        query_map: ``{query_id: query_pattern_dict}`` from the collector, so
            each query can be scored against every candidate engine.
        source_engine: the source database engine (e.g. ``"mysql"``), used
            only to break an exact tie in the source-compatible engine's
            favor (ADR-031 Decision 2).
        co_dependency_groups: ``Assignment.co_dependency_groups`` (query
            structure, not current placement, so it stays valid across
            every consolidation this run has already made) -- a group with
            2+ members still on the SAME orphan engine moves as one unit,
            never split between two different destinations. The resolver
            re-run's own ``RoutingTrace.group_by_query`` (what Phase 2
            reads) and this list come from the same ``build_co_dependency_
            groups(queries, tables)`` call on the same collector output, so
            they agree -- except a group with no ownable candidate at all,
            which the resolver sends through the Aurora fallback instead
            and so never appears in its own trace; that group sits on
            Aurora and never reaches this sweep either way.
        routing_trace: an optional, live
            :class:`~src.agents.referee.routing_trace.RoutingTrace`
            to record an unresolved tie onto, when a caller has one; the
            production handler does not build one for this phase, so this
            is ``None`` there and recording is simply skipped.
        query_signals: ``{query_id: [triage_signal_names]}``
            (:func:`~src.agents.referee.reality_check_rules._build_query_signals`),
            fed to the eligibility check's specialist-absorb rule
            (``may_absorb``) -- missing or empty means no engine is
            treated as having a matching specialty, the more conservative
            direction (fewer, not more, destinations judged eligible).
        ledger: the move ledger (#616) each moved query is recorded on
            (``move_ledger.apply_to_assignment``, pass ``sweep``, rule
            ``sweep.orphan``), with the query's score on the orphan engine
            (ignoring any model flag) and on its destination, both from the
            shared scorer and both in its reason (#755). A fresh one from
            ``revised_assignments`` when ``None``.

    Returns:
        (updated_assignments, updated_consolidations, sweep_decisions) —
        the first two may be unchanged. ``sweep_decisions`` (#589) has one
        entry per orphan engine this sweep evaluated: ``engine``,
        ``decision`` ("kept" or "removed"), ``reason``, ``blocking_query_ids``
        (the queries/group members with nowhere else to go, when kept; empty
        when removed) and ``blocking_rule`` (why they had nowhere to go, when
        kept; ``None`` when removed) -- the same explanation that otherwise
        only ever reached a ``[reality-check]`` log line.
    """
    if ledger is None:
        ledger = ledger_for(revised_assignments)
    engine_counts = Counter(qa["assigned_engine"] for qa in revised_assignments)
    source_compatible_engine = SOURCE_ENGINE_TO_AURORA.get((source_engine or "").lower())
    workload = settled_counts(revised_assignments)
    by_id = {qa["query_id"]: qa for qa in revised_assignments}
    # The engines a tiny orphan may actually move to: Aurora (the
    # consolidation target ADR-031 Decision 7 has in mind), or any engine
    # that has already earned its place by carrying more than the orphan
    # threshold -- fixed ONCE, up front, from every engine's count before
    # this sweep moves anything. Two mutually tiny engines can never absorb
    # each other this way: whichever happened to be evaluated first would
    # otherwise "win" purely by iteration order, which is exactly the kind
    # of unprincipled bias #579 removed on the count axis -- reintroducing
    # it on an order axis instead would not be a fix.
    safe_destinations = sorted(
        e for e, c in engine_counts.items() if e in AURORA_ENGINES or c > ORPHAN_ENGINE_THRESHOLD
    )
    # query_id -> the 2+-member group it belongs to (query structure, #579
    # round 2 Important 3) -- only groups of 2+ matter here; a lone query
    # whose other co-dependency members already sit elsewhere is placed as
    # an individual, the same way it would be if it had no group at all.
    group_by_qid: dict[str, list[str]] = {}
    for defined_group in co_dependency_groups or []:
        if len(defined_group) < 2:
            continue
        for qid in defined_group:
            group_by_qid[qid] = defined_group

    # #589: the sweep's own kept/removed verdict for every orphan engine it
    # evaluates below -- otherwise this explanation only ever reaches the
    # ``print()`` lines further down, never ``reality-check/output.json``.
    sweep_decisions: list[dict] = []

    for engine, count in list(engine_counts.items()):
        if engine in AURORA_ENGINES:
            continue
        if count > ORPHAN_ENGINE_THRESHOLD:
            continue

        engine_qids = [
            qa["query_id"] for qa in revised_assignments if qa["assigned_engine"] == engine
        ]

        committed_elsewhere = [e for e in safe_destinations if e != engine]
        if not committed_elsewhere:
            sweep_decisions.append(
                {
                    "engine": engine,
                    "decision": "kept",
                    "reason": "no other committed engine exists to serve these queries",
                    "blocking_query_ids": sorted(engine_qids),
                    "blocking_rule": BLOCKING_RULE_NO_SERVICEABLE_ENGINE,
                }
            )
            continue  # nothing else exists to serve these queries

        engine_qid_set = set(engine_qids)

        # Partition into co-dependency-group units (2+ members still on
        # THIS engine move together) and individual units -- never split a
        # group that is on the orphan between two different destinations.
        seen: set[str] = set()
        units: list[list[str]] = []
        for qid in engine_qids:
            if qid in seen:
                continue
            co_group = group_by_qid.get(qid)
            members_here = sorted(m for m in co_group if m in engine_qid_set) if co_group else []
            if len(members_here) >= 2:
                units.append(members_here)
                seen.update(members_here)
            else:
                units.append([qid])
                seen.add(qid)

        blocked_notes: list[str] = []
        # #589: the same information as blocked_notes, structured so a kept
        # engine's sweep_decision can name exactly which queries/groups
        # blocked it and under which rule, not just the printed sentence.
        blocked_units: list[dict] = []
        resolved: list[tuple[list[str], str, str | None, list[str], dict[str, float]]] = []
        for members in units:
            is_group = len(members) > 1

            override_reason = _signal_override_block(members, by_id)
            if override_reason is not None:
                blocked_notes.append(override_reason)
                blocked_units.append({"members": members, "rule": BLOCKING_RULE_SIGNAL_OVERRIDE})
                continue

            original_tied = next(
                (by_id[m].get("tied_engines") for m in members if by_id[m].get("tied_engines")),
                None,
            )
            pool = _tied_candidate_pool(original_tied, committed_elsewhere)

            if is_group:
                scores, reasons = _group_serviceable_scores(
                    members, pool, query_map, scoring_context
                )
                label = f"co-dependency group {', '.join(members)}"
            else:
                qid = members[0]
                query = query_map.get(qid) or {"query_id": qid}
                scores, reasons = _serviceable_scores(query, pool, scoring_context)
                label = qid

            if not scores:
                blocked_notes.append(f"{label} ({'; '.join(reasons)})")
                blocked_units.append(
                    {"members": members, "rule": BLOCKING_RULE_NO_SERVICEABLE_ENGINE}
                )
                continue

            eligible = _eligible_destinations(
                members,
                engine,
                original_tied,
                scores,
                query_signals or {},
                query_map,
                scoring_context.query_capabilities,
            )
            if not eligible:
                # Serviceable (the shared scorer's gates passed) but still
                # not somewhere Phase 2 would ever place this query/group
                # (#579 round 3): the write/specialist-absorb rule or the
                # join-off-non-Aurora-onto-DynamoDB block. Named so, never
                # folded into the generic serviceability reason above.
                blocked_notes.append(
                    f"{label} (no destination eligible under the reality check's own "
                    "placement rule -- the write/specialist-absorb gate or the "
                    "join-onto-DynamoDB block)"
                )
                blocked_units.append(
                    {"members": members, "rule": BLOCKING_RULE_INELIGIBLE_DESTINATION}
                )
                continue
            scores = {e: s for e, s in scores.items() if e in eligible}

            top = max(scores.values())
            tied = sorted(e for e, s in scores.items() if s == top)
            if len(tied) > 1:
                # No "current engine" stability candidate: the orphan being
                # removed is never itself a safe destination, so it can
                # never be one of ``tied`` for this to matter (round 2
                # review: the old ``{engine}`` stability set was meaningless
                # for exactly this reason).
                seed = ",".join(members)
                winner, tie_kind = break_tie(
                    tied, set(), source_compatible_engine, settled_counts=workload, query_id=seed
                )
            else:
                (winner,) = tied
                tie_kind = None
            resolved.append((members, winner, tie_kind, tied, scores))

        if blocked_notes:
            # At least one query (or co-dependency group) has nowhere else
            # to go (#579): the whole engine stays, and nothing is moved
            # onto it either -- never decided by raw count, and never by
            # generalizing one blocked query's gate into protecting (or
            # redirecting) every other query here too.
            print(
                f"[reality-check] Sanity sweep: kept {display_engine(engine)} "
                f"({count} queries) -- not every query has a serviceable committed "
                f"engine: {'; '.join(blocked_notes)}"
            )
            # #589: the same verdict, structured -- blocking_query_ids/_rule
            # name EVERY blocked unit, not just the first, the same way
            # blocked_notes already does in the printed sentence above. This
            # is never an engine pick (ADR-031/#571's bias scan is about
            # choosing BETWEEN candidate engines) -- distinct_rules names
            # which gate categories blocked this one engine, built by a
            # plain append loop (never ``sorted``/``min``/``max`` or set
            # order) so "one rule" vs. "several, so 'mixed'" never depends on
            # iteration order either.
            distinct_rules: list[str] = []
            for u in blocked_units:
                if u["rule"] not in distinct_rules:
                    distinct_rules.append(u["rule"])
            sweep_decisions.append(
                {
                    "engine": engine,
                    "decision": "kept",
                    "reason": (
                        "not every query has a serviceable committed engine: "
                        f"{'; '.join(blocked_notes)}"
                    ),
                    "blocking_query_ids": sorted({m for u in blocked_units for m in u["members"]}),
                    "blocking_rule": (
                        distinct_rules[0] if len(distinct_rules) == 1 else BLOCKING_RULE_MIXED
                    ),
                }
            )
            continue

        # Every query (and every co-dependency group, as one unit) has
        # somewhere to go: remove the engine, each unit to its own
        # best-scoring serviceable committed engine.
        moved_counts: Counter[str] = Counter()
        for members, winner, tie_kind, tied, unit_scores in resolved:
            is_group = len(members) > 1
            new_tied = sorted(tied) if tie_kind == "tie_unresolved" else []
            basis = (
                "co-dependency group moves together"
                if is_group
                else "best-scoring serviceable committed engine"
            )
            # ADR-031 Decision 7 (#755): the before and after scores, from the
            # shared scorer -- the unit's own score on the orphan engine
            # (ignoring a model flag there) and the score it won with.
            # Rounded to 2 places (a group's mean can run long): the same number
            # in the Move and in the reason.
            raw_before = _unit_score(members, engine, query_map, _ignoring_flags(scoring_context))
            score_before = None if raw_before is None else round(raw_before, 2)
            score_after = round(unit_scores[winner], 2)
            scores_text = _move_scores_text(engine, score_before, winner, score_after)
            tie_rule = (
                tie_reason_code(tie_kind, tied, workload, ",".join(members))
                if tie_kind is not None
                else None
            )
            unit_group_key = group_key(group_by_qid[members[0]]) if is_group else None
            for mqid in members:
                qa = by_id[mqid]
                # Always the FRESH verdict (round 2 review Important 1b):
                # never left reading a stale tie that names the engine this
                # sweep just removed.
                apply_to_assignment(
                    ledger,
                    qa,
                    winner,
                    pass_=MovePass.SWEEP,
                    reason_code=ReasonCode.SWEEP_ORPHAN,
                    score_before=score_before,
                    score_after=score_after,
                    runner_up=best_other(unit_scores, winner),
                    evidence=MoveEvidence(
                        candidates=scorer_ladder(
                            query_map.get(mqid) or {"query_id": mqid}, scoring_context
                        ),
                        group_scores=dict(unit_scores) if is_group else {},
                        tie_rule=tie_rule,
                    ),
                    group_key=unit_group_key,
                    tied_engines=new_tied,
                )
                qa["assignment_reason"] = (
                    f"sanity sweep: redirected from {display_engine(engine)} to "
                    f"{display_engine(winner)} (orphan engine with {count} queries; {basis}; "
                    f"{scores_text})"
                )
                moved_counts[winner] += 1
                if tie_kind == "tie_unresolved" and routing_trace is not None:
                    routing_trace.record_tie(mqid, tie_kind)

        # Name order only, for deterministic output -- every destination
        # was already decided above, per query, by score and tie-break;
        # this never picks a "winner" among them, so it is not the same
        # shape as a count- or name-keyed engine pick.
        destinations = sorted(moved_counts)
        single_destination = len(destinations) == 1
        for to_engine in destinations:
            moved = moved_counts[to_engine]
            consolidations.append(
                {
                    "from_engine": engine,
                    "to_engine": to_engine,
                    "query_count": moved,
                    "reason": (
                        f"Sanity sweep: {display_engine(engine)} had only {count} queries, "
                        "each with a serviceable committed engine; "
                        f"{moved} scored best on {display_engine(to_engine)}"
                    ),
                    # Split proportionally across every destination rather
                    # than crediting one record with the whole estimate by
                    # some other pick (e.g. its query share) -- which would
                    # be exactly the count-keyed bias #579 removed, one
                    # field over.
                    "saved_cost_estimate": round(500.0 * moved / count, 2),
                    "action": "full" if single_destination else "partial",
                    "queries_retained": [],
                    "retention_reason": None,
                }
            )
            print(
                f"[reality-check] Sanity sweep: {display_engine(engine)} ({count} queries) "
                f"→ {moved} to {display_engine(to_engine)} (best-scoring committed engine)"
            )

        # #589: one "removed" verdict per engine, not per destination --
        # the per-destination split is already on the Consolidation records
        # above; this names the engine-level outcome the same way a "kept"
        # verdict does.
        dest_text = "; ".join(f"{moved_counts[d]} to {display_engine(d)}" for d in destinations)
        sweep_decisions.append(
            {
                "engine": engine,
                "decision": "removed",
                "reason": (
                    f"{count} queries, each with a serviceable committed engine: {dest_text}"
                ),
                "blocking_query_ids": [],
                "blocking_rule": None,
            }
        )

    return revised_assignments, consolidations, sweep_decisions


def _call_llm_validator(
    from_engine: str,
    to_engine: str,
    queries: list[dict],
    batch: tuple[str, int] | None = None,
) -> list:
    """Call the LLM to validate whether target engine can serve the queries.

    Returns the model's raw flag entries (validated by the caller). Raises
    :class:`ValidationFailed` when no LLM gave a usable verdict (none available,
    a call error, or a response that is not the JSON array asked for).
    """
    errors: list[str] = []
    # Try Strands (production), fall back to boto3 (local)
    for attempt in (_try_strands_validator, _try_boto3_validator):
        try:
            result = attempt(from_engine, to_engine, queries, batch)
        except ValidationFailed as exc:
            errors.append(str(exc))
            continue
        if result is not None:
            return result

    raise ValidationFailed("; ".join(errors) or "no LLM available")


def _build_validation_prompt(
    from_engine: str,
    to_engine: str,
    queries: list[dict],
    batch: tuple[str, int] | None = None,
) -> str:
    """Build the validation prompt for the LLM.

    The query records (SQL, table names) are customer data: they are framed as
    untrusted data, never instructions. ``batch`` is ``("start-end", total)``
    when the consolidation's moved queries are reviewed in several calls.
    """
    target_context = engine_context().get(to_engine, f"{display_engine(to_engine)} database")

    queries_block = ""
    for q in queries:
        signals_str = f" [signals: {', '.join(q['signals'])}]" if q["signals"] else ""
        queries_block += (
            f"- {q['query_id']} ({q['type']}, {q['cps']:.1f} cps, "
            f"tables: {', '.join(q['tables'])}){signals_str}\n"
            f"  SQL: {q['sql']}\n\n"
        )

    if batch is not None and batch[1] > len(queries):
        scope = f"queries {batch[0]} of the {batch[1]} moved"
    else:
        scope = f"{len(queries)} queries"
    return (
        f"You are validating a database migration consolidation decision.\n\n"
        f"DECISION: Move {scope} from {from_engine} to {to_engine}.\n\n"
        f"TARGET ENGINE CAPABILITIES:\n{target_context}\n\n"
        f"QUERIES BEING MOVED:\n" + frame_untrusted(queries_block, label="moved queries") + "\n\n"
        f"TASK: For each query above, decide whether {to_engine} can serve it, "
        f"following the instruction and the flag criteria you were given.\n\n"
        f"Respond with ONLY a compact JSON object (no indentation) in the response "
        f'format you were given, with "flagged_engine" set to "{to_engine}" and no '
        f'"executive_summary" (the summary is a separate call). '
        f'If all queries are fine, respond with {{"consolidation_corrections": []}}.\n\n'
        f"JSON response:"
    )


def _parse_llm_response(response_text: str) -> list[ModelFlag]:
    """Parse the LLM's JSON response into valid flags (invalid entries dropped).

    Returns [] for a response that is not the ``RealityCheckModelResponse``
    shape; see :func:`_parse_flagged`.
    """
    return parse_model_flags(_parse_flagged(response_text) or [])[0]


def _parse_flagged(response_text: str) -> list[object] | None:
    """The raw flag entries in a response, or None when it is not the contract's shape.

    The response must be a :class:`RealityCheckModelResponse` -- the same
    contract the headless ``llm_responses/reality_check.json`` is validated
    against (``parse_reality_check_response``, #628). Entries are not validated
    here: :func:`validate_consolidations` validates each one against
    ``ModelFlag`` (``parse_model_flags``), so one bad entry is dropped and
    counted while the valid flags beside it are kept -- the same handling the
    headless response gets.
    """
    text = response_text.strip()

    # Strip markdown code fences if present
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [line for line in lines if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        # The raw LLM response text is DEBUG only -- it is model output, which
        # must stay out of INFO and above (#143). WARNING keeps only the length.
        logger.debug("Failed to parse LLM validation response: %s", text[:200])
        logger.warning("Failed to parse LLM validation response (length=%d)", len(text))
        return None
    try:
        response = parse_reality_check_response(result)
    except ValidationError:
        logger.debug("LLM validation response is off the contract: %s", text[:200])
        logger.warning(
            "LLM validation response does not match RealityCheckModelResponse (type=%s)",
            type(result).__name__,
        )
        return None
    return list(response.consolidation_corrections)


def _report(from_engine: str, to_engine: str, queries: list[dict], flagged: list) -> None:
    if flagged:
        # Raw entries, not yet validated: the console narration stays to the
        # entry count (the explanation is model text, DEBUG only, #143).
        print(
            f"[reality-check] LLM returned {len(flagged)} flag(s) on {len(queries)} queries "
            f"moved to {to_engine}"
        )
        logger.debug("[reality-check] flags on %s: %s", to_engine, flagged)
    else:
        print(f"[reality-check] LLM confirmed all {len(queries)} queries OK on {to_engine}")


def _validator_model_id() -> str:
    return resolve_model_id(Role.VALIDATOR)


def _error_label(exc: Exception) -> str:
    """A short, safe label for an LLM-call exception.

    Normally the exception's type name. A boto3/Strands ``ClientError`` wraps
    every AWS service error behind that one class, so the type name alone
    ("ClientError") loses the distinction between e.g. throttling and a bad
    request; its own ``Error.Code`` (a short AWS-defined enum string such as
    "ThrottlingException", not free text) is safe to log above DEBUG (#143)
    and far more useful. Falls back to the type name when ``exc`` is not a
    ``ClientError`` or carries no recognizable code.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = response.get("Error", {}).get("Code")
        if code:
            return f"{type(exc).__name__}:{code}"
    return type(exc).__name__


def _try_strands_validator(
    from_engine: str,
    to_engine: str,
    queries: list[dict],
    batch: tuple[str, int] | None = None,
) -> list | None:
    """Validate using a Strands agent (production); None when Strands is not installed.

    Asks for structured output against :class:`RealityCheckModelResponse`, the
    contract the headless response uses too (each flag entry advertises the
    ``ModelFlag`` schema; the caller validates entries one by one).

    Raises :class:`ValidationFailed` when the call fails or the response is unusable.
    """
    try:
        from strands import Agent
    except ImportError:
        return None

    prompt = _build_validation_prompt(from_engine, to_engine, queries, batch)

    try:
        model = build_bedrock_model(Role.VALIDATOR, model_id=_validator_model_id())
        agent = Agent(
            model=model,
            system_prompt=validator_system_prompt(),
            tools=[],
            structured_output_model=RealityCheckModelResponse,
            callback_handler=None,
        )

        print(
            f"[reality-check] Validating {len(queries)} queries "
            f"moved {from_engine} → {to_engine}..."
        )
        result = agent(prompt)
        structured = getattr(result, "structured_output", None)
        response_text = (
            "" if isinstance(structured, RealityCheckModelResponse) else str(result).strip()
        )
    except Exception as exc:
        # The underlying error from an LLM call can carry prompt/response
        # fragments (#143): full text at DEBUG only. The ValidationFailed
        # message itself must also carry only the type, since it propagates
        # into a WARNING log a few frames up (run_consolidation_validation).
        logger.debug("Strands validation failed: %s", exc)
        logger.error("Strands validation failed (%s)", _error_label(exc))
        raise ValidationFailed(f"strands: {_error_label(exc)}") from exc

    flagged: list | None = (
        list(structured.consolidation_corrections)
        if isinstance(structured, RealityCheckModelResponse)
        else None
    )
    if flagged is None:
        flagged = _parse_flagged(response_text)
    if flagged is None:
        raise ValidationFailed("strands: response does not match the flag contract")
    _report(from_engine, to_engine, queries, flagged)
    return flagged


def _try_boto3_validator(
    from_engine: str,
    to_engine: str,
    queries: list[dict],
    batch: tuple[str, int] | None = None,
) -> list | None:
    """Validate using boto3 Bedrock directly (local development); None without boto3.

    Raises :class:`ValidationFailed` when the call fails or the response is unusable.
    """
    try:
        import boto3
    except ImportError:
        return None

    prompt = _build_validation_prompt(from_engine, to_engine, queries, batch)

    try:
        client = boto3.client("bedrock-runtime")
        response = client.converse(
            **converse_request_fields(Role.VALIDATOR, model_id=_validator_model_id()),
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            system=[{"text": validator_system_prompt()}],
        )
        # Newer models may lead with a reasoning block; take the first text block.
        response_text = next(
            block["text"] for block in response["output"]["message"]["content"] if "text" in block
        )
    except Exception as exc:
        # Same reasoning as the Strands branch above: full text at DEBUG only.
        logger.debug("boto3 validation failed: %s", exc)
        logger.error("boto3 validation failed (%s)", _error_label(exc))
        raise ValidationFailed(f"boto3: {_error_label(exc)}") from exc

    print(f"[reality-check] Validating {len(queries)} queries moved {from_engine} → {to_engine}...")
    flagged = _parse_flagged(response_text)
    if flagged is None:
        raise ValidationFailed("boto3: response does not match the flag contract")
    _report(from_engine, to_engine, queries, flagged)
    return flagged
