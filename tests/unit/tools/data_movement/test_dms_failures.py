# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Known DMS task failures and the fix hint each carries (#2061).

A hint is given only for a failure the catalog describes; any other message gets none, never a
near-miss hint."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from src.tools.data_movement import dms_failures as df

OOM = "Failed to run load command. Cannot allocate memory"
KNOWN = [
    ("mysql_replication_grants",
     "Error 1227 (42000): Access denied; you need (at least one of) the REPLICATION SLAVE "
     "privilege(s) for this operation", "REPLICATION SLAVE"),
    ("mysql_replication_grants",
     "Access denied; you need the replication client privilege", "REPLICATION CLIENT"),
    ("postgresql_replication_role",
     "permission denied to create replication slot: the user is not a member of rds_replication",
     "rds_replication"),
    ("postgresql_heartbeat_create",
     "ERROR: permission denied for table public.awsdms_heartbeat", "CREATE ON SCHEMA"),
    ("postgresql_heartbeat_create",
     "permission denied for relation awsdms_heartbeat", "write access"),
    ("mysql_binary_logging",
     "Binary logging must be enabled for MySQL server", "automated backups"),
    ("mysql_binary_logging",
     "CDC task failing: binary logging disabled on the source", "binlog_row_image=FULL"),
    ("out_of_memory",
     "Failed to run load command. Cannot allocate memory", "instance_class"),
    ("out_of_memory", "Stop Reason STOPPED_DUE_TO_LOW_MEMORY", "instance_class"),
    ("low_disk", "Stop Reason STOPPED_DUE_TO_LOW_DISK", "allocated_storage"),
    # #2133: the control schema step 1's DDL creates, which the administrator owns.
    ("postgresql_control_schema",
     "ERROR:  permission denied for schema awsdms_control", 'ON SCHEMA "awsdms_control"'),
    ("postgresql_control_schema",
     'RetCode: SQL_ERROR SqlState: 42501 NativeError: 1 Message: ERROR: permission denied for '
     'schema "awsdms_control"', "USAGE, CREATE"),
    # MySQL 8.0.46 and 8.4.13's own text (tests/integration/test_dms_control_grants_mysql.py):
    # a CREATE TABLE names the table unqualified (1142), a USE names the database (1044).
    ("mysql_control_database",
     "ERROR 1142 (42000) at line 1: CREATE command denied to user 'dms_user'@'localhost' for "
     "table 'awsdms_apply_exceptions'", "GRANT ALL PRIVILEGES ON `awsdms\\_control`.*"),
    ("mysql_control_database",
     "RetCode: SQL_ERROR SqlState: 42000 NativeError: 1142 Message: [MySQL][ODBC 8.0(w) "
     "Driver][mysqld-8.0.39]CREATE command denied to user 'dms_user'@'10.0.0.1' for table "
     "'awsdms_validation_failures_v1'", "GRANT ALL PRIVILEGES ON `awsdms\\_control`.*"),
    ("mysql_control_database",
     "ERROR 1044 (42000) at line 1: Access denied for user 'dms_user'@'localhost' to database "
     "'awsdms_control'", "GRANT ALL PRIVILEGES ON `awsdms\\_control`.*"),
    ("mysql_control_database",
     "CREATE command denied to user 'dms_user'@'10.0.0.1' for table "
     "'awsdms_control.awsdms_apply_exceptions'", "GRANT ALL PRIVILEGES ON `awsdms\\_control`.*"),
]  # fmt: skip

UNKNOWN = [
    "Last Error Table error; Task error notification received",  # DMS's generic task error
    "Failed to get lob data, going to set to null",  # a LOB lookup failure, not a truncation
    "permission denied for table orders",  # a SELECT grant, not the heartbeat's CREATE
    "Could not find first log file name in binary log index file",  # purged, not turned off
    "Connection reset by peer",
    "memory usage is high",
    "REPLICATION",
    "",
    # Review counter-examples (#2074): a message that names a binlog variable or a table like
    # the heartbeat's is not the failure the hint fixes.
    "Failed to read binlog_format: Access denied for user dms",
    "Could not read @@binlog_row_image; Lost connection to MySQL server during query",
    "binlog_format is MIXED, the task needs ROW",  # no documented message: no hint
    "permission denied for table heartbeat_events",
    "permission denied for table awsdms_heartbeat_archive",
    "Access denied for user 'dms'@'10.0.0.1' (using password: YES)",
    "Lost connection to MySQL server during query",
    "LOB data was truncated for column 'body'",  # no entry: truncation does not stop the task
    # Re-review counter-examples (#2074).
    "Binary logging is enabled but account 'dms' is disabled",
    "permission denied for table orders; while writing public.awsdms_heartbeat",
    "permission denied for schema public; CREATE TABLE public.awsdms_heartbeat",  # the schema's
    "could not fork autovacuum worker process: Cannot allocate memory",  # the source's memory
    "Cannot allocate memory",
    # #2133 counter-examples: another schema or database, or the name only in passing.
    "permission denied for schema awsdms_control_archive",
    "permission denied for schema public",
    "Access denied for user 'dms_user'@'%' to database 'wordpress'",
    "Cannot create ValidationFailure table for owner awsdms_control",
    "CREATE command denied to user 'dms_user'@'localhost' for table 'wp_posts'",
    "CREATE command denied to user 'dms_source'@'localhost' for table 'awsdms_heartbeat'",
    "INSERT command denied to user 'dms_user'@'localhost' for table 'awsdms_apply_exceptions_old'",
]


