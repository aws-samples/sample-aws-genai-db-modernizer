"""Re-place a waived count when its table's writes move (#1151).

ADR-031 Decision 2 Amendment 12 (#377) lets a key-scoped ``COUNT`` pass an engine's
capability gate (DynamoDB's ``aggregation``) only while the table it counts has its
source of truth on that engine (``gates.toml`` ``[[waivers]]``). The first assignment
and the reality check place a waived count once the writes are placed. A later pass
can move the table's writes: a customer pick that pulls them along
(``group.write_customer_propagation``), the source-of-truth reroute of a saved split
(``group.write_reroute``), or a post-schema reroute of a flagged write. After that the
waiver no longer holds, and the count would sit on an engine its gate rules out.

:func:`replace_lapsed_counts` is the rule every such pass runs after its own moves:
for each table whose source of truth the pass changed, every in-scope count on it that
sits on an engine only the waiver let serve it is re-placed with the shared scorer
(``score_query_for_engine``, the pass's version's sources of truth), among the engines
the version keeps that may own it. An exact tie goes through ``tie_break.break_tie``
with ``settled_counts``. The engine change is a ledger Move on the pass's own pass,
rule ``gate.key_scoped_count`` when it lands on the table's new source of truth, else
``highest_score`` (or the tie's rule). A count with nothing to re-place is left alone,
so a pass that moves no table's writes changes nothing.

A customer's pin is never moved by a rule (ADR-028). Only the customer gate re-places
a pinned count, when the customer's own newer edit moved its table's writes (``pinned
= "move"``: the Move is the customer's, and the pin is cleared with a warning). The two
automatic reroutes hold a pinned count instead (``pinned = "hold"``): they keep its
table's writes on its engine as they keep a pinned write's, and a count they still
strand is left pinned and gated, for the feasibility review to block on
(:func:`stranded_counts`, ``FindingKind.STRANDED_COUNT``).
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from src.agents.referee.cache_overlay import CACHE_OVERLAY_ENGINES, can_own
from src.agents.referee.consolidation_validator import scorer_ladder
from src.agents.referee.move_ledger import MoveLedger, apply_to_assignment, best_other
from src.agents.referee.query_scoring import ScoringContext, score_query_for_engine, waiver_table
from src.agents.referee.reality_check_rules import tie_reason_code
from src.agents.referee.tie_break import TIE_KIND_UNRESOLVED, break_tie, settled_counts
from src.agents.referee.write_groups import sources_of_truth
from src.contracts.move_ledger import MoveActor, MoveEvidence, MovePass, ReasonCode
from src.shared.engine_names import display_engine

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CountMove:
    """One waived count a pass re-placed."""

    query_id: str
    table: str
    from_engine: str
    to_engine: str
    # The table's source of truth after the pass (None: its writes are on two engines).
    source_of_truth: str | None
    score_after: float | None
    reason: str
    # The count carried a customer pin, which the move cleared.
    unpinned: bool


@dataclass(frozen=True)
class CountReplacement:
    """What :func:`replace_lapsed_counts` did: the counts it moved, the pinned ones it
    held (``pinned = "hold"``), and those no kept engine can serve (left as placed)."""

    moved: list[CountMove]
    held: list[str]
    unserved: list[str]

    @property
    def warnings(self) -> list[str]:
        """The version's warnings for the counts it could not, or would not, re-place."""
        out = [unpinned_warning(m) for m in self.moved if m.unpinned]
        out += [
            f"Query {qid} is a key-scoped count pinned to an engine that serves it only "
            "while its table's writes are there, and they moved; the pin is kept, so "
            "choose the count's engine or keep the table's writes on it in the "
            "assignment review."
            for qid in self.held
        ]
        out += [
            f"Query {qid} is a key-scoped count whose table's writes moved off the "
            "engine it is on, and no engine this version keeps can serve it; choose its "
            "engine in the assignment review."
            for qid in self.unserved
        ]
        return out


def _get(qa: Any, key: str) -> Any:
    return qa.get(key) if isinstance(qa, Mapping) else getattr(qa, key, None)


def _set(qa: Any, key: str, value: Any) -> None:
    if isinstance(qa, dict):
        qa[key] = value
    else:
        setattr(qa, key, value)


