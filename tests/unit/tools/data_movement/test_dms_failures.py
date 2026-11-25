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


STOP = datetime(2026, 11, 16, 15, 15, 40, tzinfo=UTC)
STOP_MS = int(STOP.timestamp() * 1000)


class FakeLogs:
    """A CloudWatch Logs client: FilterLogEvents over ``events`` ((epoch ms, message), oldest
    first, as CloudWatch returns them; one millisecond's events in the order given) within
    startTime/endTime, ``limit`` a page, with a next token while more remain. Each event has its
    own ``eventId``. As AWS documents, a page may be partly full or empty while a next token
    remains: ``page`` caps a page below ``limit``, and ``empty_pages`` empty pages come first on
    each query. ``next_forever``: an empty page with a next token, every call."""

    def __init__(self, events=(), *, next_forever: bool = False, page: int | None = None,
                 empty_pages: int = 0) -> None:  # fmt: skip
        self.events = sorted(events, key=lambda e: e[0])
        self.next_forever = next_forever
        self.page = page
        self.empty_pages = empty_pages
        self.calls: list[dict] = []

    def filter_log_events(self, **kwargs):
        self.calls.append(kwargs)
        if self.next_forever:
            return {"events": [], "nextToken": "more"}
        token = str(kwargs.get("nextToken") or f"{self.empty_pages}:0")
        empty, offset = (int(part) for part in token.split(":"))
        if empty:
            return {"events": [], "nextToken": f"{empty - 1}:{offset}"}
        found = [(i, e) for i, e in enumerate(self.events)
                 if kwargs["startTime"] <= e[0] <= kwargs["endTime"]]  # fmt: skip
        size = min(kwargs["limit"], self.page or kwargs["limit"])
        page = found[offset : offset + size]
        more = offset + size < len(found)
        return {"events": [{"eventId": f"ev-{i}", "timestamp": t, "message": m}
                           for i, (t, m) in page],
                **({"nextToken": f"0:{offset + size}"} if more else {})}  # fmt: skip


def _at(seconds_before_stop: float, message: str) -> tuple[int, str]:
    return STOP_MS - int(seconds_before_stop * 1000), message


def _window() -> tuple[int, int]:
    return df.log_window({"StopDate": STOP}, STOP + timedelta(days=1))


@pytest.mark.passes_on_main  # the message DMS gave live never named the failure (#2364)
def test_the_generic_fatal_message_alone_carries_no_hint():
    assert df.failure_hint({"Status": "failed", "LastFailureMessage": GENERIC_FATAL,
                            "StopReason": GENERIC_FATAL}) is None  # fmt: skip


def test_the_task_logs_control_schema_denial_carries_the_schema_grant():
    logs = FakeLogs([_at(1, PG_DENIAL_LOG[0]), _at(0, PG_DENIAL_LOG[1])])
    start, end = _window()
    found = df.log_hint(logs, "dms-tasks-ri", start, end)
    assert found is not None and found.id == "postgresql_control_schema"
    assert 'GRANT USAGE, CREATE ON SCHEMA "awsdms_control"' in found.hint
    # One read of the error lines only, the newest slice of the window, of this task's group.
    assert logs.calls == [{"logGroupName": "dms-tasks-ri", "startTime": end - 300_000,
                           "endTime": end, "filterPattern": df.LOG_ERRORS,
                           "limit": df.LOG_PAGE}]  # fmt: skip


def test_the_task_logs_mysql_control_denial_carries_the_grant_database_grant():
    line = ("2026-11-16T15:15:39 [TARGET_APPLY    ]E:  RetCode: SQL_ERROR  SqlState: 42000 "
            "NativeError: 1142 Message: [MySQL][ODBC 8.0(w) Driver][mysqld-8.0.39]CREATE command "
            "denied to user 'dms_user'@'10.0.0.1' for table 'awsdms_apply_exceptions' [1022502]")  # fmt: skip
    found = df.log_hint(FakeLogs([_at(1, line)]), "dms-tasks-ri", *_window())
    assert found is not None and found.id == "mysql_control_database"
    assert "GRANT ALL PRIVILEGES ON `awsdms\\_control`.*" in found.hint


@pytest.mark.parametrize(("hint_id", "message", "words"), KNOWN,
                         ids=[f"{h}-{i}" for i, (h, *_) in enumerate(KNOWN)])  # fmt: skip
def test_every_catalog_pattern_is_matched_against_log_lines_too(hint_id, message, words):
    """The log is matched with the same catalog as DMS's own message (synthetic lines: only the
    control-schema denial is backed by a rehearsal's real log line)."""
    line = f"2026-11-16T15:15:39 [SOURCE_CAPTURE  ]E:  {message} [1020101]"
    found = df.log_hint(FakeLogs([_at(5, "an unrelated error"), _at(1, line)]), "dms-tasks-ri",
                        *_window())  # fmt: skip
    assert found is not None and found.id == hint_id and words in found.hint


