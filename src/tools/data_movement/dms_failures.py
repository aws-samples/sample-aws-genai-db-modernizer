# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Known step 1 task failures, each with a fix hint (#2061).

When DMS stops step 1's task with an error, ``scripts/run_dms_step.py`` puts the hint of the
first entry of ``FAILURE_HINTS`` whose pattern matches the task's ``LastFailureMessage`` or
``StopReason`` on the line, as ``hint``. A message no entry matches gets no hint (``None``):
never a near-miss guess. Each entry names where the failure and its fix are described: the AWS
DMS User Guide or the step 1 rehearsals, and the fix is always the customer's to make (the
modernizer never connects to their databases).

Live DMS often puts only ``Stream Component Fatal error ... FATAL_ERROR`` in those fields, and
the error it hit (a permission denial on the control schema, a missing source grant) only in
the task's CloudWatch Logs log group (#2364). So for a failed task no hint matched, Prove also
matches the catalog against the task log's recent error events (``log_hint``): read-only,
bounded to ``LOG_WINDOW_MINUTES`` before the task stopped and to ``LOG_EVENTS`` events, and only
the matched entry's own fixed text ever leaves this module, never a log line.

Only a task that failed now gets a hint: DMS keeps the last failure message after a task is
resumed, so a running task, or one stopped normally, gets none. A LOB larger than the task's
limit has no entry: on the step 1 rehearsals ``FailTasksOnLobTruncation`` did not stop a live
full load (the data-movement README), so no failure message can be relied on for it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from src.tools.data_movement.dms_control import CONTROL_SCHEMA, CONTROL_TABLE_PATTERN
from src.tools.database.mysql_grants import grant_database

FAILED = frozenset({"failed", "failed-move"})
# StopReason values that mean the task stopped by itself on a problem (DMS API Reference,
# ReplicationTask.StopReason), not because someone stopped it or it reached a stop point.
ERROR_STOPS = ("RECOVERABLE_ERROR", "FATAL_ERROR", "STOPPED_DUE_TO_LOW_MEMORY",
               "STOPPED_DUE_TO_LOW_DISK")  # fmt: skip


@dataclass(frozen=True)
class FailureHint:
    id: str
    pattern: re.Pattern[str]
    hint: str
    source: str


def _hint(hint_id: str, pattern: str, hint: str, source: str) -> FailureHint:
    return FailureHint(hint_id, re.compile(pattern, re.IGNORECASE | re.DOTALL), hint, source)