def changed_sources(before: Mapping[str, str], after: Mapping[str, str]) -> set[str]:
    """The tables whose source of truth differs between ``before`` and ``after``."""
    return {t for t in set(before) | set(after) if before.get(t) != after.get(t)}


def relies_on_waiver(query: Mapping, engine: str, ctx: ScoringContext) -> str | None:
    """The table ``query`` counts when ``engine`` serves it only through a capability
    waiver (it does when that table's writes are on ``engine``), else None. Reads no
    triage: the waiver exists only for an engine that lacks the capability."""
    table = waiver_table(query)
    if table is None:
        return None
    if not ctx.with_sources_of_truth({table: engine}).capability_waivers(query, engine):
        return None
    return table


def stranded_counts(
    query_assignments: Iterable[Any],
    query_by_id: Mapping[str, Mapping],
    ctx: ScoringContext,
    write_groups: Mapping[str, Iterable[str]],
) -> list[tuple[str, str, str, str | None]]:
    """``(query_id, table, engine, source_of_truth)`` for each in-scope count of a
    version that sits on an engine only the waiver lets serve it while its table's
    source of truth is elsewhere (or split): a pinned count a reroute held."""
    qas = list(query_assignments)
    sources = version_sources(qas, write_groups)
    out = []
    for qa in sorted(qas, key=lambda q: str(_get(q, "query_id"))):
        qid = str(_get(qa, "query_id"))
        engine = str(_get(qa, "assigned_engine") or "")
        query = query_by_id.get(qid)
        if _get(qa, "in_scope") is False or query is None or not engine:
            continue
        table = relies_on_waiver(query, engine, ctx)
        if table is not None and sources.get(table) != engine:
            out.append((qid, table, engine, sources.get(table)))
    return out


def pinned_counts_by_table(
    query_assignments: Iterable[Any],
    query_by_id: Mapping[str, Mapping],
    ctx: ScoringContext,
) -> dict[str, list[str]]:
    """``{table: [query id]}`` of the customer-pinned counts that sit on their engine
    through the waiver: each holds its table's writes there, as a pinned write does."""
    out: dict[str, list[str]] = {}
    for qa in query_assignments:
        qid = str(_get(qa, "query_id"))
        query = query_by_id.get(qid)
        if not _get(qa, "customer_override") or query is None or _get(qa, "in_scope") is False:
            continue
        table = relies_on_waiver(query, str(_get(qa, "assigned_engine") or ""), ctx)
        if table is not None:
            out.setdefault(table, []).append(qid)
    return {t: sorted(q) for t, q in sorted(out.items())}


def lapsed(
    query: Mapping, engine: str, ctx: ScoringContext, after: Mapping[str, str]
) -> str | None:
    """The table whose waiver ``query`` on ``engine`` relied on and no longer has under
    ``after`` (``{table: engine}``), or None: the count only passes ``engine``'s gate
    while its table's writes are there, and ``after`` puts them elsewhere."""
    table = relies_on_waiver(query, engine, ctx)
    if table is None or after.get(table) == engine:
        return None
    after_ctx = ctx.with_sources_of_truth(after)
    if score_query_for_engine(query, engine, after_ctx).score is not None:
        return None
    return table


def version_sources(query_assignments: Iterable[Any], write_groups: Mapping) -> dict[str, str]:
    """``{table: engine}`` of a version's in-scope query assignments (dicts or models)."""
    engine_of = {
        str(_get(qa, "query_id")): str(_get(qa, "assigned_engine") or "")
        for qa in query_assignments
        if _get(qa, "in_scope") is not False
    }
    return sources_of_truth(write_groups, engine_of)


