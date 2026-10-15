"""Plain-English reasons for the move ledger (#339, #616).

The one renderer of a :class:`~src.contracts.move_ledger.Move`'s reason: the
query decision trace (graph ``query_trace``, the trace API, and later the UI
and the engineering report) reads its sentences from here, never from a local
copy. The brief for #489 calls for exactly one: customer prose is rendered
from ``reason_code`` + evidence, so two deliverables cannot word the same
move differently.

The sentences use display names (``display_engine``), scores and fixed
phrases only. They never contain a reason code, a pass id or an engine id,
and they never interpolate free text from an artifact (query text, a model's
words, a customer note), so nothing untrusted reaches them. They are plain
text: a renderer that puts them in HTML or Markdown still escapes them
(``src/report/escaping.py``), like every other string.
"""

from __future__ import annotations

from collections.abc import Iterable

from src.agents.referee.query_scoring import (
    GATE_KIND_CAPABILITY,
    GATE_KIND_HARD_EXCLUSION,
    GATE_KIND_MODEL_FLAG,
)
from src.contracts.move_ledger import Move, MoveActor, ReasonCode
from src.shared.engine_names import display_engine

__all__ = [
    "GATE_TEXT_KINDS",
    "GROUP_OWNER_GATE",
    "WRITE_GATE",
    "format_score",
    "gate_text",
    "render_move_reason",
    "render_outscored",
    "render_runner_up",
    "render_trace_summary",
    "rule_label",
]

# The ladder's gate for an engine that may not own the query (a write on a
# cache or a search index): the score is kept, the engine cannot take it.
WRITE_GATE = "write_gate"

# The trace's gate for an engine that cannot own every query of a
# co-dependency group (it is missing from the group's mean scores).
GROUP_OWNER_GATE = "group_owner"

# Why an engine could not take the query (``CandidateScore.gate``): every gate
# kind the shared scorer emits (``query_scoring.GATE_KINDS``) plus the ladder's
# write gate and the trace's group-owner gate. ``tests/unit/shared/test_move_reasons.py`` checks it is complete,
# so a new gate kind cannot reach a customer as generic wording.
_GATE_TEXT: dict[str, str] = {
    GATE_KIND_HARD_EXCLUSION: "ruled out for this kind of query",
    GATE_KIND_CAPABILITY: "missing a capability the query needs",
    GATE_KIND_MODEL_FLAG: "a design review found it cannot support this query",
    WRITE_GATE: "cannot own a query that writes data (not a system of record)",
    GROUP_OWNER_GATE: "cannot own every query in its co-dependency group",
}

GATE_TEXT_KINDS: frozenset[str] = frozenset(_GATE_TEXT)

# A short label for each rule, for a badge or a runner-up note.
_RULE_LABEL: dict[ReasonCode, str] = {
    ReasonCode.HIGHEST_SCORE: "highest score",
    ReasonCode.TIE_SOURCE_COMPATIBLE: "tie settled by source compatibility",
    ReasonCode.TIE_STABILITY: "tie settled by staying put",
    ReasonCode.TIE_PLACEHOLDER_SETTLED_WORKLOAD: "unresolved tie",
    ReasonCode.TIE_PLACEHOLDER_HASH: "unresolved tie",
    ReasonCode.TIE_CLEARED: "tie settled",
    ReasonCode.GROUP_MEAN: "co-dependency group",
    ReasonCode.OVERRIDE_TEXT_SEARCH: "full-text search",
    ReasonCode.OVERRIDE_UTILITY: "administration statement",
    ReasonCode.FALLBACK_NO_SCORE: "no engine could score it",
    ReasonCode.FALLBACK_NO_OWNER: "no engine could own its group",
    ReasonCode.CONSOLIDATE_NO_UNIQUE_VALUE: "engine consolidated away",
    ReasonCode.CONSOLIDATE_SOURCE_COMPATIBLE_SURVIVES: "source-compatible engine kept",
    ReasonCode.FLOOR_OPENSEARCH: "too little search work for OpenSearch",
    ReasonCode.FLOOR_COST_SHARE: "too small a share of the workload for its cost",
    ReasonCode.SWEEP_ORPHAN: "engine left with too few queries",
    ReasonCode.FLAG_CAPABILITY_GAP: "flagged by a design review",
    ReasonCode.CUSTOMER_PICK: "customer choice",
    ReasonCode.GROUP_CUSTOMER_PROPAGATION: "moved with its co-dependency group",
    ReasonCode.CACHE_LEGACY_OWNER: "a cache cannot own the query",
    ReasonCode.ROLLBACK_REDESIGN_FAILED: "redesign failed, routing restored",
}