FAILURE_HINTS: tuple[FailureHint, ...] = (
    _hint("mysql_replication_grants", r"\bREPLICATION\s+(SLAVE|CLIENT)\b",
          "The source user lacks a replication privilege: on the source, grant it REPLICATION "
          "CLIENT and REPLICATION SLAVE (the README's \"Before you deploy the DMS stack\"), then "
          "retry.",
          "AWS DMS User Guide, Using any MySQL-compatible database as a source (prerequisites)"),
    _hint("postgresql_replication_role", r"\brds_replication\b",
          "The source user lacks the replication role: on the source, GRANT rds_replication TO "
          "the user in the source secret (a self-managed source: give it the REPLICATION "
          "attribute), then retry.",
          "AWS DMS User Guide, Using a PostgreSQL database as an AWS DMS source"),
    # Only a permission error that itself names the heartbeat's table: a customer table named
    # like it, or a denial on another object in the same message, never matches.
    _hint("postgresql_heartbeat_create",
          r"\bpermission denied for (?:schema|table|relation)\s+(?:\S+\.)?\"?awsdms_heartbeat\b(?!_)",
          "The DMS heartbeat cannot create or write its table (awsdms_heartbeat): on PostgreSQL "
          "15 and later the source user needs CREATE on the heartbeat schema (public by "
          "default), and write access to the table once it exists. On the source, GRANT CREATE "
          "ON SCHEMA public TO the user in the source secret, then retry.",
          "AWS DMS User Guide, PostgreSQL source endpoint settings (HeartbeatSchema); "
          "step 1 rehearsal (awsdms_heartbeat)"),
    # The control schema step 1's DDL creates (#2133), owned by whoever ran it: a DMS user made
    # from an earlier step-1.ddl.sql has no CREATE there. Only a denial on that schema itself.
    _hint("postgresql_control_schema",
          rf"\bpermission denied for schema\s+\"?{re.escape(CONTROL_SCHEMA)}\b\"?(?![\w])",
          f"DMS cannot create its control tables in {CONTROL_SCHEMA} (the task's ControlSchema): "
          "step-1.ddl.sql creates that schema and the user who ran it owns it, so a DMS user "
          "created from an earlier step-1.ddl.sql has no CREATE there. On Aurora, as that user, "
          f'run GRANT USAGE, CREATE ON SCHEMA "{CONTROL_SCHEMA}" TO <the user in the target '
          "secret>; then retry.",
          "AWS DMS User Guide, Using a PostgreSQL database as a target (security requirements); "
          "Control table task settings (ControlSchema)"),
    # MySQL's own text (8.0 and 8.4): a CREATE on a control table names it unqualified
    # (ERROR 1142, "CREATE command denied to user ... for table 'awsdms_apply_exceptions'"),
    # a USE names the database (ERROR 1044, "... to database '<it>'"). Only DMS's
    # control table names, unqualified or in the control database.
    _hint("mysql_control_database",
          rf"\bdenied (?:for|to) user \S+ (?:to database '{re.escape(CONTROL_SCHEMA)}'"
          rf"|for table '(?:{re.escape(CONTROL_SCHEMA)}\.)?{CONTROL_TABLE_PATTERN}')",
          f"DMS cannot create or write its control tables in {CONTROL_SCHEMA} (the task's "
          "ControlSchema): on Aurora, as the administrator, run GRANT ALL PRIVILEGES ON "
          f"{grant_database(CONTROL_SCHEMA)}.* TO '<the user in the target secret>'@'%'; "
          "then retry.",
          "AWS DMS User Guide, Using a MySQL-compatible database as a target (prerequisites); "
          "Control table task settings (ControlSchema)"),
    # Binary logging said to be off, never a message that merely names a binlog variable.
    _hint("mysql_binary_logging",
          r"\bbinary\s+logging\s+(?:is\s+)?(?:disabled|not\s+enabled|must\s+be\s+enabled)\b",
          "Change capture is not on at the source: turn on binary logging (RDS for MySQL: "
          "automated backups with a retention above zero; Aurora MySQL: binlog_format in the "
          "cluster parameter group), with binlog_format=ROW and binlog_row_image=FULL, then "
          "retry.",
          "AWS DMS User Guide, Troubleshooting: CDC task failing because binary logging is "
          "disabled; Using an AWS-managed MySQL-compatible database as a source"),
    # DMS's own load command on the replication instance, never a source database's error.
    _hint("out_of_memory",
          r"\bFailed to run load command\b[^;]*\bCannot allocate memory\b"
          r"|\bSTOPPED_DUE_TO_LOW_MEMORY\b",
          "The replication instance ran out of memory: set a larger instance_class in the "
          "inputs file, create and execute the change set again (Move), then retry.",
          "step 1 rehearsal (Cannot allocate memory); AWS DMS API Reference, ReplicationTask "
          "StopReason STOPPED_DUE_TO_LOW_MEMORY"),
    _hint("low_disk", r"\bSTOPPED_DUE_TO_LOW_DISK\b",
          "The replication instance ran out of storage: set a larger allocated_storage in the "
          "inputs file, create and execute the change set again (Move), then retry.",
          "AWS DMS API Reference, ReplicationTask StopReason STOPPED_DUE_TO_LOW_DISK"),
)  # fmt: skip


def match_failure(message: str | None) -> FailureHint | None:
    """The first catalog entry whose pattern matches ``message``; None for any other."""
    if not message:
        return None
    return next((h for h in FAILURE_HINTS if h.pattern.search(message)), None)


def failed_now(task: dict[str, Any]) -> bool:
    """DMS reports the task failed, or stopped by itself on a problem (``ERROR_STOPS``)."""
    state = task.get("Status")
    reason = str(task.get("StopReason") or "").upper()
    return state in FAILED or (state == "stopped" and any(s in reason for s in ERROR_STOPS))


