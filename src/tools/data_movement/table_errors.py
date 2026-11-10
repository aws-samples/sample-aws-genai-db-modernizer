# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's table errors and validation failures, while Prove polls (#2064).

DMS reports a table that failed to load (``TableState`` ``Table error`` or ``Table does not
exist``) and a table whose rows did not validate (``ValidationFailedRecords``,
``ValidationSuspendedRecords``) in its table statistics, long before the gate runs. Errors are
read from the table states only: ``FullLoadErrorRows`` and ``FullLoadCondtnlChkFailedRows``
are "valid only for migrations where DynamoDB is the target" (DMS API Reference,
TableStatistics), so on Aurora they say nothing. The task's own ``TablesErrored`` is reported
(``task_tables_errored``) but never ends the wait by itself: a stale counter with no table
named would end every later run. ``Table cancelled`` can show for a moment on a running task
(a stop and resume mid-load), so it ends the wait only when two polls in a row see it
(``CancelWatch``). ``table_problems`` names those tables with their counts (never a row's
values) so Prove can stop at once on an errored table instead of polling a load that cannot
finish, and point the customer at DMS's validation failure table on Aurora with read-only SQL
they run themselves (``failures_check``): the modernizer never connects to their databases.

Two writes may follow, each only after the customer's approval:

- ``reload_plan``: a per-table ``data-reload`` (DMS ``ReloadTables``) runs only when DMS's
  statistics show that no errored table was ever written to (``never_wrote``, the rule
  ``retry`` uses for the whole task, #2061). With ``TargetTablePrepMode: DO_NOTHING`` a reload
  into a table that already holds rows appends duplicates or hits the same key errors again, so
  any other case gives the customer's own steps instead (``table_steps``: empty the tables on
  Aurora, then reload them with their own credentials).
- ``revalidate_tables``: a ``validate-only`` reload of the tables with failed or suspended
  records; it reloads no data, so it is safe whenever the task runs with validation on.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Mapping
from typing import Any

TABLES_SHOWN = 10  # tables named on a line; the line counts the rest
RELOAD_BATCH = 10  # tables per ReloadTables call, as the DMS console revalidates at most 10
# DescribeTableStatistics' TableState values that mean the table did not load (case-folded).
TABLE_FAILED = frozenset({"table error", "table does not exist"})
CANCELLED = "table cancelled"  # a failure only when two polls in a row see it
# ValidationState values that mean records did not validate (case-folded).
VALIDATION_FAILED = frozenset({"mismatched records", "suspended records", "table error", "error"})
# Every per-table counter that says DMS wrote something to the target (run_dms_step's list).
WRITE_COUNTERS = ("FullLoadRows", "FullLoadErrorRows", "FullLoadCondtnlChkFailedRows", "Inserts",
                  "Updates", "Deletes", "Ddls", "AppliedInserts", "AppliedUpdates",
                  "AppliedDeletes", "AppliedDdls")  # fmt: skip
CAUSES = ("rows already on Aurora from an earlier attempt (the task never empties a table), "
          "duplicate keys, zero dates or the target's sql_mode, a type or character set "
          "conversion")  # fmt: skip


def table_name(row: Mapping[str, Any]) -> str:
    return f"{row.get('SchemaName')}.{row.get('TableName')}"


def _count(row: Mapping[str, Any], key: str) -> int:
    return int(row.get(key) or 0)


def _state(row: Mapping[str, Any]) -> str:
    return str(row.get("TableState") or "").casefold()


def errored(row: Mapping[str, Any]) -> bool:
    """The table did not load: an error state, or cancelled."""
    return _state(row) in TABLE_FAILED or _state(row) == CANCELLED


def _failure(row: Mapping[str, Any]) -> dict[str, Any]:
    """One table's validation failure, in the one shape every line uses."""
    return {"table": table_name(row), "validation_state": row.get("ValidationState"),
            "failed_records": _count(row, "ValidationFailedRecords"),
            "suspended_records": _count(row, "ValidationSuspendedRecords")}  # fmt: skip


def validation_failed(row: Mapping[str, Any]) -> bool:
    """Records of the table failed or were suspended in validation."""
    state = str(row.get("ValidationState") or "").casefold()
    return bool(
        _count(row, "ValidationFailedRecords") + _count(row, "ValidationSuspendedRecords")
    ) or (state in VALIDATION_FAILED)