# How an exact tie was decided (``evidence.tie_rule`` or a tie Move's code).
_TIE_DECIDED: dict[ReasonCode, str] = {
    ReasonCode.TIE_SOURCE_COMPATIBLE: "the tie went to the engine compatible with the source database",
    ReasonCode.TIE_STABILITY: "the tie kept the query on the engine it was already on",
    ReasonCode.TIE_PLACEHOLDER_SETTLED_WORKLOAD: (
        "the engine already serving more of the settled workload holds it until the "
        "customer chooses"
    ),
    ReasonCode.TIE_PLACEHOLDER_HASH: (
        "a fixed, repeatable order picked an engine to hold it until the customer chooses"
    ),
}


def format_score(score: float | None) -> str:
    """A score as the trace shows it: ``82``, ``82.5``, or ``n/a`` when there is none."""
    if score is None:
        return "n/a"
    rounded = round(float(score), 1)
    return f"{rounded:.0f}" if rounded == int(rounded) else f"{rounded:.1f}"


def gate_text(gate: str | None) -> str | None:
    """Why a gated engine could not take the query, in plain words (``None`` when not gated)."""
    if not gate:
        return None
    return _GATE_TEXT.get(gate, "not eligible for this query")


def rule_label(code: ReasonCode | str) -> str:
    """A short plain-English label for the rule that decided a Move."""
    try:
        return _RULE_LABEL[ReasonCode(code)]
    except ValueError:
        return "routing rule"


def _names(engines: Iterable[str]) -> str:
    """``A``, ``A and B``, ``A, B and C`` from engine ids, in display names."""
    names = [display_engine(e) for e in engines]
    if not names:
        return ""
    if len(names) == 1:
        return names[0]
    return f"{', '.join(names[:-1])} and {names[-1]}"


def _scored(engine: str | None, score: float | None) -> str:
    """``DynamoDB (69)``, or just ``DynamoDB`` when there is no score."""
    if engine is None:
        return "no engine"
    name = display_engine(engine)
    return name if score is None else f"{name} ({format_score(score)})"


def _tie_partners(move: Move) -> list[str]:
    """The engines the query tied with, other than where it landed."""
    tied = [e for e in move.tied_engines if e != move.to_engine]
    if tied:
        return tied
    # A decided tie keeps no tied_engines: its partners are the candidates
    # that scored exactly what the winner scored.
    partners = [
        c.engine
        for c in move.evidence.candidates
        if c.engine != move.to_engine
        and not c.gate
        and c.score is not None
        and move.score_after is not None
        and c.score == move.score_after
    ]
    if partners:
        return sorted(partners)
    if move.runner_up and move.runner_up.score == move.score_after:
        return [move.runner_up.engine]
    return []


def _first_assignment(move: Move) -> str:
    """The sentence for a query's first engine (``from_engine`` is ``None``)."""
    to = display_engine(move.to_engine)
    code = move.reason_code
    if code == ReasonCode.HIGHEST_SCORE:
        ahead = (
            f", ahead of {_scored(move.runner_up.engine, move.runner_up.score)}"
            if (move.runner_up)
            else ""
        )
        return f"{to} scored highest for this query ({format_score(move.score_after)}){ahead}."
    if code in _TIE_DECIDED:
        partners = _tie_partners(move)
        with_ = f" with {_names(partners)}" if partners else ""
        return f"{to} tied{with_} at {format_score(move.score_after)}; {_TIE_DECIDED[code]}."
    if code == ReasonCode.GROUP_MEAN:
        group_score = move.evidence.group_scores.get(move.to_engine, move.score_after)
        return (
            f"Placed on {to} with its co-dependency group: {to} has the best mean score "
            f"across the group's queries ({format_score(group_score)})."
        )
    if code == ReasonCode.OVERRIDE_TEXT_SEARCH:
        return f"Routed to {to} because the query needs full-text search."
    if code == ReasonCode.OVERRIDE_UTILITY:
        return (
            f"Kept on {to}: administration and metadata statements stay on the "
            "source-compatible relational engine."
        )
    if code == ReasonCode.FALLBACK_NO_SCORE:
        return f"No engine could score this query, so it falls back to {to}."
    if code == ReasonCode.FALLBACK_NO_OWNER:
        return (
            f"No engine can own every query in its co-dependency group, so it falls back to {to}."
        )
    if code == ReasonCode.CUSTOMER_PICK:
        return f"Assigned to {to} at the customer's request."
    return f"Assigned to {to} ({rule_label(code)})."