def failure_hint(task: dict[str, Any] | None) -> str | None:
    """The fix hint for a task that failed now; None for any other task or an unknown failure."""
    if task is None or not failed_now(task):
        return None
    for text in (task.get("LastFailureMessage"), task.get("StopReason")):
        found = match_failure(text)
        if found is not None:
            return found.hint
    return None


# The task log Prove reads for a failed task no hint matched (#2364). DMS writes a task's log to
# the log group ``dms-tasks-<replication instance identifier>`` (AWS DMS User Guide, Logging
# task settings); step 1's stack makes its own instance, so that group holds this task only,
# but every run of it: the window starts no earlier than the run that failed.
LOG_GROUP_PREFIX = "dms-tasks-"
LOG_WINDOW_MINUTES = 15  # read back at most this far from when the task stopped
LOG_AFTER_SECONDS = 60  # and this far past it (DMS flushes its last lines after the stop)
LOG_EVENTS = 500  # match at most this many events in all, the newest first
LOG_PAGE = 100  # events asked for per FilterLogEvents call
LOG_CALLS = 10  # FilterLogEvents calls at most
LOG_SLICE_SECONDS = 300  # read the window newest slice first, so the cap keeps the newest events
# DMS's error lines only, as it tags them: "[TARGET_APPLY    ]E:  ...".
LOG_ERRORS = '"]E:"'


def task_log_group(instance_identifier: str) -> str:
    """The CloudWatch Logs log group DMS writes the task's log to."""
    return f"{LOG_GROUP_PREFIX}{instance_identifier}"


def _ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def log_window(stats: dict[str, Any], now: datetime) -> tuple[int, int]:
    """(start, end) in epoch milliseconds of the run that failed: from the later of its
    ``StartDate`` (DMS's last start, fresh or resume) and ``LOG_WINDOW_MINUTES`` before its
    ``StopDate``, to ``LOG_AFTER_SECONDS`` after the stop; an earlier run's errors are never
    read. Without a stop date, the minutes before ``now``."""

    def aware(when: Any) -> datetime | None:
        if not isinstance(when, datetime):
            return None
        return when if when.tzinfo else when.replace(tzinfo=now.tzinfo)

    stop, started = aware(stats.get("StopDate")), aware(stats.get("StartDate"))
    end = now if stop is None else stop + timedelta(seconds=LOG_AFTER_SECONDS)
    start = (now if stop is None else stop) - timedelta(minutes=LOG_WINDOW_MINUTES)
    if started is not None and started > start:
        start = started
    return _ms(start), max(_ms(end), _ms(start))


def log_hint(logs: Any, log_group: str, start_ms: int, end_ms: int) -> FailureHint | None:
    """The hint of the newest error event in [``start_ms``, ``end_ms``] that a ``FAILURE_HINTS``
    entry matches (the first entry, for that event); None for none. Read-only
    (``FilterLogEvents``), newest ``LOG_SLICE_SECONDS`` slice first, at most ``LOG_EVENTS``
    events over at most ``LOG_CALLS`` calls, so the cap keeps the events nearest the stop. The
    messages are matched and dropped here, and only the entry (its own fixed text) is returned.
    AWS errors propagate to the caller."""
    calls, read = 0, 0
    slice_end = end_ms
    while slice_end > start_ms and calls < LOG_CALLS and read < LOG_EVENTS:
        slice_start = max(start_ms, slice_end - LOG_SLICE_SECONDS * 1000)
        kwargs: dict[str, Any] = {"logGroupName": log_group, "startTime": slice_start,
                                  "endTime": slice_end, "filterPattern": LOG_ERRORS,
                                  "limit": LOG_PAGE}  # fmt: skip
        events: list[dict[str, Any]] = []
        while calls < LOG_CALLS:
            page = logs.filter_log_events(**kwargs)
            calls += 1
            events += page.get("events") or []
            if not page.get("nextToken"):
                break
            kwargs["nextToken"] = page["nextToken"]
        newest = sorted(events, key=lambda e: int(e.get("timestamp") or 0), reverse=True)
        for event in newest[: LOG_EVENTS - read]:
            message = str(event.get("message") or "")
            found = next((h for h in FAILURE_HINTS if h.pattern.search(message)), None)
            if found is not None:
                return found
        read += min(len(newest), LOG_EVENTS - read)
        slice_end = slice_start
    return None
