# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The MySQL backend's load and call of an event (#1971), on a fake cursor so CI runs it
(the real-server test is ``tests/integration/test_routine_events_mysql.py``)."""

from __future__ import annotations

import pytest

from src.tools.database import disposable_engine as de

EVENT = de.SourceRoutine(
    "NightJob", "event", "CREATE EVENT NightJob ON SCHEDULE EVERY 1 HOUR DO SELECT 1"
)


class _Error(Exception):
    pass


class _Cursor:
    def __init__(self, definition=None):
        self.sql: list[tuple[str, object]] = []
        self.definition = definition
        self.description = None
        self.stored_results: list = []
        self._last = ""

    def execute(self, sql, params=None):
        self.sql.append((sql, params))
        self._last = sql

    def fetchall(self):
        if "EVENT_DEFINITION" in self._last and self.definition is not None:
            return [(self.definition,)]
        return []

    def nextset(self):
        return None

    def close(self):
        pass


class _Conn:
    def __init__(self, cursor):
        self.cur = cursor

    def cursor(self):
        return self.cur


def _backend(cursor):
    backend = object.__new__(de._MySQLBackend)
    backend._mc = type("mc", (), {"Error": _Error})
    backend._kinds = {}
    backend._no_run = {}
    backend._conn = _Conn(cursor)
    return backend


def _statements(cur):
    return [s for s, _ in cur.sql]


def test_loading_an_event_makes_a_runner_procedure_from_the_servers_own_body():
    cur = _Cursor("INSERT INTO t VALUES (1)")
    _backend(cur).load([], {}, [EVENT])
    runner = de.event_runner_name("nightjob")
    assert _statements(cur)[0] == EVENT.create_sql
    lookup = next((s, p) for s, p in cur.sql if "EVENT_DEFINITION" in s)
    assert lookup[1] == ["nightjob"] and "LOWER(EVENT_NAME)" in lookup[0]
    assert f"DROP PROCEDURE IF EXISTS `{runner}`" in _statements(cur)
    assert f"CREATE PROCEDURE `{runner}`() INSERT INTO t VALUES (1)" in _statements(cur)


@pytest.mark.passes_on_main  # main makes no runner for a procedure; this keeps it so
def test_loading_a_procedure_makes_no_runner():
    cur = _Cursor("x")
    _backend(cur).load(
        [], {}, [de.SourceRoutine("p", "procedure", "CREATE PROCEDURE p() SELECT 1")]
    )
    assert not any("__proof_event_" in s or "EVENT_DEFINITION" in s for s in _statements(cur))


def test_calling_an_event_calls_its_runner_not_the_event_name():
    cur = _Cursor("SELECT 1")
    backend = _backend(cur)
    backend.load([], {}, [EVENT])
    cur.sql.clear()
    result = backend.call("NightJob", [], [], None)
    assert result.error is None
    assert _statements(cur) == [f"CALL `{de.event_runner_name('NightJob')}`()"]


def test_calling_an_event_in_another_case_runs_the_same_runner():
    cur = _Cursor("SELECT 1")
    backend = _backend(cur)
    backend.load([], {}, [EVENT])
    cur.sql.clear()
    # another case, as the proof input names it: still the loaded event's runner
    backend.call(de.SourceRoutine("nightjob", "event", ""), [], [], None)
    assert _statements(cur) == [f"CALL `{de.event_runner_name('NightJob')}`()"]


@pytest.mark.parametrize(
    ("args", "modes", "casts"), [([1], None, None), ([], ["IN"], None), ([], None, ["integer"])]
)
def test_the_backend_refuses_each_argument_part_of_an_event_run(args, modes, casts):
    cur = _Cursor("SELECT 1")
    backend = _backend(cur)
    backend.load([], {}, [EVENT])
    cur.sql.clear()
    with pytest.raises(ValueError, match="no argument"):
        backend.call("NightJob", args, modes, casts)
    assert cur.sql == []


def test_an_event_the_server_dropped_on_creation_fails_only_its_own_run():
    cur = _Cursor(None)  # no row: its time had passed and the server dropped it
    backend = _backend(cur)
    backend.load(
        [], {}, [EVENT, de.SourceRoutine("p", "procedure", "CREATE PROCEDURE p() SELECT 1")]
    )
    cur.sql.clear()
    result = backend.call("NightJob", [], [], None)
    assert result.error is not None and "time had passed" in result.error
    assert cur.sql == []  # nothing ran


def test_an_event_with_a_name_no_runner_can_carry_loads_and_fails_only_its_own_run():
    cur = _Cursor("SELECT 1")
    backend = _backend(cur)
    odd = de.SourceRoutine(
        "my-ev", "event", "CREATE EVENT `my-ev` ON SCHEDULE EVERY 1 HOUR DO SELECT 1"
    )
    backend.load([], {}, [odd, EVENT])  # no ValueError, and the next routine still loads
    assert not any("my-ev" in s and "PROCEDURE" in s for s in _statements(cur))
    assert any(de.event_runner_name("NightJob") in s for s in _statements(cur))
    cur.sql.clear()
    result = backend.call("my-ev", [], [], None)
    assert result.error is not None and "plain identifier" in result.error
    assert cur.sql == []


def test_the_postgresql_backend_refuses_an_event():
    backend = object.__new__(de._PostgresBackend)
    backend._kinds = {}
    with pytest.raises(ValueError, match="MySQL only"):
        backend.call(de.SourceRoutine("e", "event", ""), [], None, None)