def _engine_change(move: Move) -> str:
    """The sentence for a Move from one engine to another."""
    to = display_engine(move.to_engine)
    frm = display_engine(move.from_engine or "")
    scores = (
        f" ({format_score(move.score_before)} to {format_score(move.score_after)})"
        if (move.score_before is not None or move.score_after is not None)
        else ""
    )
    lead = f"Moved from {frm} to {to}{scores}"
    code = move.reason_code
    why: str
    if code == ReasonCode.HIGHEST_SCORE:
        why = f"{to} scores it higher among the engines kept"
    elif code == ReasonCode.CONSOLIDATE_NO_UNIQUE_VALUE:
        why = (
            f"the reality check removed {frm}, which adds too little over the engines kept "
            "to justify running another engine"
        )
    elif code == ReasonCode.CONSOLIDATE_SOURCE_COMPATIBLE_SURVIVES:
        why = (
            "the source-compatible engine serves this query at least as well, so "
            f"{frm} was removed"
        )
    elif code == ReasonCode.FLOOR_OPENSEARCH:
        why = "there is too little search work to justify a separate OpenSearch domain"
    elif code == ReasonCode.FLOOR_COST_SHARE:
        why = f"{frm} would serve too small a share of the workload to justify its cost"
    elif code == ReasonCode.SWEEP_ORPHAN:
        why = f"after consolidation, {frm} was left with too few queries to keep"
    elif code == ReasonCode.FLAG_CAPABILITY_GAP:
        why = f"a design review found that {frm} cannot support this query"
    elif code == ReasonCode.CUSTOMER_PICK:
        why = "the customer chose it"
    elif code == ReasonCode.GROUP_CUSTOMER_PROPAGATION:
        why = "it moves with a co-dependent query the customer re-routed"
    elif code == ReasonCode.CACHE_LEGACY_OWNER:
        why = (
            f"a cache cannot be the system of record, so {to} owns the query and the cache "
            "can sit in front of it"
        )
        if move.actor == MoveActor.CUSTOMER:
            why += " (the customer asked to cache it)"
    elif code == ReasonCode.ROLLBACK_REDESIGN_FAILED:
        lead = f"Moved back from {frm} to {to}"
        why = f"the redesign on {frm} failed, so the earlier routing is restored"
    elif code == ReasonCode.GROUP_MEAN:
        group_score = move.evidence.group_scores.get(move.to_engine, move.score_after)
        why = (
            f"its co-dependency group moves together, and {to} has the best mean score "
            f"across the group ({format_score(group_score)})"
        )
    elif code == ReasonCode.OVERRIDE_UTILITY:
        why = (
            "administration and metadata statements stay on the source-compatible "
            "relational engine"
        )
    elif code == ReasonCode.OVERRIDE_TEXT_SEARCH:
        why = "the query needs full-text search"
    elif code in _TIE_DECIDED:
        why = _TIE_DECIDED[code]
    else:
        why = rule_label(code)
    sentence = f"{lead}: {why}."
    if move.evidence.tie_rule is not None and move.evidence.tie_rule in _TIE_DECIDED:
        sentence += f" {to} tied for the best score there; {_TIE_DECIDED[move.evidence.tie_rule]}."
    return sentence


def _same_engine(move: Move) -> str:
    """The sentence for a Move that keeps the engine (a tie change or a re-statement)."""
    to = display_engine(move.to_engine)
    code = move.reason_code
    if code == ReasonCode.TIE_CLEARED:
        previous = [e for e in move.evidence.previous_tied_engines if e != move.to_engine]
        with_ = f" with {_names(previous)}" if previous else ""
        return f"The tie{with_} is settled: the query stays on {to}."
    if code in _TIE_DECIDED:
        partners = _tie_partners(move)
        with_ = f" with {_names(partners)}" if partners else ""
        return f"{to} tied{with_} at {format_score(move.score_after)}; {_TIE_DECIDED[code]}."
    if code == ReasonCode.OVERRIDE_UTILITY:
        return (
            f"Pinned to {to}: administration and metadata statements stay on the "
            "source-compatible relational engine."
        )
    if code == ReasonCode.CUSTOMER_PICK:
        return f"The customer confirmed {to} for this query."
    if code == ReasonCode.GROUP_MEAN:
        return f"Kept on {to} with its co-dependency group."
    return f"Stays on {to} ({rule_label(code)})."