@pytest.mark.parametrize(("hint_id", "message", "words"), KNOWN,
                         ids=[f"{h}-{i}" for i, (h, *_) in enumerate(KNOWN)])  # fmt: skip
def test_a_known_failure_message_carries_its_fix_hint(hint_id, message, words):
    found = df.match_failure(message)
    assert found is not None and found.id == hint_id
    assert words in found.hint


@pytest.mark.parametrize("message", UNKNOWN)
def test_an_unknown_failure_message_carries_no_hint(message):
    assert df.match_failure(message) is None


def test_every_hint_names_its_documented_source():
    ids = [h.id for h in df.FAILURE_HINTS]
    assert len(ids) == len(set(ids)) and set(ids) == {k for k, *_ in KNOWN}
    for hint in df.FAILURE_HINTS:
        assert hint.source.startswith(
            ("AWS DMS User Guide", "AWS DMS API Reference", "step 1 rehearsal")
        ), hint.id


@pytest.mark.parametrize("status", ["failed", "failed-move"])
def test_a_failed_task_gets_the_hint_of_its_failure_message(status):
    task = {"Status": status, "LastFailureMessage": OOM, "StopReason": "FATAL_ERROR"}
    assert "instance_class" in str(df.failure_hint(task))


def test_a_stop_reason_alone_can_carry_the_hint():
    task = {
        "Status": "failed",
        "StopReason": "Stop Reason FATAL_ERROR: Failed to run load command. Cannot allocate memory",
    }
    assert "instance_class" in str(df.failure_hint(task))


@pytest.mark.parametrize("status", ["running", "starting", "stopping", "ready"])
def test_a_task_that_did_not_fail_gets_no_hint_from_an_old_failure_message(status):
    """DMS keeps the last failure message after a resume: a running task has no failure now."""
    assert df.failure_hint({"Status": status, "LastFailureMessage": OOM}) is None


def test_a_task_stopped_normally_gets_no_hint():
    task = {"Status": "stopped", "StopReason": "Stop Reason NORMAL",
            "LastFailureMessage": OOM}  # fmt: skip
    assert df.failure_hint(task) is None


@pytest.mark.parametrize("reason", ["STOPPED_DUE_TO_LOW_MEMORY", "STOPPED_DUE_TO_LOW_DISK"])
def test_a_task_dms_stopped_for_low_memory_or_disk_gets_its_hint(reason):
    hint = df.failure_hint({"Status": "stopped", "StopReason": f"Stop Reason {reason}"})
    assert (
        hint is not None
        and ("instance_class" if "MEMORY" in reason else "allocated_storage") in hint
    )


def test_a_task_stopped_by_an_error_gets_its_hint():
    task = {"Status": "stopped", "StopReason": "Stop Reason FATAL_ERROR",
            "LastFailureMessage": OOM}  # fmt: skip
    assert "instance_class" in str(df.failure_hint(task))


def test_no_task_or_no_message_gets_no_hint():
    assert df.failure_hint(None) is None
    assert df.failure_hint({"Status": "failed"}) is None


# --- The task log (#2364): the hint DMS's own message never names ----------------------------

# The two denial lines of the #2135 Discourse rehearsal's task log, as DMS wrote them (only
# those lines). The task's LastFailureMessage held only DMS's generic fatal error.
PG_DENIAL_LOG = [
    "2026-11-16T15:15:39 [TARGET_APPLY    ]E:  RetCode: SQL_ERROR  SqlState: 42501 NativeError: 1 "
    "Message: ERROR: permission denied for schema awsdms_control; Error while executing the "
    "query [1022502]  (ar_odbc_stmt.c:5188)",
    "2026-11-16T15:15:40 [VALIDATOR_TARGE ]E:  RetCode: SQL_ERROR  SqlState: 42501 NativeError: 1 "
    "Message: ERROR: permission denied for schema awsdms_control; Error while executing the "
    "query [1022502]  (ar_odbc_stmt.c:5188)",
]
GENERIC_FATAL = (
    "Last Error ;\n Stream Component Fatal error. Stop Reason FATAL_ERROR Error Level FATAL"
)