@pytest.mark.parametrize("message", UNKNOWN)
def test_a_log_with_no_known_failure_carries_no_hint(message):
    events = [_at(2, message), _at(1, "Stream component failed at subtask 0")]
    assert df.log_hint(FakeLogs(events), "dms-tasks-ri", *_window()) is None


def test_the_log_read_is_bounded_in_calls():
    endless = FakeLogs(next_forever=True)  # empty pages with a next token, forever
    assert df.log_hint(endless, "dms-tasks-ri", *_window()) is None
    assert len(endless.calls) == df.LOG_CALLS
    assert all(c["limit"] == df.LOG_PAGE for c in endless.calls)


def test_over_the_event_cap_the_newest_events_nearest_the_stop_are_kept():
    """FilterLogEvents returns the oldest first: a busy window keeps the newest LOG_EVENTS."""
    filler = [_at(10 + i * 0.1, "a line DMS logged") for i in range(df.LOG_EVENTS + 100)]
    newest = FakeLogs([*filler, _at(1, PG_DENIAL_LOG[0])])
    found = df.log_hint(newest, "dms-tasks-ri", *_window())
    assert found is not None and found.id == "postgresql_control_schema"
    oldest = FakeLogs([_at(290, PG_DENIAL_LOG[0]),
                       *(_at(1 + i * 0.1, "a line DMS logged") for i in range(df.LOG_EVENTS))])  # fmt: skip
    assert df.log_hint(oldest, "dms-tasks-ri", *_window()) is None  # past the cap: never matched
    assert len(oldest.calls) <= df.LOG_CALLS


# --- Within a busy slice, the newest events first (#2383) ------------------------------------


