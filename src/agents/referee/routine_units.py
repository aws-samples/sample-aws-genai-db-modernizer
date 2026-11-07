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

Routine units (#1654, ADR-031 Decision 4 Amendment 21)
------------------------------------------------------

On the source, a procedure's statements run together, in the routine (in one transaction
where the routine opens one), and a trigger's run in the statement, and so the
transaction, of the write that fires it. Each routine the collection names gives a
*routine unit* (:func:`build_routine_units`): its leaders (the statements routed on their
own, so no utility statement and no follower), and, for an enabled trigger, the routed
writes that fire it (a write of the trigger's table whose statement type is one of the
trigger's events; a ``MERGE``, or a write whose type the collection does not give, fires
every event). A unit keeps only these statements and is dropped below two. Routing
places each unit as one, through the one placement-group merge
(``write_groups.placement_groups``, a third source next to co-dependency groups and write
groups); its engine comes from the shared group rule, and the first assignment records
``group.routine`` for a member no write group holds. The assignment saves the units
(``Assignment.routine_units``), and every later pass reads the saved ones
(:func:`routine_units_for`); only an assignment saved before routine units, which
records none, falls back to the collection's. ``report.json`` lists what each merged
unit holds and which routines merged it (:func:`routine_unit_facts`). A collection that
names no routine has no unit, and routes exactly as before.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping, MutableSequence
from dataclasses import dataclass, field
from typing import Any

from src.agents.referee.cache_overlay import can_own
from src.agents.referee.table_resolution import SourceScope
from src.agents.referee.utility_statements import is_utility_statement, procedure_internal_reason
from src.shared.engine_names import display_engine
from src.tools.sql.query_facts import source_engine_of
from src.tools.sql.view_lineage import access_text