def table_problems(
    task: Mapping[str, Any], rows: list[dict[str, Any]], *, shown: int = TABLES_SHOWN
) -> dict[str, Any]:
    """The tables that did not load and the tables with validation failures, names and counts
    only, at most ``shown`` of each (``*_not_shown`` counts the rest). ``tables_errored``
    counts the tables in an error state (``Table error``, ``Table does not exist``),
    ``tables_cancelled`` those ``Table cancelled``, and ``task_tables_errored`` is the task's
    own ``TablesErrored``, reported only."""
    bad = sorted((r for r in rows if errored(r)), key=table_name)
    failed = sorted((r for r in rows if validation_failed(r)), key=table_name)
    return {
        "tables_errored": sum(1 for r in bad if _state(r) in TABLE_FAILED),
        "tables_cancelled": sum(1 for r in bad if _state(r) == CANCELLED),
        "task_tables_errored": _count(task.get("ReplicationTaskStats") or {}, "TablesErrored"),
        "errored_tables": [{"table": table_name(r), "table_state": r.get("TableState")}
                           for r in bad[:shown]],
        "errored_tables_not_shown": max(len(bad) - shown, 0),
        "cancelled": sorted(table_name(r) for r in bad if _state(r) == CANCELLED),
        "validation_failures": [_failure(r) for r in failed[:shown]],
        "validation_failures_not_shown": max(len(failed) - shown, 0),
    }  # fmt: skip


class CancelWatch:
    """``Table cancelled`` ends the wait only when two polls in a row see the same tables."""

    def __init__(self) -> None:
        self.last: list[str] = []

    def judge(self, current: dict[str, Any]) -> dict[str, Any]:
        cancelled = list((current.get("table_problems") or {}).get("cancelled") or [])
        seen, self.last = self.last, cancelled
        if not cancelled or cancelled != seen or current.get("phase") == "table_errors":
            return current
        return {**current, "phase": "table_errors",
                "table_errors_reason": f"{len(cancelled)} tables Table cancelled on two polls in a row"}  # fmt: skip


def never_wrote(row: Mapping[str, Any]) -> list[str]:
    """Why DMS may have written to this table on Aurora; empty only when its statistics show it
    never did: every write counter 0, no full load start time and no reload."""
    name = table_name(row)
    why = [f"{name} {k} is {row[k]}" for k in WRITE_COUNTERS if row.get(k)]
    if row.get("FullLoadStartTime") is not None or row.get("FullLoadReloaded"):
        why.append(f"{name} was loaded")
    return why


def reload_plan(
    task: Mapping[str, Any], rows: list[dict[str, Any]]
) -> tuple[str | None, list[str], list[tuple[str, str]]]:
    """(plan, why, tables) for the tables that did not load. ``reload-tables``: no such table
    shows a write. ``steps``: the customer empties and reloads them. None: no table is errored
    by name (``why`` says so)."""
    bad = sorted((r for r in rows if errored(r)), key=table_name)
    tables = [(str(r.get("SchemaName")), str(r.get("TableName"))) for r in bad]
    if not bad:
        counted = _count(task.get("ReplicationTaskStats") or {}, "TablesErrored")
        return None, [f"the task counts {counted} errored tables but its table statistics "
                      "name none" if counted else "no table is errored"], []  # fmt: skip
    why = [w for r in bad for w in never_wrote(r)]
    return ("steps" if why else "reload-tables"), why, tables


