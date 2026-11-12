# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What the slice plan reads of a job's assignment and collection (#1129, #1698).

Synthesis builds ``SliceInputs`` here, once, from the effective assignment: each in-scope
query with an engine (its collected tables and call rate), the write groups and each
written table's one writer (ADR-031 Amendment 9), the co-dependency groups, the routine
units, each routine's temporary-object followers with its leaders (Amendment 20, from
``report.json``'s ``routine_followers``) and the collection's foreign keys. The facts
layer computes the plan from them (``src.shared.slice_plan``); nothing here routes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from src.agents.referee.routine_units import routine_leaders, routine_units_of
from src.agents.referee.table_resolution import SourceScope
from src.agents.referee.write_groups import sources_of_truth
from src.shared.move_units import SliceInputs, SliceQuery
from src.shared.placeholder_tables import is_placeholder_table, saved_write_groups

__all__ = ["slice_inputs"]


def slice_inputs(
    assignment: Mapping[str, Any] | None,
    collector: Mapping[str, Any] | None,
    followers: Iterable[Mapping[str, Any]] = (),
) -> SliceInputs:
    """The slice plan's inputs from ``assignment`` (an artifact dict) and ``collector``.

    ``followers`` are ``report.json``'s ``routine_followers`` (#1891): each
    temporary-object statement that sits on its routine's engine, unpinned. Each
    joins its routine's statements as one kept-whole group (Amendment 20)."""
    assignment = assignment or {}
    collector = collector or {}
    patterns = (collector.get("queries") or {}).get("query_patterns") or []
    rate = {
        str(q.get("query_id")): float(q.get("calls_per_second") or 0.0)
        for q in patterns
        if isinstance(q, Mapping)
    }
    write_groups = {t: tuple(m) for t, m in saved_write_groups(assignment).items()}
    known = _collected_tables(collector) | set(write_groups)
    queries = []
    for qa in assignment.get("query_assignments") or []:
        engine = str(qa.get("assigned_engine") or "")
        qid = str(qa.get("query_id") or "")
        if not qid or not engine or engine == "unservable" or not qa.get("in_scope", True):
            continue
        tables = tuple(
            sorted(
                {
                    str(t)
                    for t in qa.get("source_tables") or []
                    if t and not is_placeholder_table(str(t)) and (not known or str(t) in known)
                }
            )
        )
        cache = qa.get("cache_engine") if not qa.get("cache_dropped") else None
        queries.append(
            SliceQuery(
                query_id=qid,
                engine=engine,
                tables=tables,
                cache_engine=str(cache) if cache else None,
                calls_per_second=rate.get(qid, 0.0),
            )
        )
    routine_followers: dict[str, list[str]] = {}
    for row in followers:
        qid, routine = str(row.get("query_id") or ""), str(row.get("routine_id") or "")
        if qid and routine:
            routine_followers.setdefault(routine, []).append(qid)
    leaders = (
        routine_leaders(patterns, SourceScope.from_collector(dict(collector)))
        if routine_followers
        else {}
    )
    engine_of = {q.query_id: q.engine for q in queries}
    return SliceInputs(
        queries=tuple(queries),
        writes_by_table=write_groups,
        owners=sources_of_truth(write_groups, engine_of),
        co_dependency_groups=tuple(
            tuple(str(q) for q in g) for g in assignment.get("co_dependency_groups") or []
        ),
        routine_units={r: tuple(m) for r, m in routine_units_of(assignment, collector).items()},
        routine_followers={
            r: tuple(dict.fromkeys([*leaders.get(r, []), *qids]))
            for r, qids in sorted(routine_followers.items())
        },
        foreign_keys=_foreign_keys(collector),
    )


def _collected_tables(collector: Mapping[str, Any]) -> set[str]:
    """The collected tables' and views' ids: the tables a plan carries. A name a query
    reads that the collection does not hold (a system catalog, an unresolved name) is no
    table the plan moves, unless a write group is keyed on it."""
    schema = collector.get("database_schema") or {}
    return {
        str(row.get(key))
        for rows, key in ((schema.get("tables"), "table_id"), (schema.get("views"), "view_id"))
        for row in rows or []
        if isinstance(row, Mapping) and row.get(key)
    }


def _foreign_keys(collector: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    """``(table, referenced table)`` for each foreign key the collection names, by table id."""
    tables = [
        t
        for t in (collector.get("database_schema") or {}).get("tables") or []
        if isinstance(t, Mapping) and t.get("table_id")
    ]
    ids = {str(t["table_id"]) for t in tables}
    by_name: dict[str, set[str]] = {}
    for t in tables:
        by_name.setdefault(str(t.get("table_name") or "").lower(), set()).add(str(t["table_id"]))
    out: set[tuple[str, str]] = set()
    for t in tables:
        table = str(t["table_id"])
        schema = str(t.get("schema_name") or "")
        for fk in t.get("foreign_keys") or []:
            ref = str((fk or {}).get("referenced_table") or "")
            if not ref:
                continue
            if ref in ids:
                target = ref
            elif f"{schema}.{ref}" in ids:
                target = f"{schema}.{ref}"
            else:
                named = by_name.get(ref.rsplit(".", 1)[-1].lower(), set())
                if len(named) != 1:
                    continue
                (target,) = named
            if target != table:
                out.add((table, target))
    return tuple(sorted(out))
