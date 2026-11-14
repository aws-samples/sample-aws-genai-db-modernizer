# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The live collectors' routine reads (#1652): bodies opt out, counts and how they were
read, MySQL's parameters, events and capture, Oracle's source lines and links."""

from __future__ import annotations

import pytest

from src.tools.database.mysql_tools import MySQLRemoteCollector
from src.tools.database.oracle_tools import OracleRemoteCollector
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector
from src.tools.database.sqlserver_tools import SQLServerRemoteCollector


def _collector(cls, *answers: list[dict]):
    collector = cls.__new__(cls)
    seen: list[str] = []
    queue = list(answers)

    def query(sql: str) -> list[dict]:
        seen.append(sql)
        return queue.pop(0) if queue else []

    collector._query = query  # type: ignore[method-assign]
    collector.get_current_schema = lambda: "APP"  # type: ignore[method-assign]
    return collector, seen


def test_postgresql_reads_every_user_schema_routines_and_triggers_with_their_create():
    collector, seen = _collector(PostgreSQLRemoteCollector)
    collector.collect_procedures()
    collector.collect_triggers()
    assert (
        "FROM pg_proc pr" in seen[0]
        and "n.nspname NOT IN ('pg_catalog', 'information_schema')" in seen[0]
    )
    assert "CASE WHEN 1 = 1 THEN pg_get_functiondef(pr.oid) END AS definition" in seen[0]
    assert (
        "FROM pg_trigger t" in seen[1]
        and "n.nspname NOT IN ('pg_catalog', 'information_schema')" in seen[1]
    )
    assert "CASE WHEN 1 = 1 THEN pg_get_triggerdef(t.oid) END AS definition" in seen[1]


def test_postgresql_opting_out_reads_no_body():
    collector, seen = _collector(PostgreSQLRemoteCollector)
    collector.collect_procedures(include_bodies=False)
    collector.collect_triggers(include_bodies=False)
    assert "CASE WHEN 0 = 1 THEN pg_get_functiondef(pr.oid) END" in seen[0]
    assert "CASE WHEN 0 = 1 THEN pg_get_triggerdef(t.oid) END" in seen[1]


@pytest.mark.parametrize(
    ("setting", "expected"),
    [("none", ([], {"status": "not_tracked", "reason": "track_functions is none"})),
     ("pl", ([{"routine_name": "f", "execution_count": 3}], {"status": "tracked"}))],
)  # fmt: skip
def test_postgresql_function_counts_say_how_they_were_read(setting, expected):
    collector, seen = _collector(
        PostgreSQLRemoteCollector,
        [{"routine_name": "f", "execution_count": 3}],
        [{"track_functions": setting}],
    )
    assert collector.collect_routine_stats() == expected
    assert "FROM pg_stat_user_functions" in seen[0]
    assert not any("SET " in sql for sql in seen)


def test_sqlserver_counts_need_view_server_state():
    collector, seen = _collector(SQLServerRemoteCollector, [{"ok": 0}])
    assert collector.collect_routine_stats() == (
        [], {"status": "not_tracked", "reason": "VIEW SERVER STATE is not granted"},
    )  # fmt: skip
    assert len(seen) == 1
    collector, seen = _collector(
        SQLServerRemoteCollector, [{"ok": 1}], [{"routine_name": "usp", "execution_count": 4}]
    )
    stats, status = collector.collect_routine_stats()
    assert (stats[0]["execution_count"], status) == (4, {"status": "tracked"})
    assert "sys.dm_exec_trigger_stats" in seen[1]


def test_mysql_procedures_carry_the_rebuild_columns_and_their_status():
    collector, seen = _collector(MySQLRemoteCollector)
    collector.collect_procedures()
    assert "IF(1 = 1, routine_definition, NULL) AS definition" in seen[0]
    assert "THEN 'not_readable'" in seen[0] and "definer" in seen[0]


def test_mysql_opting_out_reads_no_body():
    collector, seen = _collector(MySQLRemoteCollector)
    collector.collect_procedures(include_bodies=False)
    collector.collect_triggers(include_bodies=False)
    collector.collect_events(include_bodies=False)
    assert "IF(0 = 1, routine_definition, NULL)" in seen[0]
    assert "IF(0 = 1, action_statement, NULL)" in seen[1]
    assert "IF(0 = 1, event_definition, NULL)" in seen[2]


def test_mysql_parameters_and_events_are_read_for_the_current_database():
    collector, seen = _collector(MySQLRemoteCollector)
    collector.collect_routine_parameters()
    collector.collect_events()
    assert "FROM information_schema.parameters" in seen[0] and "DATABASE()" in seen[0]
    assert "FROM information_schema.events" in seen[1] and "DATABASE()" in seen[1]
    assert "%Y-%m-%d %H:%i:%s" in seen[1]


def test_mysql_capture_counts_and_names_what_is_not_granted():
    collector, _ = _collector(
        MySQLRemoteCollector, [{"n": 7}], [{"privilege": "TRIGGER"}, {"privilege": "EVENT"}],
        [{"n": 3}],
    )  # fmt: skip
    assert collector.collect_routine_capture() == {
        "routines_counted": 7,
        "parameters_counted": 3,
        "privileges_not_seen": ["EVENT", "TRIGGER"],
    }


def test_mysql_program_counts_are_read_only_where_the_server_keeps_them():
    collector, seen = _collector(MySQLRemoteCollector, [{"n": 0}])
    assert collector.collect_routine_stats() == (
        [], {"status": "not_tracked",
             "reason": "performance_schema program statistics are not available"},
    )  # fmt: skip
    assert len(seen) == 1
    collector, _ = _collector(MySQLRemoteCollector, [{"n": 1}], [{"ps": 0}])
    assert collector.collect_routine_stats()[1]["reason"] == "performance_schema is off"
    collector, seen = _collector(
        MySQLRemoteCollector,
        [{"n": 1}],
        [{"ps": 1}],
        [{"routine_name": "p", "execution_count": 12}],
    )
    stats, status = collector.collect_routine_stats()
    assert (stats[0]["execution_count"], status) == (12, {"status": "tracked"})
    assert "events_statements_summary_by_program" in seen[2]


def test_oracle_reads_source_lines_and_statement_units():
    collector, seen = _collector(
        OracleRemoteCollector,
        [{"owner": "app", "name": "p", "type": "PROCEDURE", "line": 1, "text": "PROCEDURE p"}],
        [{"query_id": "fms_1", "routine_schema": "app", "routine_name": "p"},
         {"query_id": "fms_1", "routine_schema": "app", "routine_name": "a"}],
    )  # fmt: skip
    assert collector.collect_routine_sources()[0]["name"] == "p"
    assert "FROM ALL_SOURCE" in seen[0] and "OWNER = 'APP'" in seen[0]
    assert collector.collect_query_routines() == {"fms_1": ["app.a", "app.p"]}
    assert "s.PROGRAM_ID <> 0" in seen[1]


def test_a_failed_oracle_link_read_records_no_links():
    collector = OracleRemoteCollector.__new__(OracleRemoteCollector)
    collector.get_current_schema = lambda: "APP"  # type: ignore[method-assign]

    def fail(sql: str) -> list[dict]:
        raise RuntimeError("ORA-00942")

    collector._query = fail  # type: ignore[method-assign]
    assert collector.collect_query_routines() is None