__all__ = [
    "ROUTINE_UNIT_LABEL",
    "RoutineFollowers",
    "build_routine_units",
    "collector_routine_units",
    "routine_unit_facts",
    "routine_unit_label",
    "routine_unit_members",
    "routine_units_for",
    "routine_units_of",
    "saved_routine_units",
    "without_unservable_routine_units",
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


# ---------------------------------------------------------------------------
# Routine units (#1654, ADR-031 Decision 4 Amendment 21)
# ---------------------------------------------------------------------------

# The assignment reason's label for a member placed by its routine unit (the move
# ledger's rule is ``group.routine``).
ROUTINE_UNIT_LABEL = "routine unit"

# The trigger events a write of each statement type fires. A ``MERGE`` (and a write the
# collection types ``OTHER``, a data-modifying CTE for one) can insert, update and delete,
# so it fires every event: keeping too much together never splits a transaction.
_ALL_EVENTS = frozenset({"INSERT", "UPDATE", "DELETE"})
_FIRES: dict[str, frozenset[str]] = {
    "INSERT": frozenset({"INSERT"}),
    "UPDATE": frozenset({"UPDATE"}),
    "DELETE": frozenset({"DELETE"}),
    "REPLACE": frozenset({"INSERT", "DELETE"}),
}


def _norm(name: object) -> str:
    return str(name or "").strip().strip('[]"`').lower()


def _split_name(name: object) -> tuple[str, str]:
    """``(schema, table)`` of a collected name, lower-case, ``""`` for no schema."""
    parts = [_norm(p) for p in str(name or "").split(".") if p.strip()]
    if not parts:
        return "", ""
    return (parts[-2] if len(parts) >= 2 else ""), parts[-1]


def _same_table(trigger_table: object, schema: object, written: object) -> bool:
    """Whether a written table is the trigger's: the same name, and the same schema
    when both name one (the trigger's ``schema_name`` stands in for a bare
    ``table_id``)."""
    t_schema, t_name = _split_name(trigger_table)
    t_schema = t_schema or _norm(schema)
    w_schema, w_name = _split_name(written)
    if not t_name or t_name != w_name:
        return False
    return not t_schema or not w_schema or t_schema == w_schema


def _trigger_routine_id(trigger: Mapping) -> str:
    """The id the collection's statement links give a trigger: ``<schema>.<name>``
    (``routine_links.routine_id``), or its ``trigger_id`` when that already names a
    schema."""
    from src.tools.database.routine_links import routine_id

    tid = str(trigger.get("trigger_id") or "")
    if "." in tid:
        return tid
    return routine_id(trigger.get("schema_name"), trigger.get("trigger_name") or tid) or tid


def _fires(
    query: Mapping, trigger: Mapping, source_engine: str, source: SourceScope | None
) -> bool:
    """Whether ``query``, a routed statement, is a write that fires ``trigger``."""
    from src.agents.referee.cache_overlay import is_write_query
    from src.agents.referee.write_groups import written_tables

    if trigger.get("is_enabled") is False or not is_write_query(query, source_engine):
        return False
    events = {str(e).upper() for e in trigger.get("events") or []} or {
        str(trigger.get("event_type") or "").upper()
    }
    fired = _FIRES.get(str(query.get("query_type") or "").upper(), _ALL_EVENTS)
    if not events & fired:
        return False
    return any(
        _same_table(trigger.get("table_id"), trigger.get("schema_name"), table)
        for table in written_tables(query, source_engine, source)
    )


def build_routine_units(
    queries: Iterable[Mapping],
    source: SourceScope | None,
    schema: Mapping | None = None,
    source_engine: str = "",
) -> list[dict[str, Any]]:
    """Each routine unit of a collection's statements (see the module docstring):
    ``[{routine_id, kind, query_ids, fired_by}]``, by routine id.

    ``query_ids`` are the routine's leaders (``routine_leaders``), in query order;
    ``fired_by``, for an enabled trigger in ``schema`` (``database_schema``), the
    routed writes that fire it, in query order, none of them its own statements.
    ``kind`` is ``trigger`` for a trigger in ``schema``, ``function`` for a procedure
    entry of type ``FUNCTION``, else ``procedure``. A unit of fewer than two statements
    is left out. Empty for a collection that names no statement's routine.
    """
    queries = list(queries)
    if not any(_routine_ids(q) for q in queries):
        return []
    source_engine = source_engine or ((source.engine if source else None) or "")
    schema = schema or {}
    leaders = routine_leaders(queries, source)
    triggers = {
        _norm(_trigger_routine_id(t)): t
        for t in schema.get("triggers") or []
        if isinstance(t, Mapping)
    }
    functions = {
        _norm(p.get("procedure_id"))
        for p in schema.get("procedures") or []
        if isinstance(p, Mapping) and str(p.get("procedure_type") or "").upper() == "FUNCTION"
    }
    routed = [
        q
        for q in queries
        if q.get("query_id")
        and not is_utility_statement(access_text(q), q.get("tables_accessed"), source)
    ]
    units: list[dict[str, Any]] = []
    for rid in sorted(leaders):
        members = list(leaders[rid])
        trigger = triggers.get(_norm(rid))
        fired_by: list[str] = []
        if trigger is not None:
            own = set(members)
            fired_by = [
                str(q["query_id"])
                for q in routed
                if str(q["query_id"]) not in own and _fires(q, trigger, source_engine, source)
            ]
        if len(members) + len(fired_by) < 2:
            continue
        kind = (
            "trigger"
            if trigger is not None
            else "function" if _norm(rid) in functions else "procedure"
        )
        units.append({"routine_id": rid, "kind": kind, "query_ids": members, "fired_by": fired_by})
    return units


def collector_routine_units(collector_output: Mapping | None) -> list[dict[str, Any]]:
    """:func:`build_routine_units` of a collector output's statements and schema, read
    in its source engine's dialect."""
    collector_output = collector_output or {}
    return build_routine_units(
        (collector_output.get("queries") or {}).get("query_patterns") or [],
        SourceScope.from_collector(dict(collector_output)),
        collector_output.get("database_schema") or {},
        source_engine_of(collector_output),
    )


def _unit_dict(unit: Any) -> dict[str, Any]:
    raw = unit if isinstance(unit, Mapping) else unit.model_dump(mode="json")
    return {
        "routine_id": str(raw.get("routine_id") or ""),
        "kind": str(raw.get("kind") or "procedure"),
        "query_ids": [str(q) for q in raw.get("query_ids") or []],
        "fired_by": [str(q) for q in raw.get("fired_by") or []],
    }


def routine_unit_members(units: Iterable[Any]) -> dict[str, list[str]]:
    """``{routine_id: [query_id, ...]}``: each unit's statements, then the writes that
    fire it, each once; the third source :func:`~src.agents.referee.write_groups.
    placement_groups` merges."""
    out: dict[str, list[str]] = {}
    for unit in units:
        u = _unit_dict(unit)
        if u["routine_id"]:
            out[u["routine_id"]] = list(dict.fromkeys([*u["query_ids"], *u["fired_by"]]))
    return out


def saved_routine_units(assignment: Any) -> list[dict[str, Any]]:
    """An assignment's routine units (``Assignment.routine_units``, a dict's key or a
    model's field), as dicts; ``[]`` for an assignment saved before #1654."""
    raw = (
        assignment.get("routine_units")
        if isinstance(assignment, Mapping)
        else getattr(assignment, "routine_units", None)
    )
    return [_unit_dict(u) for u in raw or []]


def records_routine_units(assignment: Any) -> bool:
    """Whether ``assignment`` records its routine units (#1654): a dict with the
    ``routine_units`` key, or an ``Assignment`` whose field was set (read from a file
    that has it, or built by a pass that set it). An assignment saved before routine
    units records none."""
    if isinstance(assignment, Mapping):
        return "routine_units" in assignment
    fields_set = getattr(assignment, "model_fields_set", None)
    return fields_set is not None and "routine_units" in fields_set


def routine_units_for(assignment: Any, collector_output: Mapping | None) -> list[dict[str, Any]]:
    """The routine units a pass deriving a version from ``assignment`` places: the ones
    the assignment saved, or, for an assignment saved before routine units (which
    records none), the collection's (:func:`collector_routine_units`). Every pass after
    the first assignment reads its units here."""
    if records_routine_units(assignment):
        return saved_routine_units(assignment)
    return collector_routine_units(collector_output) if collector_output else []


def routine_units_of(
    assignment: Any, collector_output: Mapping | None = None
) -> dict[str, list[str]]:
    """:func:`routine_unit_members` of :func:`routine_units_for`."""
    return routine_unit_members(routine_units_for(assignment, collector_output))


def without_unservable_routine_units(
    units: Iterable[Any], unservable: Iterable[Any]
) -> list[dict[str, Any]]:
    """The routine units of an assignment's assigned queries: a unit is unservable as
    one (#1329), so one whose every statement is unservable is left out, the same rule
    ``engine_scope.without_unservable_groups`` applies to the other groups."""
    ids = {
        str(u.get("query_id") if isinstance(u, Mapping) else getattr(u, "query_id", u))
        for u in unservable
    }
    out = [_unit_dict(u) for u in units]
    if not ids:
        return out
    return [u for u in out if not set(u["query_ids"]) | set(u["fired_by"]) <= ids]


def routine_unit_label(routines: Iterable[str]) -> str:
    """The assignment reason's label for a member a routine unit placed."""
    return f"{ROUTINE_UNIT_LABEL} ({', '.join(routines)})"


def routine_unit_facts(
    assignment: Mapping | None, collector_output: Mapping | None = None
) -> list[dict[str, Any]]:
    """``report.json``'s ``routine_units`` (#1654): each placement unit a routine merged,
    with what merged it, ``[{merged_by: [{kind, id}], query_ids, tables, engine,
    engines}]``, in the order of the placement units; ``kind`` is the routine's
    (``procedure``, ``function`` or ``trigger``), procedures and functions first.

    ``query_ids`` are the unit's in-scope statements, ``tables`` the source tables they
    read or write, ``engines`` the engines they are on and ``engine`` the one engine
    when there is one (None when a customer edit split the unit). Empty for an
    assignment saved before routine units, or with none.
    """
    from src.agents.referee.write_groups import assignment_placement_groups

    assignment = assignment or {}
    units = routine_units_for(assignment, collector_output)
    if not units:
        return []
    records = {
        str(qa.get("query_id")): qa
        for qa in assignment.get("query_assignments") or []
        if qa.get("query_id") and qa.get("in_scope", True)
    }
    members = routine_unit_members(units)
    kinds = {u["routine_id"]: u["kind"] for u in units}
    out: list[dict[str, Any]] = []
    for group in assignment_placement_groups(assignment, collector_output):
        in_group = set(group)
        merged_by = [
            {"kind": kinds[rid], "id": rid}
            for rid in sorted(members, key=lambda r: (kinds[r] == "trigger", r))
            if set(members[rid]) & in_group
        ]
        if not merged_by:
            continue
        placed = [q for q in group if q in records]
        if not placed:
            continue
        engines = sorted({str(records[q].get("assigned_engine") or "") for q in placed} - {""})
        tables: dict[str, None] = {}
        for q in placed:
            for t in records[q].get("source_tables") or []:
                tables.setdefault(str(t), None)
        # The one engine, never a pick among several (a split unit has none).
        engine: str | None = None
        if len(engines) == 1:
            (engine,) = engines
        out.append(
            {
                "merged_by": merged_by,
                "query_ids": placed,
                "tables": sorted(tables),
                "engine": engine,
                "engines": engines,
            }
        )
    return out