def revalidate_tables(rows: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """The tables a ``validate-only`` reload covers: those with failed or suspended records."""
    return [(str(r.get("SchemaName")), str(r.get("TableName")))
            for r in sorted(rows, key=table_name) if validation_failed(r) and not errored(r)]  # fmt: skip


class PartialReload(Exception):
    """A later ``ReloadTables`` call failed after earlier ones went through."""


def reload(dms: Any, task_arn: str, tables: list[tuple[str, str]], option: str) -> int:
    """``ReloadTables`` with ``option``, ``RELOAD_BATCH`` tables a call; the calls made. When a
    later call fails, ``PartialReload`` names the tables the earlier calls already sent."""
    calls = 0
    for i in range(0, len(tables), RELOAD_BATCH):
        try:
            dms.reload_tables(ReplicationTaskArn=task_arn, ReloadOption=option,
                              TablesToReload=[{"SchemaName": s, "TableName": t}
                                              for s, t in tables[i : i + RELOAD_BATCH]])  # fmt: skip
        except Exception as exc:
            if not i:
                raise
            done = ", ".join(f"{s}.{t}" for s, t in tables[:i])
            rest = ", ".join(f"{s}.{t}" for s, t in tables[i:])
            raise PartialReload(f"ReloadTables ({option}) failed after {calls} of its calls "
                                f"({type(exc).__name__}: {exc}): DMS already took {done}; "
                                f"not sent: {rest}") from exc  # fmt: skip
        calls += 1
    return calls


def _task_name(task_arn: str) -> str:
    """The task's external resource id as an SQL literal: DMS ids are letters and digits, so
    anything else is refused rather than quoted."""
    resource = task_arn.rsplit(":", 1)[-1]
    if not re.fullmatch(r"[A-Za-z0-9-]{1,255}", resource):
        raise ValueError(f"not a DMS task ARN: {task_arn!r}")
    return f"'{resource}'"


V2_FROM = (3, 6, 1)  # DMS creates awsdms_validation_failures_v2 on PostgreSQL from 3.6.1
_TABLES_QUERY = ("SELECT table_schema, table_name FROM information_schema.tables WHERE "
                 "table_name LIKE 'awsdms\\_validation\\_failures\\_v%';")  # fmt: skip


def _version(text: str | None) -> tuple[int, ...] | None:
    if not text or not re.fullmatch(r"\d+(\.\d+)*", text):
        return None
    return tuple(int(x) for x in text.split("."))


def failures_table(family: str | None, dms_version: str | None) -> str:
    """Where DMS lists the records that did not validate, in the default control schema (the
    task settings set no ``ControlSchema``): on PostgreSQL ``public``, ``_v2`` from DMS 3.6.1
    and ``_v1`` before; on MySQL ``awsdms_control.awsdms_validation_failures_v1`` (AWS DMS User
    Guide, AWS DMS data validation and Control table task settings)."""
    version = _version(dms_version)
    pg = ("public.awsdms_validation_failures_v1" if version is not None and version < V2_FROM
          else "public.awsdms_validation_failures_v2")  # fmt: skip
    return {"postgresql": pg, "mysql": "awsdms_control.awsdms_validation_failures_v1"}.get(
        family or "", f"{pg} (Aurora PostgreSQL) or awsdms_control.awsdms_validation_failures_v1 "
                      "(Aurora MySQL)")  # fmt: skip


def failures_check(
    family: str | None, task_arn: str, dms_version: str | None = None
) -> dict[str, Any]:
    """Where DMS lists the records that did not validate, with read-only SQL the customer runs
    on Aurora (the target), never the modernizer: a lookup of the table DMS made first, then a
    count per table. ``TASK_NAME`` holds the task's external resource id, the ARN's last
    segment (AWS DMS User Guide, AWS DMS data validation)."""
    task = _task_name(task_arn)  # allowlisted: letters, digits and hyphens only
    steps = ["On the Aurora cluster (the target), in a read-only session, run the first query "
             "(it names the failure table DMS made; when it names another one than the next "
             "query reads, use that name), then the next:"]  # fmt: skip
    if family != "mysql":
        pg = failures_table("postgresql", dms_version)
        steps += ["On Aurora PostgreSQL:", _TABLES_QUERY,
                  f'SELECT "TABLE_OWNER", "TABLE_NAME", "FAILURE_TYPE", COUNT(*) AS failures FROM {pg} '  # nosec B608 -- read-only SQL for the customer; the task id is allowlisted
                  f'WHERE "TASK_NAME" = {task} GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;']  # fmt: skip
    if family != "postgresql":
        steps += ["On Aurora MySQL:", _TABLES_QUERY,
                  "SELECT TABLE_OWNER, TABLE_NAME, FAILURE_TYPE, COUNT(*) AS failures FROM "  # nosec B608 -- read-only SQL for the customer; the task id is allowlisted
                  f"awsdms_control.awsdms_validation_failures_v1 WHERE TASK_NAME = {task} "
                  "GROUP BY 1, 2, 3 ORDER BY 1, 2, 3;"]  # fmt: skip
    steps.append("Each row's KEY and DETAILS columns hold the key and the column values that "
                 "differ: read them on your side; never paste them into this session.")  # fmt: skip
    return {"failures_table": failures_table(family, dms_version), "check_on_target": steps}


def lob_hints(lob_tables: list[Any], failed: list[str]) -> list[str]:
    """A hint per failed table that has LOB columns: in limited LOB mode DMS moves each LOB only
    up to LobMaxSize, so a larger value differs on Aurora."""
    by_table = {t.table: t for t in lob_tables}
    hints = []
    for name in failed:
        t = by_table.get(name)
        if t is None:
            continue
        cols = ", ".join(t.columns) or "its LOB columns"
        limit = (f"the task moves LOBs only up to LobMaxSize ({t.max_size_kb} KB, limited LOB "
                 "mode)" if t.mode == "limited" else "the task runs full LOB mode")  # fmt: skip
        hints.append(f"{name} has LOB columns ({cols}): {limit}; compare the largest value of "
                     "each with that size on the source (a larger one is cut on Aurora) before "
                     "you trust or revalidate it")  # fmt: skip
    return hints


def table_steps(
    task_arn: str, region: str, tables: list[tuple[str, str]], truncate: list[str]
) -> list[str]:
    """The customer's own steps when a per-table reload is not provably safe: fix the cause,
    empty the errored tables on Aurora (``truncate``: SQL only), then reload them. Never run
    here."""
    args = " ".join(shlex.quote(f"SchemaName={s},TableName={t}") for s, t in tables)
    return [
        "Fix what made the tables fail first (the task's CloudWatch log names each table's "
        f"error; the usual causes: {CAUSES}): a reload before the fix fails the same way.",
        "On the Aurora cluster (the target), never on the source, as a user that may truncate "
        "these tables, empty them:",
        *truncate,
        "Then reload only these tables, with your own credentials: aws dms reload-tables "
        f"--replication-task-arn {task_arn} --tables-to-reload {args} --reload-option "
        f"data-reload --region {region}",
        "Then run /move step-1 again: it goes on at Prove.",
    ]


def gate_failures(gate: Any, plan: Any | None) -> dict[str, Any]:
    """For the gate's line (#2064), in the shape status's lines use: ``validation_failures``
    (the tables whose records did not validate, at most ``TABLES_SHOWN``),
    ``validation_failures_not_shown`` and ``validation_failures_check`` (where DMS lists those
    records, the read-only SQL for the customer, and a hint per known pattern), None when none
    failed. ``plan`` (the step plan, or None when it cannot be read) gives the engine, the DMS
    engine version and the LOB tables."""
    from src.tools.data_movement.source_retention import family_of

    failed = [t for t in gate.tables if t.validation_failed_records
              or t.validation_suspended_records
              or str(t.validation_state or "").casefold() in VALIDATION_FAILED]  # fmt: skip
    if not failed:
        return {"validation_failures": [], "validation_failures_not_shown": 0,
                "validation_failures_check": None}  # fmt: skip
    family = None if plan is None else family_of(plan.source.engine)
    replication = None if plan is None else plan.replication
    lob = [] if replication is None else list(replication.lob_tables)
    rows = [{"SchemaName": t.table.rpartition(".")[0], "TableName": t.table.rpartition(".")[2],
             "ValidationState": t.validation_state,
             "ValidationFailedRecords": t.validation_failed_records,
             "ValidationSuspendedRecords": t.validation_suspended_records} for t in failed]  # fmt: skip
    check = failures_check(family, gate.task_arn, dms_version(plan))
    return {"validation_failures": [_failure(r) for r in rows[:TABLES_SHOWN]],
            "validation_failures_not_shown": max(len(failed) - TABLES_SHOWN, 0),
            "validation_failures_check": {**check, "hints": lob_hints(lob, [t.table for t in failed])}}  # fmt: skip


def dms_version(plan: Any | None) -> str | None:
    """The step plan's DMS engine version (the catalog's for a plan without one), or None."""
    if plan is None or plan.replication is None:
        return None
    from src.tools.data_movement.dms_step import engine_rules

    return str(engine_rules(plan.replication).version)


def empty_target_check(family: str | None, tables: list[str]) -> dict[str, Any]:
    """Before the task starts (#2064): the customer confirms every step 1 table is empty on
    Aurora, with a read-only query that names any table holding rows. The task never empties a
    table (``TargetTablePrepMode: DO_NOTHING``), and DMS's statistics restart with each
    reload, so nothing here can see rows an earlier attempt left. SQL only; never run here."""
    quote = '"' if family == "postgresql" else "`"
    dual = "" if family == "postgresql" else " FROM DUAL"

    def ident(part: str) -> str:
        return f"{quote}{part.replace(quote, quote * 2)}{quote}"

    def literal(text: str) -> str:
        return "'" + text.replace("'", "''").replace("\\", "\\\\") + "'"

    parts = [f"SELECT {literal(t)} AS non_empty_table{dual} WHERE EXISTS (SELECT 1 FROM "  # nosec B608 -- read-only SQL for the customer; identifiers quoted, names escaped
             f"{ident(t.split('.', 1)[0])}.{ident(t.split('.', 1)[1])})" for t in tables]  # fmt: skip
    engine = {"postgresql": "Aurora PostgreSQL", "mysql": "Aurora MySQL"}.get(
        family or "", "Aurora"
    )
    return {"question": "Is every table of the step empty on Aurora? The task never empties a "
                        "table: rows from an earlier attempt make the full load fail on duplicate "
                        "keys or load duplicate rows.",
            "check_on_target": [f"On {engine} (the target), in a read-only session, run (it "
                                "names every table that holds rows; no row back means all are "
                                "empty):", " UNION ALL ".join(parts) + ";"] if parts else []}  # fmt: skip


def step_plan(root: str, db: str, job_id: str) -> Any | None:
    """The step 1 plan, or None when it cannot be read."""
    import json
    from pathlib import Path

    from src.contracts.data_movement_plan import DataMovementPlan
    from src.shared.contract_versioning import load_contract

    path = Path(root) / db / job_id / "data-movement" / "step-1" / "plan.json"
    try:
        return load_contract(DataMovementPlan, json.loads(path.read_text()), artifact="step plan")
    except (OSError, ValueError):
        return None