class FakeLogs:
    """A CloudWatch Logs client that answers FilterLogEvents from ``pages`` and records calls."""

    def __init__(self, *pages: list[str], next_forever: bool = False) -> None:
        self.pages, self.next_forever = list(pages), next_forever
        self.calls: list[dict] = []

    def filter_log_events(self, **kwargs):
        self.calls.append(kwargs)
        i = len(self.calls) - 1
        events = [{"message": m} for m in (self.pages[i] if i < len(self.pages) else [])]
        more = self.next_forever or i + 1 < len(self.pages)
        return {"events": events, **({"nextToken": f"t{i + 1}"} if more else {})}


@pytest.mark.passes_on_main  # the message DMS gave live never named the failure (#2364)
def test_the_generic_fatal_message_alone_carries_no_hint():
    assert df.failure_hint({"Status": "failed", "LastFailureMessage": GENERIC_FATAL,
                            "StopReason": GENERIC_FATAL}) is None  # fmt: skip


def test_the_task_logs_control_schema_denial_carries_the_schema_grant():
    logs = FakeLogs(PG_DENIAL_LOG)
    found = df.log_hint(logs, "dms-tasks-ri", 1, 2)
    assert found is not None and found.id == "postgresql_control_schema"
    assert 'GRANT USAGE, CREATE ON SCHEMA "awsdms_control"' in found.hint
    # One bounded read of the error lines only, in the window, of this task's log group.
    assert logs.calls == [{"logGroupName": "dms-tasks-ri", "startTime": 1, "endTime": 2,
                           "filterPattern": df.LOG_ERRORS, "limit": df.LOG_PAGE}]  # fmt: skip


def test_the_task_logs_mysql_control_denial_carries_the_grant_database_grant():
    line = ("2026-11-16T15:15:39 [TARGET_APPLY    ]E:  RetCode: SQL_ERROR  SqlState: 42000 "
            "NativeError: 1142 Message: [MySQL][ODBC 8.0(w) Driver][mysqld-8.0.39]CREATE command "
            "denied to user 'dms_user'@'10.0.0.1' for table 'awsdms_apply_exceptions' [1022502]")  # fmt: skip
    found = df.log_hint(FakeLogs([line]), "dms-tasks-ri", 1, 2)
    assert found is not None and found.id == "mysql_control_database"
    assert "GRANT ALL PRIVILEGES ON `awsdms\\_control`.*" in found.hint


@pytest.mark.parametrize(("hint_id", "message", "words"), KNOWN,
                         ids=[f"{h}-{i}" for i, (h, *_) in enumerate(KNOWN)])  # fmt: skip
def test_every_known_failure_also_carries_its_hint_from_the_task_log(hint_id, message, words):
    """A source grant, the heartbeat, binary logging or memory: any of them can be in the log
    only, so every catalog entry is matched there too."""
    line = f"2026-11-16T15:15:39 [SOURCE_CAPTURE  ]E:  {message} [1020101]"
    found = df.log_hint(FakeLogs(["an unrelated error", line]), "dms-tasks-ri", 1, 2)
    assert found is not None and found.id == hint_id and words in found.hint


@pytest.mark.parametrize("message", UNKNOWN)
def test_a_log_with_no_known_failure_carries_no_hint(message):
    assert df.log_hint(FakeLogs([message, "Stream component failed at subtask 0"]),
                       "dms-tasks-ri", 1, 2) is None  # fmt: skip


def test_the_log_read_is_bounded_in_calls_and_events():
    endless = FakeLogs(*([[]] * 20), next_forever=True)  # empty pages, a next token forever
    assert df.log_hint(endless, "dms-tasks-ri", 1, 2) is None
    assert len(endless.calls) == df.LOG_PAGES
    assert all(c["limit"] == df.LOG_PAGE for c in endless.calls)
    assert [c.get("nextToken") for c in endless.calls] == [None, "t1", "t2", "t3", "t4"]
    filler = ["a line DMS logged"] * df.LOG_PAGE
    late = FakeLogs(*([filler] * (df.LOG_EVENTS // df.LOG_PAGE)), PG_DENIAL_LOG)
    assert df.log_hint(late, "dms-tasks-ri", 1, 2) is None  # past LOG_EVENTS: never read
    assert len(late.calls) == df.LOG_EVENTS // df.LOG_PAGE


def test_the_log_window_ends_just_past_the_tasks_stop():
    stop = datetime(2026, 11, 16, 15, 15, 40, tzinfo=UTC)
    start, end = df.log_window({"StopDate": stop}, datetime(2026, 11, 17, tzinfo=UTC))
    assert end - start == (df.LOG_WINDOW_MINUTES * 60 + df.LOG_AFTER_SECONDS) * 1000
    assert end == int((stop + timedelta(seconds=df.LOG_AFTER_SECONDS)).timestamp() * 1000)
    now = datetime(2026, 11, 17, tzinfo=UTC)
    start, end = df.log_window({}, now)  # no stop date: the minutes before now
    assert end == int(now.timestamp() * 1000) and end - start == df.LOG_WINDOW_MINUTES * 60_000
