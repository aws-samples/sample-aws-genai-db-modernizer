# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A statement on temporary objects follows its routine (#1891, ADR-031 Decision 4
Amendment 20).

A SQL Server statement that touches only temporary tables or table variables is
procedure-internal (#1876, :func:`~src.agents.referee.utility_statements.
procedure_internal_reason`): it works on scratch rows of the procedure, function or
trigger it runs in. When the collection names that routine
(``QueryPattern.parent_routine_ids``: SQL Server's ``sys.dm_exec_sql_text(sql_handle).
objectid``), the statement is a *follower* of the routine's other statements, the ones
routed on their own (not utility statements), its *leaders*:

- it is in no placement unit (no co-dependency group, no write group: it writes no
  table), so no pass scores it or gates a unit on it, and its capabilities
  (``triage.detect_query_capabilities``) are none: a temporary-table step never pulls
  its routine anywhere;
- every routing pass places the leaders without it, then puts it on the engine its
  leaders are on, through the move ledger (rule ``group.routine_follow``,
  :func:`follow_routines`);
- the last resort is the #1876 utility pin on the relational (Aurora) engine, which has
  temporary tables: when its leaders are on more than one engine or none, or their
  engine may not own it.

Not a follower, and kept with the utility statements as #1876 does: a statement whose
routine the collection does not name (``parent_routine_ids`` None or empty), one it links
to two or more routines (a shape several routines share is not one routine's), and one
whose routine runs nothing routed on its own.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping, MutableSequence
from dataclasses import dataclass, field
from typing import Any

from src.agents.referee.cache_overlay import can_own
from src.agents.referee.table_resolution import SourceScope
from src.agents.referee.utility_statements import is_utility_statement, procedure_internal_reason
from src.shared.engine_names import display_engine
from src.tools.sql.view_lineage import access_text

__all__ = [
    "RoutineFollowers",
    "follow_reason",
    "follow_routines",
    "following",
    "pin_reason",
    "relational_engine_of",
    "unplaced_reason",
    "routine_followers",
    "routine_leaders",
]


def _routine_ids(query: Mapping) -> list[str]:
    ids = query.get("parent_routine_ids") or []
    return [str(r) for r in ids if r] if isinstance(ids, list) else []


def routine_leaders(queries: Iterable[Mapping], source: SourceScope | None) -> dict[str, list[str]]:
    """``{routine_id: [query_id, ...]}``: each routine's statements routed on their own
    (not utility statements), in query order."""
    out: dict[str, list[str]] = {}
    for query in queries:
        qid = str(query.get("query_id") or "")
        routines = _routine_ids(query)
        if not qid or not routines:
            continue
        if is_utility_statement(access_text(query), query.get("tables_accessed"), source):
            continue
        for rid in routines:
            members = out.setdefault(rid, [])
            if qid not in members:
                members.append(qid)
    return out


def routine_followers(queries: Iterable[Mapping], source: SourceScope | None) -> dict[str, str]:
    """``{query_id: routine_id}``: each procedure-internal statement that follows its
    routine (see the module docstring), in query order.

    Empty for a source other than SQL Server (no statement is procedure-internal there)
    and for a collection that names no statement's routine.
    """
    queries = list(queries)
    if not any(_routine_ids(q) for q in queries):
        return {}
    leaders = routine_leaders(queries, source)
    out: dict[str, str] = {}
    for query in queries:
        qid = str(query.get("query_id") or "")
        routines = _routine_ids(query)
        if not qid or len(set(routines)) != 1 or routines[0] not in leaders:
            continue
        if procedure_internal_reason(access_text(query), query.get("tables_accessed"), source):
            out[qid] = routines[0]
    return out


@dataclass(frozen=True)
class RoutineFollowers:
    """A collection's followers, their routines' leaders and what the follow rule reads."""

    routine_of: Mapping[str, str] = field(default_factory=dict)
    leaders: Mapping[str, list[str]] = field(default_factory=dict)
    queries: Mapping[str, Mapping] = field(default_factory=dict)
    reasons: Mapping[str, str] = field(default_factory=dict)
    source_engine: str = ""

    @classmethod
    def of(cls, queries: Iterable[Mapping], source: SourceScope | None) -> RoutineFollowers:
        queries = list(queries)
        routine_of = routine_followers(queries, source)
        if not routine_of:
            return cls(source_engine=(source.engine if source else None) or "")
        by_id = {str(q.get("query_id")): q for q in queries}
        return cls(
            routine_of=routine_of,
            leaders=routine_leaders(queries, source),
            queries=by_id,
            reasons={
                qid: str(
                    procedure_internal_reason(
                        access_text(by_id[qid]), by_id[qid].get("tables_accessed"), source
                    )
                )
                for qid in routine_of
            },
            source_engine=(source.engine if source else None) or "",
        )

    @classmethod
    def from_collector(cls, collector_output: Mapping | None) -> RoutineFollowers:
        collector_output = collector_output or {}
        return cls.of(
            (collector_output.get("queries") or {}).get("query_patterns") or [],
            SourceScope.from_collector(dict(collector_output)),
        )

    def __contains__(self, qid: object) -> bool:
        return qid in self.routine_of

    def __bool__(self) -> bool:
        return bool(self.routine_of)

    def target(self, qid: str, engine_of: Mapping[str, str]) -> str | None:
        """The engine ``qid``'s leaders are all on, when it may own ``qid``; None when
        they are on more than one engine or none (the last resort applies)."""
        engines = {
            engine_of[leader]
            for leader in self.leaders.get(self.routine_of.get(qid, ""), [])
            if engine_of.get(leader) and engine_of[leader] != "unservable"
        }
        if len(engines) != 1:
            return None
        (engine,) = engines
        return engine if can_own(engine, self.queries.get(qid), self.source_engine) else None


def following(assignment: Mapping | None, collector_output: Mapping | None) -> list[dict]:
    """The followers ``assignment`` has on their routine's engine, unpinned (#1891):
    ``[{query_id, routine_id, engine}]`` in query order, for ``report.json``'s
    ``routine_followers``. A follower its last resort pinned is not listed."""
    followers = RoutineFollowers.from_collector(collector_output)
    if not followers:
        return []
    records = [
        qa
        for qa in (assignment or {}).get("query_assignments") or []
        if qa.get("query_id") and qa.get("in_scope", True)
    ]
    engine_of = {str(qa["query_id"]): str(qa.get("assigned_engine") or "") for qa in records}
    pinned = {str(qa["query_id"]) for qa in records if qa.get("pinned")}
    return [
        {"query_id": qid, "routine_id": routine, "engine": engine_of[qid]}
        for qid, routine in followers.routine_of.items()
        if qid in engine_of
        and qid not in pinned
        and followers.target(qid, engine_of) == engine_of[qid]
    ]


def follow_reason(kind: str, routine: str, engine: str) -> str:
    """A follower's assignment reason: ``kind`` (its procedure-internal reason), the
    routine and the engine it follows it to."""
    return f"{kind}: follows routine {routine} to {display_engine(engine)}"


def unplaced_reason(kind: str, routine: str) -> str:
    """Why a follower is unservable (#1891): its routine's statements are on no engine
    that can take it, and no relational engine, its last resort, is in the scope."""
    return (
        f"{kind}: follows routine {routine}, but its routine's other statements are on no "
        "one engine that can take it, and no relational engine, its last resort, is in "
        "the plan"
    )


def pin_reason(kind: str, engine: str, compatible: bool) -> str:
    """The #1876 utility pin's reason, the follower's last resort."""
    return (
        f"{kind}, kept on the {'source-compatible' if compatible else 'chosen'} "
        f"relational engine ({display_engine(engine)})"
    )


def relational_engine_of(
    assignment: Any, collector_output: Mapping | None
) -> tuple[str | None, bool]:
    """The relational engine a follower's last resort pins it to, and whether it is the
    source-compatible one: the Aurora engine the assignment chose
    (``aurora_engine_choice``, ADR-031 Decision 6, which a customer's engine scope only
    sets in scope), else the source's own, else the first assignment's pick (the Aurora
    engine its utility statements are pinned to), else the one Aurora engine the
    assignment's queries use; None with none (no Aurora engine in the plan)."""
    from src.agents.referee.aurora_choice import source_database_engine
    from src.agents.referee.triage import SOURCE_ENGINE_TO_AURORA

    compatible = SOURCE_ENGINE_TO_AURORA.get(source_database_engine(dict(collector_output or {})))
    choice = (
        (assignment or {}).get("aurora_engine_choice")
        if isinstance(assignment, Mapping) or assignment is None
        else getattr(assignment, "aurora_engine_choice", None)
    ) or {}
    chosen = (
        choice.get("engine") if isinstance(choice, Mapping) else getattr(choice, "engine", None)
    )
    engine = chosen or compatible
    if not engine:
        # One Aurora engine analysed and no choice recorded: the one the assignment's
        # queries already use, when there is exactly one.
        from src.agents.referee.aurora_choice import AURORA_ENGINES

        records = (
            (assignment or {}).get("query_assignments") or []
            if isinstance(assignment, Mapping)
            else getattr(assignment, "query_assignments", None) or []
        )
        # The first assignment's pick (``pick_aurora_engine``) is where it pinned the
        # utility statements; with none pinned, the one Aurora engine in use.
        pinned = {
            _get(qa, "assigned_engine") for qa in records if _get(qa, "pinned") == "utility"
        } & AURORA_ENGINES
        used = pinned or {_get(qa, "assigned_engine") for qa in records} & AURORA_ENGINES
        engine = None
        if len(used) == 1:
            (engine,) = used  # the one engine, never a pick among several
    return engine, bool(engine) and engine == compatible


def _get(qa: Any, key: str) -> Any:
    return qa.get(key) if isinstance(qa, Mapping) else getattr(qa, key, None)


def _set(qa: Any, key: str, value: Any) -> None:
    if isinstance(qa, MutableMapping):
        qa[key] = value
    else:
        setattr(qa, key, value)


def follow_routines(
    query_assignments: MutableSequence[Any] | MutableMapping[str, Any],
    followers: RoutineFollowers,
    ledger: Any,
    *,
    relational_engine: str | None,
    source_compatible: bool = False,
    scope: Iterable[str] | None = None,
    assignment: Any = None,
) -> list[str]:
    """Put every follower in ``query_assignments`` on its leaders' engine, as a Move on
    ``ledger`` (pass ``co_dependency``, rule ``group.routine_follow``), or, as the last
    resort, pin it to ``relational_engine`` (rule ``override.utility``), or, with no
    relational engine (none in the engine ``scope``), make it unservable with
    :func:`unplaced_reason` (``move_ledger.unservable_in_assignment``). A follower
    ``assignment`` already lists as unservable comes back on its leaders' engine once
    they are on one that can take it (``move_ledger.servable_in_assignment``). The step
    every routing pass ends with, after it places the leaders without the followers.

    ``query_assignments`` is the pass's records, a list or a ``{query_id: record}``
    mapping, edited in place; ``assignment`` (a dict or an ``Assignment``) the version
    whose ``unservable_queries`` the ledger edits, and ``ledger`` must start from it
    (``ledger_for(..., unservable=...)``). With no ``assignment``, a follower with no
    target and no relational engine stays where it is. A follower the customer re-routed
    (``customer_override``) is the customer's and is left where it is. Returns the query
    ids moved.
    """
    from src.agents.referee.engine_scope import unservable_record
    from src.agents.referee.move_ledger import (
        apply_to_assignment,
        servable_in_assignment,
        unservable_in_assignment,
    )
    from src.contracts.assignment_models import QueryAssignment
    from src.contracts.move_ledger import UNSERVABLE, MovePass, ReasonCode
    from src.shared.workload_fit_labels import PINNED_UTILITY

    if not followers:
        return []
    records = list(
        query_assignments.values()
        if isinstance(query_assignments, MutableMapping)
        else query_assignments
    )
    engine_of = {
        str(_get(qa, "query_id")): str(_get(qa, "assigned_engine") or "")
        for qa in records
        if _get(qa, "in_scope") is not False
    }
    confidence = {str(_get(qa, "query_id")): _get(qa, "confidence") for qa in records}

    def leader_score(routine: str, target: str) -> float | None:
        scores = [
            float(c)
            for leader in followers.leaders[routine]
            if engine_of.get(leader) == target and (c := confidence.get(leader)) is not None
        ]
        return sum(scores) / len(scores) if scores else None

    moved: list[str] = []
    for qa in records:
        qid = str(_get(qa, "query_id"))
        if qid not in followers or _get(qa, "customer_override"):
            continue
        routine = followers.routine_of[qid]
        kind = followers.reasons.get(qid, "procedure-internal")
        target = followers.target(qid, engine_of)
        current = _get(qa, "assigned_engine")
        if target is not None:
            # A Move when the engine changes, and also when it stays but the record
            # still carries a tie or the pin of an earlier last resort: a follower holds
            # neither (the same placement reads the same, whatever its history).
            if target != current or _get(qa, "tied_engines") or _get(qa, "pinned"):
                apply_to_assignment(
                    ledger,
                    qa,
                    target,
                    pass_=MovePass.CO_DEPENDENCY,
                    reason_code=ReasonCode.ROUTINE_FOLLOW,
                    score_after=leader_score(routine, target),
                    tied_engines=[],
                )
                moved.append(qid)
            _set(qa, "assignment_reason", follow_reason(kind, routine, target))
        elif relational_engine:
            if current != relational_engine or _get(qa, "pinned") != PINNED_UTILITY:
                apply_to_assignment(
                    ledger,
                    qa,
                    relational_engine,
                    pass_=MovePass.UTILITY_PIN,
                    reason_code=ReasonCode.OVERRIDE_UTILITY,
                    tied_engines=[],
                )
                _set(
                    qa, "assignment_reason", pin_reason(kind, relational_engine, source_compatible)
                )
                moved.append(qid)
        elif assignment is not None:
            record = unservable_record(
                qid,
                [],
                rule=ReasonCode.FALLBACK_NO_SCORE.value,
                scope=sorted(scope or ()),
                closest_engine=None,
                gates=[],
            )
            record["reason"] = unplaced_reason(kind, routine)
            unservable_in_assignment(
                ledger,
                assignment,
                qa,
                record,
                records=query_assignments,
                reason_code=ReasonCode.FALLBACK_NO_SCORE,
                closest_engine=None,
            )
            engine_of.pop(qid, None)
            moved.append(qid)
            continue
        _set(qa, "source_tables", [])
        engine_of[qid] = str(_get(qa, "assigned_engine") or "")
    # A follower an earlier pass made unservable comes back once its leaders are on one
    # engine that can take it: the same rule, read the same way in every pass.
    listed = (
        assignment.get("unservable_queries")
        if isinstance(assignment, Mapping)
        else getattr(assignment, "unservable_queries", None)
    ) or []
    for u in list(listed):
        qid = str(_get(u, "query_id"))
        if qid not in followers or qid in moved:
            continue
        target = followers.target(qid, engine_of)
        if target is None:
            continue
        routine = followers.routine_of[qid]
        model = QueryAssignment(
            query_id=qid,
            assigned_engine=UNSERVABLE,
            confidence=0,
            source_tables=[],
            assignment_reason=follow_reason(
                followers.reasons.get(qid, "procedure-internal"), routine, target
            ),
        )
        back: Any = (
            model
            if isinstance(query_assignments, MutableMapping)
            else model.model_dump(mode="json")
        )
        servable_in_assignment(
            ledger,
            assignment,
            back,
            target,
            records=query_assignments,
            pass_=MovePass.CO_DEPENDENCY,
            reason_code=ReasonCode.ROUTINE_FOLLOW,
            score_after=leader_score(routine, target),
            tied_engines=[],
        )
        moved.append(qid)
    return moved
