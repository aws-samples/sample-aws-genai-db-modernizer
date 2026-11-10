# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's routines section (#1947, ADR-034 Decision 6): which routines step 1 carries,
in what order, and why each other one is not carried.

Read from the job's routine-migration artifact (#1934), never computed again: the
newest ``routines/v<N>/routine_migration.json`` at or below the contract's assignment
version (a version with none falls back to an earlier one; a job with none at all gets
it written for the contract's version, by the same deterministic phase). Then, in the
artifact's deploy order:

1. A routine that needs attention (or is anything but ``as_is`` or ``rewritten``) is
   not carried, with the artifact's reasons.
2. A routine that names a view is not carried (``depends_on_view:<schema.view>``, one
   per view): step 1 creates no views. The artifact records a view as a table
   dependency, so views are told apart by the collection's view list (and the
   contract's).
3. A routine that names a table step 1 does not create (a blocked rule, or a table
   outside the step), or a trigger on such a table, is not carried
   (``depends_on_table_not_in_step``).
4. A routine that names a routine that is not carried, or one the section has not
   settled as carried, is not carried either (``depends_on_not_carried``); in deploy
   order every routine it names is settled first (the artifact puts dependency cycles in
   *needs attention*).
5. A MySQL trigger or event is deferred to the cutover script (#1948): MySQL can't
   create a trigger disabled, and an enabled event would run on the target while DMS
   replicates the effects the source's own event already had.
6. Anything else is carried, numbered in deploy order; a PostgreSQL trigger is created
   disabled.

A trigger also carries what the cutover script needs of it, from the collection (#1948):
a PostgreSQL trigger's source state (``source_state``), a MySQL trigger's timing and
event (``fires``) and ``ACTION_ORDER``.

The section holds ids, names, signatures and reason codes, never a routine's text.
Routines never change routing (ADR-034 Decision 8): nothing in routing or synthesis
reads this module. Linear in the routines.
"""

from __future__ import annotations

from typing import Any

from src.contracts.routine_migration import RoutineMigration, RoutineMigrationContract
from src.contracts.transformation_contract import StepContract, StepRoutine, StepRoutines

__all__ = [
    "DEFERRED_KINDS",
    "attach_step_routines",
    "build_step_routines",
    "step_tables",
]

#: The routine kinds each target leaves to the cutover script.
DEFERRED_KINDS = {"aurora_mysql": ("trigger", "event"), "aurora_postgresql": ()}
TABLE_NOT_IN_STEP = "depends_on_table_not_in_step"
VIEW_DEPENDENCY = "depends_on_view"  # reason "depends_on_view:<schema.view>"
NOT_CARRIED_DEPENDENCY = "depends_on_not_carried"
NOT_CARRIABLE = "status_not_carried"
ENGINE_DIFFERS = "target_engine_differs"


def step_tables(contract: StepContract) -> set[str]:
    """The tables step 1 creates, as lower-case ``schema.name``: every rule that is not
    blocked (a rule that moves no rows still creates its table)."""
    return {r.source_table.lower() for r in contract.rules if r.status != "blocked"}


def _trigger_table(row: RoutineMigration) -> str | None:
    if row.kind != "trigger" or not row.table:
        return None
    table = row.table.lower()
    return table if "." in table or not row.schema_name else f"{row.schema_name.lower()}.{table}"


TriggerKey = tuple[str, str, str]  # schema, table, trigger name (lower case on MySQL)


def _trigger_key(schema: str | None, table: str | None, name: str, engine: str) -> TriggerKey:
    table = (table or "").rpartition(".")[2]
    key = (schema or "", table, name)
    return tuple(k.lower() for k in key) if engine == "aurora_mysql" else key  # type: ignore[return-value]


def _source_state(found: dict[str, Any]) -> str | None:
    """The collection's state of a PostgreSQL trigger. The collection records only whether
    it fires for ordinary sessions (``is_enabled``: O or A reads enabled, D or R disabled),
    so the state is ``enabled`` or ``disabled``, never a ``tgenabled`` letter it does not
    have; None when the collection does not say."""
    # #2099: once the collection keeps the tgenabled letter, carry it as it is (O, A, R or
    # D); the cutover script and its check already set and verify an exact letter.
    enabled = found.get("is_enabled")
    if enabled is True:
        return "enabled"
    if enabled is False:
        return "disabled"
    return None


def _trigger_fields(
    row: RoutineMigration, engine: str, triggers: dict[TriggerKey, dict[str, Any]]
) -> dict[str, Any]:
    if row.kind != "trigger":
        return {}
    found = triggers.get(_trigger_key(row.schema_name, row.table, row.name, engine))
    if found is None:
        return {}
    if engine == "aurora_postgresql":
        return {"source_state": _source_state(found)}
    timing, event = (
        str(found.get("timing") or "").upper(),
        str(found.get("event_type") or "").upper(),
    )
    order = found.get("action_order")
    return {
        "fires": f"{timing} {event}" if timing and event else None,
        "action_order": order if isinstance(order, int) and order >= 1 else None,
    }


def _row(row: RoutineMigration, carry: str, **fields: Any) -> StepRoutine:
    return StepRoutine(
        routine_id=row.routine_id,
        kind=row.kind,
        schema_name=row.schema_name,
        name=row.name,
        specific_name=row.specific_name,
        table=row.table,
        signature=row.signature,
        returns=row.returns,
        carry=carry,  # type: ignore[arg-type]
        **fields,
    )


def build_step_routines(
    migration: RoutineMigrationContract | None,
    contract: StepContract,
    *,
    version: int | None,
    views: set[str] | None = None,
    triggers: dict[TriggerKey, dict[str, Any]] | None = None,
) -> StepRoutines:
    """The routines section for ``contract`` from ``migration`` (module docstring);
    ``source="none"`` when there is no artifact (no routine this pipeline carries).
    ``views`` are the collection's views as lower-case ``schema.name``; the contract's
    views count too. ``triggers`` are the collection's triggers by :func:`_trigger_key`."""
    if migration is None or not migration.routines:
        return StepRoutines(source="none")
    engine = contract.target.engine
    tables = step_tables(contract)
    view_names = {v.lower() for v in contract.views} | set(views or ())
    deferred_kinds = DEFERRED_KINDS.get(engine, ())
    found = triggers or {}
    carry_of: dict[tuple[str, str], str] = {}
    carried: list[StepRoutine] = []
    others: list[StepRoutine] = []
    for row in migration.routines:
        key = (row.kind, row.routine_id)
        reasons: list[str] = []
        if migration.target_engine != engine:
            reasons = [ENGINE_DIFFERS]
        elif row.status not in ("as_is", "rewritten"):
            reasons = list(row.reasons) or [NOT_CARRIABLE]
        else:
            named = {d.name.lower() for d in row.dependencies if d.kind == "table"}
            own = _trigger_table(row)
            if own is not None:
                named.add(own)
            reasons += [f"{VIEW_DEPENDENCY}:{v}" for v in sorted(named & view_names)]
            if named - tables - view_names:
                reasons.append(TABLE_NOT_IN_STEP)
            if any(
                carry_of.get((ref.kind, ref.routine_id)) != "carried"
                for d in row.dependencies
                for ref in d.routines
                if (ref.kind, ref.routine_id) != key
            ):
                reasons.append(NOT_CARRIED_DEPENDENCY)
        extra = _trigger_fields(row, engine, found)
        if reasons:
            carry_of[key] = "not_carried"
            others.append(_row(row, "not_carried", reasons=reasons, **extra))
        elif row.kind in deferred_kinds:
            carry_of[key] = "deferred_to_cutover"
            others.append(_row(row, "deferred_to_cutover", enabled_as="absent", **extra))
        else:
            carry_of[key] = "carried"
            disabled = engine == "aurora_postgresql" and row.kind == "trigger"
            carried.append(
                _row(row, "carried", order=len(carried) + 1, migration_status=row.status,
                     enabled_as="disabled" if disabled else "enabled", **extra)  # fmt: skip
            )
    others.sort(key=lambda r: (r.kind, r.routine_id))
    return StepRoutines(
        source="routine_migration",
        routine_migration_version=version,
        total=len(carried) + len(others),
        carried=len(carried),
        not_carried=sum(1 for r in others if r.carry == "not_carried"),
        deferred=sum(1 for r in others if r.carry == "deferred_to_cutover"),
        routines=[*carried, *others],
    )


def _collection_schema(store: Any, database_name: str, job_id: str) -> dict[str, Any]:
    key = f"{database_name}/{job_id}/collector/output.json"
    if store.exists(key) is not True:
        return {}
    return (store.read_json(key) or {}).get("database_schema") or {}


def _collection_triggers(schema: dict[str, Any], engine: str) -> dict[TriggerKey, dict[str, Any]]:
    """The collection's DML triggers by :func:`_trigger_key` (the first of a name wins)."""
    out: dict[TriggerKey, dict[str, Any]] = {}
    for trigger in schema.get("triggers") or ():
        if isinstance(trigger, dict) and trigger.get("trigger_name"):
            key = _trigger_key(trigger.get("schema_name"), str(trigger.get("table_id") or ""),
                               str(trigger["trigger_name"]), engine)  # fmt: skip
            out.setdefault(key, trigger)
    return out


def _collection_views(schema: dict[str, Any]) -> set[str]:
    """The collection's views, as lower-case ``schema.name`` (and their view ids)."""
    out: set[str] = set()
    for view in schema.get("views") or ():
        if not isinstance(view, dict):
            continue
        name, owner = view.get("view_name"), view.get("schema_name")
        if name:
            out.add(f"{owner}.{name}".lower() if owner else str(name).lower())
        if view.get("view_id"):
            out.add(str(view["view_id"]).lower())
    return out


def attach_step_routines(
    store: Any, database_name: str, job_id: str, contract: StepContract
) -> StepContract:
    """``contract`` with its routines section, for a step 1 that moves tables into Aurora
    PostgreSQL or Aurora MySQL; any other contract is returned as it is. A job with no
    routine-migration artifact at all gets it written for the contract's version first."""
    if contract.step != 1 or contract.target.engine not in DEFERRED_KINDS or not contract.rules:
        return contract
    from src.tools.routines.migration import latest_routine_migration

    found = latest_routine_migration(store, database_name, job_id, contract.assignment_version)
    if found is None:
        from src.tools.routines.migration import write_routine_migration

        _, written = write_routine_migration(
            store, database_name, job_id, contract.assignment_version
        )
        found = None if written is None else (contract.assignment_version, written)
    schema = _collection_schema(store, database_name, job_id)
    section = build_step_routines(
        None if found is None else found[1],
        contract,
        version=None if found is None else found[0],
        views=_collection_views(schema),
        triggers=_collection_triggers(schema, contract.target.engine),
    )
    # The section is validated on its own; nothing else of the contract changes.
    return contract.model_copy(update={"routines": section})