class _Recorder:
    """A hint pattern that matches nothing and records every message it is asked about."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    def search(self, message: str) -> None:
        self.seen.append(message)


def _recording(monkeypatch) -> _Recorder:
    recorder = _Recorder()
    hint = df.FailureHint("recorder", recorder, "", "")  # type: ignore[arg-type]
    monkeypatch.setattr(df, "FAILURE_HINTS", (hint,))
    return recorder


BINLOG_OFF = "2026-11-16T15:15:39 [SOURCE_CAPTURE  ]E:  binary logging is disabled [1020101]"


def _noise(count: int, *, newest_s: float = 10.0, step_s: float = 0.1) -> list[tuple[int, str]]:
    """``count`` error lines no hint knows, ``step_s`` apart, the newest ``newest_s`` before the
    stop."""
    return [_at(newest_s + i * step_s, "a line DMS logged") for i in range(count)]


@pytest.mark.parametrize("noise", [1000, 1500])
def test_a_match_just_before_the_stop_is_found_behind_a_busy_slice(noise):
    """#2380 review: a ``binary logging is disabled`` line 5 ms before the stop was found behind
    999 noise events and missed behind 1,000, because the slice was paged oldest first."""
    logs = FakeLogs([*_noise(noise), _at(0.005, BINLOG_OFF)])
    found = df.log_hint(logs, "dms-tasks-ri", *_window())
    assert found is not None and found.id == "mysql_binary_logging"
    assert len(logs.calls) <= df.LOG_CALLS
    assert all(c["limit"] == df.LOG_PAGE for c in logs.calls)


def test_the_newest_match_wins_over_an_older_one_in_a_busy_slice():
    """Two known failures in one busy slice: the one nearest the stop is the hint."""
    logs = FakeLogs([_at(150, PG_DENIAL_LOG[0]), *_noise(1000), _at(0.005, BINLOG_OFF)])
    found = df.log_hint(logs, "dms-tasks-ri", *_window())
    assert found is not None and found.id == "mysql_binary_logging"


@pytest.mark.passes_on_main  # an empty window was already read in a few calls
def test_an_empty_window_is_read_in_its_slices_and_carries_no_hint():
    logs = FakeLogs([])
    start, end = _window()
    assert df.log_hint(logs, "dms-tasks-ri", start, end) is None
    slices = -(-(end - start) // (df.LOG_SLICE_SECONDS * 1000))  # ceiling
    assert len(logs.calls) == slices
    assert logs.calls[0]["endTime"] == end and logs.calls[-1]["startTime"] == start


@pytest.mark.passes_on_main  # quiet slices were already read back one by one
def test_a_match_in_an_older_slice_is_found_past_empty_and_quiet_newer_ones():
    """The newest slice holds nothing, the next a few unrelated lines, and the denial is in
    the slice before that: the read goes back slice by slice and finds it."""
    stop_s = df.LOG_SLICE_SECONDS  # the newest slice ends LOG_AFTER_SECONDS past the stop
    logs = FakeLogs([_at(stop_s + 10 + i, "a line DMS logged") for i in range(5)]
                    + [_at(stop_s * 2 + 30, PG_DENIAL_LOG[0])])  # fmt: skip
    found = df.log_hint(logs, "dms-tasks-ri", *_window())
    assert found is not None and found.id == "postgresql_control_schema"
    assert len(logs.calls) <= df.LOG_CALLS


def test_a_match_in_an_older_busy_slice_is_found_past_an_empty_newer_one():
    """The newest slice is empty and the one before it is busy, its newest line the denial."""
    older = df.LOG_SLICE_SECONDS - df.LOG_AFTER_SECONDS  # seconds before the stop the slice ends
    logs = FakeLogs([*_noise(1000, newest_s=older + 20), _at(older + 5, PG_DENIAL_LOG[0])])
    found = df.log_hint(logs, "dms-tasks-ri", *_window())
    assert found is not None and found.id == "postgresql_control_schema"
    assert len(logs.calls) <= df.LOG_CALLS


@pytest.mark.passes_on_main  # one page that holds the slice exactly needs no narrower read
def test_a_slice_of_exactly_one_page_is_read_in_one_call():
    logs = FakeLogs([*_noise(df.LOG_PAGE - 1), _at(150, PG_DENIAL_LOG[0])])
    found = df.log_hint(logs, "dms-tasks-ri", *_window())
    assert found is not None and found.id == "postgresql_control_schema"
    assert len(logs.calls) == 1


@pytest.mark.passes_on_main  # the event cap held on main when the slice fit the call budget
@pytest.mark.parametrize(("newer", "found_it"), [(df.LOG_EVENTS - 1, True), (df.LOG_EVENTS, False)])
def test_the_event_cap_is_exact(newer, found_it):
    """The denial is matched as the LOG_EVENTS-th newest event, and never as the one after."""
    noise = _noise(newer, newest_s=1.0, step_s=0.1)
    logs = FakeLogs([*noise, _at(1.0 + 0.1 * newer + 5, PG_DENIAL_LOG[0])])
    found = df.log_hint(logs, "dms-tasks-ri", *_window())
    assert (found is not None and found.id == "postgresql_control_schema") is found_it
    assert len(logs.calls) <= df.LOG_CALLS


@pytest.mark.passes_on_main  # main also stopped at the budget and matched its newest events
def test_a_burst_too_dense_to_narrow_stops_at_the_call_budget(monkeypatch):
    """Thousands of lines in the last second: the reads stop at LOG_CALLS, and the pages already
    read are still matched, newest first, up to LOG_EVENTS lines."""
    burst = [(STOP_MS - 500 - i // 10, f"line {i:04d}") for i in range(5000)]  # 10 a millisecond
    recorder = _recording(monkeypatch)
    logs = FakeLogs(burst)
    assert df.log_hint(logs, "dms-tasks-ri", *_window()) is None
    assert len(logs.calls) == df.LOG_CALLS
    assert len(recorder.seen) == df.LOG_EVENTS == len(set(recorder.seen))
    ages = [int(m.split()[1]) // 10 for m in recorder.seen]  # same-millisecond lines tie
    assert ages == sorted(ages)


@pytest.mark.passes_on_main  # main also stopped at the call budget with no hint
def test_a_burst_too_dense_to_narrow_with_no_known_line_carries_no_hint():
    logs = FakeLogs([_at(0.5 + i * 0.0001, "a line DMS logged") for i in range(5000)])
    assert df.log_hint(logs, "dms-tasks-ri", *_window()) is None
    assert len(logs.calls) == df.LOG_CALLS


@pytest.mark.passes_on_main  # main paged this window in full; this guards the narrower reads
def test_a_page_all_in_one_millisecond_is_read_in_full_each_event_once(monkeypatch):
    """300 lines DMS stamped with the same millisecond, a line a second older, and 150 more:
    every line is matched, and each once."""
    same_ms = [_at(1.0, "a line DMS logged") for _ in range(300)]
    events = [*same_ms, _at(2.0, "the line before"), *_noise(150, newest_s=3.0)]
    recorder = _recording(monkeypatch)
    logs = FakeLogs(events)
    assert df.log_hint(logs, "dms-tasks-ri", *_window()) is None
    assert len(recorder.seen) == len(events)
    assert sorted(recorder.seen) == sorted(m for _, m in events)
    assert len(logs.calls) <= df.LOG_CALLS


@pytest.mark.passes_on_main  # #2611 review: main paged the crowded millisecond with its token
def test_a_denial_inside_a_crowded_millisecond_is_found():
    """150 lines in one millisecond, the denial the 120th, nothing else in the window."""
    lines = [_at(1.0, "a line DMS logged") for _ in range(150)]
    lines[119] = _at(1.0, PG_DENIAL_LOG[0])
    found = df.log_hint(FakeLogs(lines), "dms-tasks-ri", *_window())
    assert found is not None and found.id == "postgresql_control_schema"


@pytest.mark.passes_on_main  # #2611 review: FilterLogEvents may return partial pages
def test_a_denial_inside_one_millisecond_read_in_partial_pages_is_found():
    """60 lines in one millisecond, pages of 40 while a next token remains, the denial 50th."""
    lines = [_at(1.0, "a line DMS logged") for _ in range(60)]
    lines[49] = _at(1.0, PG_DENIAL_LOG[0])
    logs = FakeLogs(lines, page=40)
    found = df.log_hint(logs, "dms-tasks-ri", *_window())
    assert found is not None and found.id == "postgresql_control_schema"
    assert len(logs.calls) <= df.LOG_CALLS


@pytest.mark.passes_on_main  # main followed the token past an empty page
def test_an_empty_page_with_a_next_token_is_followed_not_queried_again():
    """Each query's first page is empty with a next token, as FilterLogEvents may return: the
    read follows the token, never sends the same query again, and reaches the denial."""
    logs = FakeLogs([*_noise(50, newest_s=1.0), _at(400, PG_DENIAL_LOG[0])], empty_pages=1)
    found = df.log_hint(logs, "dms-tasks-ri", *_window())
    assert found is not None and found.id == "postgresql_control_schema"
    fresh = [(c["startTime"], c["endTime"]) for c in logs.calls if "nextToken" not in c]
    assert len(fresh) == len(set(fresh))


@pytest.mark.passes_on_main  # main read the slice's pages once each
def test_a_full_page_at_the_windows_last_millisecond_is_not_read_twice(monkeypatch):
    """150 lines in the window's last millisecond: the rest of it comes from the next token,
    never from a narrower query that returns the same page again."""
    start, end = _window()
    recorder = _recording(monkeypatch)
    logs = FakeLogs([(end, "a line DMS logged") for _ in range(150)])
    assert df.log_hint(logs, "dms-tasks-ri", start, end) is None
    assert len(recorder.seen) == 150
    assert len(logs.calls) == 2 + 3  # the slice's two pages, then the three older slices
    assert logs.calls[1] == {**logs.calls[0], "nextToken": "0:100"}


def test_a_retry_never_sees_the_earlier_runs_errors():
    """#2364 review: the control-schema denial failed the task at 15:15; the customer granted it
    and retried at 15:21, and the retry failed on an error no hint knows. The window starts at
    the retry's StartDate, so the earlier denial is never shown again."""
    first_stop = datetime(2026, 11, 16, 15, 15, 40, tzinfo=UTC)
    retried = datetime(2026, 11, 16, 15, 21, 33, tzinfo=UTC)
    second_stop = datetime(2026, 11, 16, 15, 22, 10, tzinfo=UTC)
    ms = lambda d: int(d.timestamp() * 1000)  # noqa: E731
    logs = FakeLogs([(ms(first_stop) - 1000, PG_DENIAL_LOG[0]),
                     (ms(second_stop) - 1000, "an error no hint knows")])  # fmt: skip
    start, end = df.log_window({"StartDate": retried, "StopDate": second_stop}, second_stop)
    assert start == ms(retried) and end == ms(second_stop) + df.LOG_AFTER_SECONDS * 1000
    assert df.log_hint(logs, "dms-tasks-ri", start, end) is None


def test_the_log_window_ends_just_past_the_tasks_stop():
    stop = datetime(2026, 11, 16, 15, 15, 40, tzinfo=UTC)
    start, end = df.log_window({"StopDate": stop}, datetime(2026, 11, 17, tzinfo=UTC))
    assert end - start == (df.LOG_WINDOW_MINUTES * 60 + df.LOG_AFTER_SECONDS) * 1000
    assert end == int((stop + timedelta(seconds=df.LOG_AFTER_SECONDS)).timestamp() * 1000)
    started = stop - timedelta(seconds=2)  # the Discourse run: it failed 2 s after its start
    start, _ = df.log_window({"StartDate": started, "StopDate": stop}, stop)
    assert start == int(started.timestamp() * 1000)
    now = datetime(2026, 11, 17, tzinfo=UTC)
    start, end = df.log_window({}, now)  # no stop date: the minutes before now
    assert end == int(now.timestamp() * 1000) and end - start == df.LOG_WINDOW_MINUTES * 60_000