def render_move_reason(move: Move) -> str:
    """One short plain-English sentence for why ``move`` landed where it did.

    The only renderer of a Move's reason (#339): it reads ``reason_code``,
    the engines, the scores, the runner-up and the evidence (tie rule, the
    earlier tie, group scores), and names engines with ``display_engine``.
    A Move that leaves the query tied adds the tie and its placeholder.
    """
    if move.from_engine is None:
        sentence = _first_assignment(move)
    elif move.from_engine == move.to_engine:
        sentence = _same_engine(move)
    else:
        sentence = _engine_change(move)
    if move.tied_engines and move.reason_code not in _TIE_DECIDED:
        partners = [e for e in move.tied_engines if e != move.to_engine]
        if partners:
            sentence += (
                f" It is tied with {_names(partners)}; {display_engine(move.to_engine)} "
                "holds it until the customer chooses."
            )
    return sentence


def _tie_settled(rule: ReasonCode | None) -> str:
    """How a tie with the runner-up was settled, as a clause (empty when unknown)."""
    if rule is None:
        return ""
    if rule in _TIE_DECIDED:
        return f"; {_TIE_DECIDED[rule]}"
    if rule == ReasonCode.TIE_CLEARED:
        return "; a later routing step settled the tie"
    return f"; the tie was settled ({rule_label(rule)})"


def render_runner_up(
    final_engine: str,
    final_score: float | None,
    engine: str,
    score: float | None,
    gate: str | None = None,
    tie_rule: ReasonCode | None = None,
    group: bool = False,
) -> str:
    """Why the runner-up ``engine`` (an engine of the final plan) did not get the query.

    ``final_score`` and ``score`` are on the scale the deciding pass compared:
    the query's own scores, or its co-dependency group's mean scores
    (``group``). ``gate`` is the runner-up's gate when every other engine of
    the plan is gated; ``tie_rule`` the rule that settled a tie with it.
    """
    name = display_engine(engine)
    final = display_engine(final_engine)
    if gate:
        return f"{name} could not take the query: {gate_text(gate)}."
    if score is None:
        return f"{name} was not scored for this query."
    basis = " (group mean)" if group else ""
    if final_score is None:
        return f"{name} scored {format_score(score)}{basis}; {final} has no score for this query."
    gap = round(final_score - score, 1)
    if gap <= 0:
        return f"{name} tied with {final} at {format_score(score)}{basis}{_tie_settled(tie_rule)}."
    return (
        f"{name} was next at {format_score(score)}{basis}, {format_score(gap)} "
        f"point{'' if gap == 1 else 's'} behind {final} ({format_score(final_score)})."
    )


def render_outscored(
    final_engine: str,
    final_score: float | None,
    engine: str,
    score: float | None,
    step: Move,
    step_version: int,
    left_it: bool,
    in_plan: bool,
    group: bool = False,
) -> str:
    """Why ``engine``, which scored higher than the final engine, does not serve the query.

    ``step`` is the Move that decided it: the one that took the query off
    ``engine`` (``left_it``), else the one that put the query on its final
    engine (a customer pick, an override, a flag).
    """
    name = display_engine(engine)
    basis = " group mean" if group else ""
    higher = (
        f"{name} scored higher ({format_score(score)} against "
        f"{format_score(final_score)}{basis})"
    )
    where = f"version {step_version}"
    if left_it:
        decided = f"the query left it in {where} ({rule_label(step.reason_code)})"
    else:
        decided = f"{where} decided otherwise ({rule_label(step.reason_code)})"
    tail = "" if in_plan else ", and it is not part of the final plan"
    return f"{higher}, but {decided}{tail}."


def render_trace_summary(
    final_engine: str,
    final_score: float | None,
    step_count: int,
    runner_up_sentence: str | None,
    last_move: Move | None = None,
) -> str:
    """One line for a query's whole trace: where it ended, after how many steps, and the runner-up.

    After a post-schema rollback the final engine still carries the design
    review's flag (no score): the line says the reroute was tried and undone.
    """
    steps = f"after {step_count} routing step{'' if step_count == 1 else 's'}"
    if (
        last_move is not None
        and last_move.reason_code == ReasonCode.ROLLBACK_REDESIGN_FAILED
        and last_move.from_engine
    ):
        final = display_engine(final_engine)
        return (
            f"Ends on {final} {steps}: the reroute to {display_engine(last_move.from_engine)} "
            f"was rolled back because its redesign failed, and the design review's flag on "
            f"{final} still applies."
        )
    head = f"Ends on {_scored(final_engine, final_score)} {steps}."
    return f"{head} {runner_up_sentence}" if runner_up_sentence else head
