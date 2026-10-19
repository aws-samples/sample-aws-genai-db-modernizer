"""Pending engine choices, one per table, and ``--auto``'s "decide for me" (#1003).

Maintainer decision (2026-10-17, ADR-031 Decision 12 Amendment 10):

1. The open ties (``QueryAssignment.tied_engines``: an exact tie no routing rule
   decided, Decision 12) are grouped per table. The customer gate, the UI picker
   and every deliverable present one choice per table, its queries listed under
   it, and a pick applies to all of them through the customer gate. When the
   queries write the table, the pick also chooses its source of truth
   (Amendment 9).
2. Interactive runs never break such a tie automatically. Each choice is shown
   with the deciding facts: each engine's score, why they tie, and the trade-off
   of each engine.
3. ``--auto`` means "decide for me" and leaves no choice pending. Per table,
   :func:`src.agents.referee.tie_break.decide_for_me` picks the engine: the higher
   unrounded score, then the engine the plan already uses more, then
   ``break_tie``. Each pick is a Move (pass ``auto_pick``, rule
   ``tie.decided_for_you``, actor ``rule``) with the alternative and both
   scores, and the version records the choice (``Assignment.auto_decided_choices``)
   so the deliverables mark it "decided automatically, review if needed" and the
   UI can switch it.

A choice is built from one table's tied queries, so the grouping is the
query's own table: the table it writes (its write group), else the one of its
tables that the most tied queries touch (ties to the first by name). Queries that
must move as one unit (``write_groups.placement_groups``: co-dependency groups
and write groups) are always in the same choice, so a pick never splits a unit.
Queries tied on different engine sets share a choice only when they are one
unit; the choice then ties between the engines they all tie on (or, with fewer
than two in common, every engine any of them ties on).

A choice's scores are the shared scorer's (``score_query_for_engine``), averaged
over its queries with the shared group rule (``co_dependency.group_engine_scores``,
a write group's call-weighted mean): ``score`` is what routing compared,
``unrounded_score`` the same before the scorer drops its fractions
(``exact=True``). Nothing here decides a route on its own: a pick is applied by
``assignment_overrides.apply_assignment_overrides``, which records the Moves.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from src.agents.referee.cache_overlay import can_own
from src.agents.referee.co_dependency import build_co_dependency_groups, group_engine_scores
from src.agents.referee.tie_break import (
    DECIDED_BY_SETTLED_WORKLOAD,
    DECIDED_BY_UNROUNDED_SCORE,
    TIE_KIND_SOURCE_COMPATIBLE,
    TIE_KIND_STABILITY,
    decide_for_me,
    settled_counts,
)
from src.agents.referee.write_groups import (
    collector_write_groups,
    placement_groups,
    unit_call_weights,
)
from src.contracts.assignment_models import AutoDecidedChoice, ChoiceEngine, PendingEngineChoice
from src.shared.engine_names import display_engine

if TYPE_CHECKING:
    from src.agents.referee.query_scoring import ScoringContext

__all__ = [
    "AutoPick",
    "EngineChoiceError",
    "EngineNotTied",
    "UnknownChoice",
    "auto_decisions_still_holding",
    "build_pending_choices",
    "choice_overrides",
]

# Unrounded scores are shown, compared and stored to this many places.
UNROUNDED_PLACES = 2


class EngineChoiceError(ValueError):
    """A per-table pick the gate cannot apply (the message is customer-facing)."""


class UnknownChoice(EngineChoiceError):
    def __init__(self, choice_id: str, known: Iterable[str]) -> None:
        names = ", ".join(sorted(known)) or "none"
        super().__init__(f"No pending engine choice named {choice_id!r}. Pending choices: {names}.")
        self.choice_id = choice_id


class EngineNotTied(EngineChoiceError):
    def __init__(self, choice: PendingEngineChoice, engine: str) -> None:
        options = " or ".join(display_engine(e) for e in choice.tied_engines)
        super().__init__(
            f"{display_engine(engine)} is not one of the tied engines for {choice.table}: "
            f"choose {options}."
        )
        self.choice_id = choice.choice_id
        self.engine = engine


@dataclass(frozen=True)
class AutoPick:
    """What an ``--auto`` pick records on each Move and on the version (#1003)."""

    choice_id: str
    table: str
    engine: str
    alternatives: list[str]
    scores: dict[str, float]
    unrounded_scores: dict[str, float]
    rule: str
    reason: str
    query_ids: list[str] = field(default_factory=list)

    def record(self, version: int) -> AutoDecidedChoice:
        return AutoDecidedChoice(
            choice_id=self.choice_id,
            table=self.table,
            query_ids=list(self.query_ids),
            engine=self.engine,
            alternatives=list(self.alternatives),
            unrounded_scores=dict(self.unrounded_scores),
            scores=dict(self.scores),
            rule=self.rule,
            reason=self.reason,
            decided_in_version=version,
        )


def _field(qa: Any, name: str, default: Any = None) -> Any:
    if isinstance(qa, Mapping):
        return qa.get(name, default)
    return getattr(qa, name, default)


def _bare(table: str) -> str:
    return str(table).rsplit(".", 1)[-1]


def _names(engines: Iterable[str]) -> str:
    names = [display_engine(e) for e in engines]
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


def _fmt(score: float | None) -> str:
    return "n/a" if score is None else f"{score:.{UNROUNDED_PLACES}f}"


def _whole(score: float | None) -> str:
    return "n/a" if score is None else f"{score:g}"


class _Union:
    def __init__(self, items: Iterable[str]) -> None:
        self.parent = {i: i for i in items}

    def find(self, x: str) -> str:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def join(self, a: str, b: str) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def build_pending_choices(
    query_assignments: Iterable[Any],
    collector_output: Mapping | None,
    ctx: ScoringContext | None,
    source_compatible_engine: str | None = None,
) -> list[PendingEngineChoice]:
    """The open ties of ``query_assignments`` grouped per table (#1003), sorted by id.

    ``query_assignments`` are ``QueryAssignment`` objects or their dicts. ``ctx``
    is the pipeline's ``ScoringContext`` (``from_pipeline``); without one the
    choices carry no scores and ``--auto`` falls straight to its second rule.
    """
    qas = list(query_assignments)
    tied = {
        str(_field(qa, "query_id")): qa
        for qa in qas
        if len(_field(qa, "tied_engines") or []) >= 2 and _field(qa, "query_id")
    }
    if not tied:
        return []
    collector_output = collector_output or {}
    queries = (collector_output.get("queries") or {}).get("query_patterns") or []
    query_by_id = {str(q.get("query_id")): q for q in queries if q.get("query_id")}
    write_groups = collector_write_groups(collector_output) if queries else {}
    units = (
        placement_groups(
            build_co_dependency_groups(
                queries, (collector_output.get("database_schema") or {}).get("tables") or []
            ),
            write_groups,
        )
        if queries
        else []
    )
    weights = unit_call_weights(units, write_groups, query_by_id)
    written_by: dict[str, list[str]] = {}
    for table, members in sorted(write_groups.items()):
        for qid in members:
            written_by.setdefault(qid, []).append(table)

    tie_of = {qid: tuple(sorted(_field(qa, "tied_engines"))) for qid, qa in tied.items()}
    tables_of = {
        qid: sorted({str(t) for t in (_field(qa, "source_tables") or []) if t})
        for qid, qa in tied.items()
    }
    touches = Counter(t for qid in tied for t in tables_of[qid])

    def home(qid: str) -> str:
        written = [t for t in written_by.get(qid, []) if t in tables_of[qid]] or written_by.get(
            qid, []
        )
        if written:
            return written[0]
        tables = tables_of[qid]
        if not tables:
            return ""
        return sorted(tables, key=lambda t: (-touches[t], t))[0]

    home_of = {qid: home(qid) for qid in tied}
    union = _Union(tied)
    first_of: dict[tuple[str, tuple[str, ...]], str] = {}
    for qid in sorted(tied):
        key = (home_of[qid] or f"query:{qid}", tie_of[qid])
        if key in first_of:
            union.join(first_of[key], qid)
        else:
            first_of[key] = qid
    # A placement unit is always one choice, whatever its members tie on: a pick
    # moves the whole unit, so two choices over one unit could disagree.
    for unit in units:
        members = sorted(q for q in unit if q in tied)
        for qid in members[1:]:
            union.join(members[0], qid)

    components: dict[str, list[str]] = {}
    for qid in sorted(tied):
        components.setdefault(union.find(qid), []).append(qid)

    settled = settled_counts(qas)
    raw: list[tuple[str, list[str]]] = []
    for members in components.values():
        homes = Counter(home_of[q] for q in members if home_of[q])
        table = sorted(homes, key=lambda t: (-homes[t], t))[0] if homes else f"query:{members[0]}"
        raw.append((table, members))
    raw.sort(key=lambda item: (item[0], item[1][0]))

    choices: list[PendingEngineChoice] = []
    seen: Counter[str] = Counter()
    for table, members in raw:
        seen[table] += 1
        choice_id = table if seen[table] == 1 else f"{table}#{seen[table]}"
        choices.append(
            _choice(
                choice_id,
                table,
                members,
                tied,
                tables_of,
                written_by,
                _choice_engines([tie_of[q] for q in members]),
                query_by_id,
                weights,
                ctx,
                settled,
                source_compatible_engine,
            )
        )
    return choices


def _choice(
    choice_id: str,
    table: str,
    members: list[str],
    tied: Mapping[str, Any],
    tables_of: Mapping[str, list[str]],
    written_by: Mapping[str, list[str]],
    engines: tuple[str, ...],
    query_by_id: Mapping[str, Mapping],
    weights: Mapping[str, float],
    ctx: ScoringContext | None,
    settled: Mapping[str, int],
    source_compatible_engine: str | None,
) -> PendingEngineChoice:
    whole = _choice_scores(members, engines, query_by_id, weights, ctx, exact=False)
    exact = _choice_scores(members, engines, query_by_id, weights, ctx, exact=True)
    unrounded = {e: round(v, UNROUNDED_PLACES) for e, v in exact.items()}
    on = Counter(str(_field(tied[q], "assigned_engine")) for q in members)
    current = set(on)
    pick, rule, kind = decide_for_me(
        list(engines), unrounded, current, source_compatible_engine, settled, choice_id
    )
    writes = sorted(q for q in members if written_by.get(q))
    choice_engines = [
        ChoiceEngine(
            engine=e,
            score=whole.get(e),
            unrounded_score=unrounded.get(e),
            settled_query_count=int(settled.get(e, 0)),
            trade_off=_trade_off(e, engines, unrounded, settled, table, bool(writes)),
        )
        for e in engines
    ]
    # Shown only: where most of the choice's queries sit now (each one's own
    # placeholder; they can differ when a choice groups independent queries).
    placeholder = sorted(on, key=lambda e: (-on[e], e))[0]
    return PendingEngineChoice(
        choice_id=choice_id,
        table=table,
        tables=sorted({t for q in members for t in tables_of.get(q, [])}),
        query_ids=sorted(members),
        write_query_ids=writes,
        tied_engines=list(engines),
        current_engine=placeholder,
        current_engines=sorted(current),
        options=choice_engines,
        why_tied=_why_tied(engines, whole, unrounded),
        decide_for_me_engine=pick,
        decide_for_me_rule=rule,
        decide_for_me_reason=_pick_reason(pick, rule, kind, engines, unrounded, settled),
    )


def _choice_engines(ties: list[tuple[str, ...]]) -> tuple[str, ...]:
    """The engines a choice ties between: those every query ties on (a unit's tie,
    as ``tie_break.group_tie`` takes it), or, when its queries share fewer than two
    (a unit whose members tie on different engines), every engine any of them ties
    on, so the unit is still one choice."""
    common = set.intersection(*(set(t) for t in ties))
    if len(common) >= 2:
        return tuple(sorted(common))
    return tuple(sorted(set().union(*ties)))


def _choice_scores(
    members: list[str],
    engines: Iterable[str],
    query_by_id: Mapping[str, Mapping],
    weights: Mapping[str, float],
    ctx: ScoringContext | None,
    *,
    exact: bool,
) -> dict[str, float]:
    """Each engine's mean score over ``members`` (the shared group rule)."""
    if ctx is None:
        return {}
    from src.agents.referee.query_scoring import score_query_for_engine

    def member_score(qid: str, engine: str) -> float | None:
        query = query_by_id.get(qid) or {"query_id": qid}
        if not can_own(engine, query, ctx.source_engine):
            return None
        return score_query_for_engine(query, engine, ctx, exact=exact).score

    unit_weights = {q: weights[q] for q in members if q in weights}
    return group_engine_scores(members, list(engines), member_score, unit_weights)


def _why_tied(
    engines: tuple[str, ...], whole: Mapping[str, float], unrounded: Mapping[str, float]
) -> str:
    first = whole.get(engines[0]) if engines else None
    score = first if all(whole.get(e) == first for e in engines) else None
    if score is not None:
        sentence = (
            f"{_names(engines)} each score {_whole(round(score, UNROUNDED_PLACES))} at the "
            "whole-point resolution routing compares, so no routing rule picks one."
        )
    else:
        sentence = f"{_names(engines)} tie at the resolution routing compares."
    values = {unrounded.get(e) for e in engines}
    if None in values or not unrounded:
        return sentence
    if len(values) == 1:
        return sentence + f" They are level before rounding too ({_fmt(unrounded[engines[0]])})."
    ranked = sorted(engines, key=lambda e: (-unrounded[e], e))
    parts = ", ".join(f"{display_engine(e)} {_fmt(unrounded[e])}" for e in ranked)
    return sentence + f" Before rounding: {parts}."


def _trade_off(
    engine: str,
    engines: tuple[str, ...],
    unrounded: Mapping[str, float],
    settled: Mapping[str, int],
    table: str,
    writes: bool,
) -> str:
    name = display_engine(engine)
    n = int(settled.get(engine, 0))
    if n:
        noun = "query" if n == 1 else "queries"
        out = (
            f"{name} already serves {n} settled {noun} in this plan, so choosing it adds no engine."
        )
    else:
        out = (
            f"{name} serves no settled query in this plan: choosing it keeps it in the "
            "plan for these queries."
        )
    others = [e for e in engines if e != engine]
    if engine in unrounded and others and all(e in unrounded for e in others):
        best_other = max(unrounded[e] for e in others)
        gap = round(unrounded[engine] - best_other, UNROUNDED_PLACES)
        if gap > 0:
            out += f" It scores {_fmt(gap)} points higher before rounding."
        elif gap < 0:
            out += f" It scores {_fmt(-gap)} points lower before rounding."
    if writes:
        out += f" It becomes the source of truth for the writes of {_bare(table)}."
    return out


def _pick_reason(
    engine: str,
    rule: str,
    kind: str | None,
    engines: tuple[str, ...],
    unrounded: Mapping[str, float],
    settled: Mapping[str, int],
) -> str:
    name = display_engine(engine)
    others = [e for e in engines if e != engine]
    if rule == DECIDED_BY_UNROUNDED_SCORE:
        against = ", ".join(f"{_fmt(unrounded.get(e))} for {display_engine(e)}" for e in others)
        return f"{name} scores higher before rounding ({_fmt(unrounded.get(engine))} against {against})."
    if rule == DECIDED_BY_SETTLED_WORKLOAD:
        against = ", ".join(f"{settled.get(e, 0)} for {display_engine(e)}" for e in others)
        return (
            f"Level before rounding; {name} already serves more of the plan "
            f"({settled.get(engine, 0)} settled queries against {against}), so the plan "
            "keeps fewer engines."
        )
    if kind == TIE_KIND_SOURCE_COMPATIBLE:
        return f"Level on score and workload; {name} is compatible with the source database."
    if kind == TIE_KIND_STABILITY:
        return f"Level on score and workload; {name} is where these queries already sit."
    return (
        f"Level on score and workload; no rule prefers either engine, so a stable hash of "
        f"the table name picked {name}."
    )


def choice_overrides(
    choices: Iterable[PendingEngineChoice],
    picks: Mapping[str, str] | None = None,
    *,
    decide: bool = False,
) -> tuple[dict[str, str], dict[str, AutoPick], dict[str, str]]:
    """``(query_id -> engine, query_id -> AutoPick, choice_id -> picked engine)``
    for the gate (#1003).

    ``picks`` maps a choice id (or its bare table name) to the customer's engine;
    with ``decide`` every other choice takes its ``--auto`` engine, recorded as an
    :class:`AutoPick` for each of its queries. Raises :class:`UnknownChoice` or
    :class:`EngineNotTied` for a pick the gate cannot apply; nothing is applied then.
    """
    by_id = {c.choice_id: c for c in choices}
    by_bare: dict[str, list[str]] = {}
    for c in by_id.values():
        by_bare.setdefault(_bare(c.choice_id), []).append(c.choice_id)
    engine_of: dict[str, str] = {}
    auto: dict[str, AutoPick] = {}
    chosen: dict[str, str] = {}
    for name, engine in (picks or {}).items():
        choice_id = name if name in by_id else None
        if choice_id is None and len(by_bare.get(_bare(name), [])) == 1:
            choice_id = by_bare[_bare(name)][0]
        if choice_id is None:
            raise UnknownChoice(name, by_id)
        choice = by_id[choice_id]
        if engine not in choice.tied_engines:
            raise EngineNotTied(choice, engine)
        chosen[choice_id] = engine
        for qid in choice.query_ids:
            engine_of[qid] = engine
    if decide:
        for choice_id, choice in sorted(by_id.items()):
            if choice_id in chosen:
                continue
            pick = AutoPick(
                choice_id=choice_id,
                table=choice.table,
                engine=choice.decide_for_me_engine,
                alternatives=[e for e in choice.tied_engines if e != choice.decide_for_me_engine],
                scores={e.engine: e.score for e in choice.options if e.score is not None},
                unrounded_scores={
                    e.engine: e.unrounded_score
                    for e in choice.options
                    if e.unrounded_score is not None
                },
                rule=choice.decide_for_me_rule,
                reason=choice.decide_for_me_reason,
                query_ids=list(choice.query_ids),
            )
            for qid in choice.query_ids:
                engine_of[qid] = pick.engine
                auto[qid] = pick
    return engine_of, auto, chosen


def auto_decisions_still_holding(
    decisions: Iterable[AutoDecidedChoice], query_assignments: Iterable[Any]
) -> list[AutoDecidedChoice]:
    """The ``--auto`` decisions whose every query still sits on the decided engine
    with no tie (a later pick or reroute that moved one of them undoes it)."""
    state = {
        str(_field(qa, "query_id")): (
            str(_field(qa, "assigned_engine")),
            bool(_field(qa, "tied_engines")),
        )
        for qa in query_assignments
    }
    return [d for d in decisions if all(state.get(q) == (d.engine, False) for q in d.query_ids)]