def replace_lapsed_counts(
    query_assignments: Iterable[Any],
    ledger: MoveLedger,
    *,
    query_by_id: Mapping[str, Mapping],
    ctx: ScoringContext,
    write_groups: Mapping[str, Iterable[str]],
    before: Mapping[str, str],
    pass_: MovePass,
    source_compatible: str | None,
    pinned: Literal["move", "hold"],
    actor: MoveActor = MoveActor.RULE,
) -> CountReplacement:
    """Re-place every waived count whose table's source of truth the pass changed.

    ``query_assignments`` is the pass's new version (dicts or ``QueryAssignment``
    models), after its own moves; ``before`` is the sources of truth of the version it
    started from. ``pinned`` is what a customer-pinned count gets: ``"move"`` (the
    customer gate, the customer's own edit moved its writes) or ``"hold"`` (a rule pass:
    left where it is, pin kept). Each list is in query id order, all empty when the pass
    moved no table's writes or none of their counts relied on a waiver."""
    qas = sorted(query_assignments, key=lambda qa: str(_get(qa, "query_id")))
    after = version_sources(qas, write_groups)
    changed = changed_sources(before, after)
    if not changed:
        return CountReplacement([], [], [])
    after_ctx = ctx.with_sources_of_truth(after)
    in_scope = [qa for qa in qas if _get(qa, "in_scope") is not False]
    kept = sorted(
        {str(_get(qa, "assigned_engine")) for qa in in_scope if _get(qa, "assigned_engine")}
        - set(CACHE_OVERLAY_ENGINES)
    )
    settled = settled_counts(qas)
    moved: list[CountMove] = []
    held: list[str] = []
    unserved: list[str] = []
    for qa in in_scope:
        qid = str(_get(qa, "query_id"))
        engine = str(_get(qa, "assigned_engine") or "")
        query = query_by_id.get(qid)
        if query is None or not engine:
            continue
        table = lapsed(query, engine, ctx, after)
        if table is None or table not in changed:
            continue
        if pinned == "hold" and _get(qa, "customer_override"):
            held.append(qid)
            continue
        candidates = {
            e: s
            for e in kept
            if can_own(e, query, ctx.source_engine)
            and (s := score_query_for_engine(query, e, after_ctx).score) is not None
        }
        if not candidates:
            logger.warning(
                "waived count %r: no kept engine can serve it now that %s's writes moved; "
                "left as placed",
                qid[:80],
                table,
            )
            unserved.append(qid)
            continue
        top = max(candidates.values())
        tied = sorted(e for e, s in candidates.items() if s == top)
        tie_kind: str | None = None
        if len(tied) == 1:
            (winner,) = tied
            reason_code = (
                ReasonCode.KEY_SCOPED_COUNT
                if winner == after.get(table)
                else ReasonCode.HIGHEST_SCORE
            )
        else:
            winner, tie_kind = break_tie(tied, {engine}, source_compatible, settled, qid)
            reason_code = tie_reason_code(tie_kind, tied, settled, qid)
        source = after.get(table)
        where = f"moved to {display_engine(source)}" if source else "moved off it, onto two engines"
        reason = (
            f"Re-placed with its table: a key-scoped count of {table} runs on "
            f"{display_engine(engine)} only while {table}'s writes are there (ADR-031 "
            f"Decision 2 Amendment 12), and they {where}, so it is on "
            f"{display_engine(winner)}, its best-scoring kept engine."
        )
        apply_to_assignment(
            ledger,
            qa,
            winner,
            pass_=pass_,
            reason_code=reason_code,
            score_before=None,
            score_after=top,
            runner_up=best_other(candidates, winner),
            evidence=MoveEvidence(candidates=scorer_ladder(query, after_ctx)),
            tied_engines=tied if tie_kind == TIE_KIND_UNRESOLVED else [],
            actor=actor,
        )
        _set(qa, "assignment_reason", reason)
        unpinned = bool(_get(qa, "customer_override"))
        if unpinned:
            _set(qa, "customer_override", False)
        moved.append(
            CountMove(
                query_id=qid,
                table=table,
                from_engine=engine,
                to_engine=winner,
                source_of_truth=source,
                score_after=top,
                reason=reason,
                unpinned=unpinned,
            )
        )
    return CountReplacement(moved, held, unserved)


def unpinned_warning(move: CountMove) -> str:
    """The validation warning for a pinned count the move had to clear."""
    return (
        f"Query {move.query_id} was pinned to {display_engine(move.from_engine)}, which serves "
        f"a key-scoped count of {move.table} only while the table's writes are there; they "
        f"moved, so it moved to {display_engine(move.to_engine)} and is no longer pinned."
    )
