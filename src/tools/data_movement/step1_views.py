# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's views section (#2082): which views step 1 creates, in what order, and why
each other one is not created.

A view holds no rows, so DMS never moves it; step 1 creates it from its exact
``CREATE`` in the job's routine sources (``src.agents.collector.view_sources``), in the
views deploy script the customer runs after the full load and before the routines deploy
script. Same family only: PostgreSQL to Aurora PostgreSQL, MySQL to Aurora MySQL.

Every view of the collection is a row, except one an extension owns (``CREATE
EXTENSION`` creates it). In dependency order (a view on views after the views it reads):

1. Another engine family's view is not carried (``target_engine_differs``).
2. A view whose ``CREATE`` the collection did not keep is not carried, with its body
   status: ``body_not_collected`` (a collection that predates #2082), ``body_opted_out``,
   ``body_not_readable`` or ``body_not_supported``.
3. A view whose base tables the collection could not resolve is not carried
   (``lineage_not_resolved``).
4. A view that reads a table step 1 does not create (a blocked rule, a table moving to
   another engine, a name the collection has no table for) is not carried
   (``depends_on_table_not_in_step``).
5. A view that reads a view step 1 does not carry is not carried
   (``depends_on_not_carried``).
6. A view that calls one of the source's routines is not carried
   (``depends_on_routine:<id>``): the routines deploy script runs after the views one.
   A routine is found by its name followed by ``(`` in the view's (redacted)
   definition, so a false match only leaves a view out, never carries one that fails.
7. Anything else is carried, numbered in deploy order.

A view not carried is a named exception of the gate, with its reasons and what to do
(:data:`WHY`); a routine that names it is not carried either (``depends_on_view``). The
section holds ids, names, column names and types and reason codes, never a view's text.
Nothing in routing or synthesis reads this module. Linear in the views and their edges.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from typing import Any

from src.contracts.transformation_contract import StepContract, StepView, StepViewColumn, StepViews

__all__ = [
    "WHY",
    "attach_step_views",
    "build_step_views",
    "carried_view_names",
    "why",
]

#: The source family each target carries views from.
FAMILY = {"aurora_postgresql": ("postgresql",), "aurora_mysql": ("mysql",)}
ENGINE_DIFFERS = "target_engine_differs"
LINEAGE = "lineage_not_resolved"
TABLE_NOT_IN_STEP = "depends_on_table_not_in_step"
NOT_CARRIED_DEPENDENCY = "depends_on_not_carried"
ROUTINE_DEPENDENCY = "depends_on_routine"  # reason "depends_on_routine:<routine id>"

#: What each reason means and what to do, for the gate's exceptions and the command output.
WHY: dict[str, str] = {
    ENGINE_DIFFERS: "step 1 creates views within one engine family only; create it yourself",
    "body_not_collected": (
        "the collection predates view carrying: collect again with the current "
        "collection script, which reads what step 1 needs to create it"
    ),
    "body_opted_out": (
        "the collection ran with include_routine_bodies = 0: collect again with it on, "
        "or create the view yourself"
    ),
    "body_not_readable": (
        "the collection user cannot read its definition: collect again as its owner "
        "(MySQL: with SHOW VIEW), or create the view yourself"
    ),
    "body_not_supported": (
        "no CREATE could be built from the collection for it, or its text is not exactly one "
        "view statement (ADR-034 Decision 5): create the view yourself"
    ),
    LINEAGE: (
        "its base tables could not be resolved from its definition: create the view "
        "yourself once its tables are on Aurora"
    ),
    TABLE_NOT_IN_STEP: (
        "it reads a table step 1 does not move (a blocked table, or one that moves to "
        "another engine): create the view yourself once that table is where it reads it"
    ),
    NOT_CARRIED_DEPENDENCY: (
        "it reads a view step 1 does not create: create both yourself, the one it reads " "first"
    ),
    ROUTINE_DEPENDENCY: (
        "it calls a routine of the source, which routines-deploy.sql creates after the "
        "views: create the view yourself after routines-deploy.sql"
    ),
}


def why(reasons: Iterable[str]) -> str:
    """What the reasons mean and what to do, one sentence each, in their order."""
    out: list[str] = []
    for reason in reasons:
        text = WHY.get(reason.split(":", 1)[0])
        if text and text not in out:
            out.append(text)
    return "; ".join(out)


def _lower(value: object) -> str:
    return str(value or "").lower()


def _view_key(view: Mapping[str, Any]) -> str:
    return str(view.get("view_id") or view.get("view_name") or "")


def _functions(procedures: Iterable[Any]) -> list[tuple[str, re.Pattern[str]]]:
    """The source's routines a view could call, ``(id, pattern of a call)``."""
    out: list[tuple[str, re.Pattern[str]]] = []
    for p in procedures:
        if not isinstance(p, Mapping):
            continue
        name, rid = p.get("procedure_name"), p.get("procedure_id") or p.get("procedure_name")
        if isinstance(name, str) and name and rid:
            pattern = re.compile(rf"(?<![\w$])[\"`]?{re.escape(name)}[\"`]?\s*\(", re.IGNORECASE)
            out.append((str(rid), pattern))
    return out


def _order(views: dict[str, Mapping[str, Any]]) -> list[str]:
    """The view ids, each after the views it reads (Kahn's order, ties by id); a view on a
    cycle comes last (its lineage is unresolved, so it is never carried)."""
    reads = {
        vid: {r for r in (v.get("referenced_views") or ()) if r in views and r != vid}
        for vid, v in views.items()
    }
    readers: dict[str, set[str]] = {vid: set() for vid in views}
    for vid, deps in reads.items():
        for dep in deps:
            readers[dep].add(vid)
    waiting = {vid: len(deps) for vid, deps in reads.items()}
    queue = sorted(vid for vid, n in waiting.items() if n == 0)
    out: list[str] = []
    while queue:
        vid = queue.pop(0)
        out.append(vid)
        for reader in sorted(readers[vid]):
            waiting[reader] -= 1
            if waiting[reader] == 0:
                queue.append(reader)
        queue.sort()
    return out + sorted(set(views) - set(out))


def _columns(view: Mapping[str, Any]) -> list[StepViewColumn]:
    out = []
    for c in view.get("columns") or ():
        if isinstance(c, Mapping) and isinstance(c.get("name"), str) and c["name"]:
            kind = c.get("data_type")
            out.append(
                StepViewColumn(name=c["name"], data_type=kind if isinstance(kind, str) else None)
            )
        else:
            return []
    return out


def build_step_views(
    schema: Mapping[str, Any], contract: StepContract, *, source_engine: str
) -> StepViews:
    """The views section for ``contract`` from the collection's ``database_schema``
    (module docstring)."""
    from src.tools.data_movement.step1_routines import step_tables

    views = {
        _view_key(v): v
        for v in schema.get("views") or ()
        if isinstance(v, Mapping) and _view_key(v) and v.get("extension_owned") is not True
    }
    if not views:
        return StepViews(source="none")
    same_family = source_engine in FAMILY.get(contract.target.engine, ())
    tables = step_tables(contract)
    functions = _functions(schema.get("procedures") or ())
    carry_of: dict[str, bool] = {}
    carried: list[StepView] = []
    others: list[StepView] = []
    for vid in _order(views):
        v = views[vid]
        columns = _columns(v)
        reasons: list[str] = []
        status = v.get("body_status")
        if not same_family:
            reasons = [ENGINE_DIFFERS]
        elif status != "collected" or not columns:
            reasons = [
                f"body_{status}" if status and status != "collected" else "body_not_collected"
            ]
        elif v.get("lineage_status") != "resolved":
            reasons = [LINEAGE]
        else:
            if {_lower(t) for t in v.get("referenced_tables") or ()} - tables:
                reasons.append(TABLE_NOT_IN_STEP)
            if any(not carry_of.get(r, False) for r in v.get("referenced_views") or () if r != vid):
                reasons.append(NOT_CARRIED_DEPENDENCY)
            text = str(v.get("definition") or "")
            reasons += [
                f"{ROUTINE_DEPENDENCY}:{rid}" for rid, call in functions if call.search(text)
            ]
        name = str(v.get("view_name") or vid)
        reads = sorted({r for r in v.get("referenced_views") or () if r in views and r != vid})
        schema_name = v.get("schema_name") if isinstance(v.get("schema_name"), str) else None
        security = (
            v.get("security_type") if v.get("security_type") in ("DEFINER", "INVOKER") else None
        )
        carry_of[vid] = not reasons
        if reasons:
            others.append(StepView(view_id=vid, schema_name=schema_name, name=name,
                                   carry="not_carried", columns=columns, reads=reads, source_security=security,
                                   reasons=reasons))  # fmt: skip
        else:
            carried.append(StepView(view_id=vid, schema_name=schema_name, name=name, carry="carried",
                                    order=len(carried) + 1, columns=columns, reads=reads,
                                    source_security=security))  # fmt: skip
    others.sort(key=lambda r: r.view_id)
    return StepViews(
        source="collection",
        total=len(carried) + len(others),
        carried=len(carried),
        not_carried=len(others),
        views=[*carried, *others],
    )


def carried_view_names(contract: StepContract) -> set[str]:
    """The views step 1 carries, as lower-case ``schema.name`` and view id: a routine that
    names one of them is no longer held back by it."""
    section = contract.views_section
    out: set[str] = set()
    for v in section.views if section is not None else ():
        if v.carry == "carried":
            out.add(v.view_id.lower())
            out.add(f"{v.schema_name}.{v.name}".lower() if v.schema_name else v.name.lower())
    return out


def attach_step_views(
    store: Any, database_name: str, job_id: str, contract: StepContract
) -> StepContract:
    """``contract`` with its views section, for a step 1 that moves tables into Aurora
    PostgreSQL or Aurora MySQL; any other contract is returned as it is."""
    if contract.step != 1 or contract.target.engine not in FAMILY or not contract.rules:
        return contract
    key = f"{database_name}/{job_id}/collector/output.json"
    collection = (store.read_json(key) or {}) if store.exists(key) is True else {}
    schema = collection.get("database_schema") or {}
    source = (collection.get("metadata") or {}).get("source_database") or {}
    engine = str(source.get("engine") or "").lower()
    section = build_step_views(schema, contract, source_engine=engine)
    return contract.model_copy(update={"views_section": section})
