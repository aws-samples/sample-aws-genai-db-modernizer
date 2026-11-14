# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Raw procedure and trigger rows, read once for every collector path (#1652): a
trigger's orientation, state and function, the execution counts the engine keeps,
Oracle's source lines and how the counts were read."""

from __future__ import annotations

from src.agents.collector.routine_rows import (
    attach_routine_sources,
    attach_routine_stats,
    execution_stats,
    routine_stats_status,
    trigger_enabled,
    trigger_for_each,
    trigger_function_name,
)


def test_for_each_from_any_spelling():
    assert trigger_for_each("BEFORE EACH ROW") == "ROW"
    assert trigger_for_each("ROW") == "ROW"
    assert trigger_for_each("AFTER STATEMENT") == "STATEMENT"
    assert trigger_for_each("BEFORE") is None
    assert trigger_for_each("ROWS") is None
    assert trigger_for_each(None) is None


def test_the_function_a_postgresql_trigger_runs():
    assert trigger_function_name("EXECUTE FUNCTION audit.log_order()") == "log_order"
    assert trigger_function_name('EXECUTE PROCEDURE "LogOrder"()') == "LogOrder"
    assert trigger_function_name(
        'CREATE TRIGGER t AFTER INSERT ON x FOR EACH ROW EXECUTE FUNCTION public."a.b"()'
    ) == "a.b"  # fmt: skip
    assert trigger_function_name("INSERT INTO t VALUES (1)") is None
    assert trigger_function_name("EXECUTE IMMEDIATE 'x'") is None
    assert trigger_function_name(None) is None


def test_enabled_in_every_source_spelling():
    for on in (True, 1, "1", "YES", "ENABLED", "O", "A"):
        assert trigger_enabled(on) is True, on
    for off in (False, 0, "0", "NO", "DISABLED", "D", "R"):
        assert trigger_enabled(off) is False, off
    assert trigger_enabled(None) is None and trigger_enabled("") is None


def test_execution_stats():
    assert execution_stats({"execution_count": 40, "total_time_ms": 100}) == {
        "total_executions": 40,
        "avg_execution_time_ms": 2.5,
    }
    assert execution_stats({"execution_count": "0", "total_time_ms": 5}) == {"total_executions": 0}
    assert execution_stats({}) is None
    assert execution_stats({"execution_count": -1}) is None
    assert execution_stats({"execution_count": True}) is None
    assert execution_stats({"execution_count": "many"}) is None


def test_stats_attach_by_kind_schema_and_name_and_sum_overloads():
    procs = [{"routine_name": "place_order", "schema_name": "dbo"}, {"routine_name": "f"}]
    trigs = [{"trigger_name": "tg", "schema_name": "dbo"}, {"trigger_name": "place_order"}]
    stats = [
        {"routine_name": "PLACE_ORDER", "schema_name": "dbo", "object_type": "PROCEDURE",
         "execution_count": 10, "total_time_ms": 50},
        {"routine_name": "f", "schema_name": "x", "object_type": "FUNCTION", "execution_count": 1},
        {"routine_name": "f", "schema_name": "x", "object_type": "FUNCTION", "execution_count": 2},
        {"routine_name": "tg", "schema_name": "dbo", "object_type": "TRIGGER", "execution_count": 7},
        {"routine_name": "gone", "schema_name": "dbo", "object_type": "PROCEDURE", "execution_count": 9},
    ]  # fmt: skip
    attach_routine_stats(procs, trigs, stats, source_engine="sqlserver")
    assert (procs[0]["execution_count"], procs[0]["total_time_ms"]) == (10, 50.0)
    assert procs[1]["execution_count"] == 3  # no schema on the row: matched by name
    assert "total_time_ms" not in procs[1]
    assert trigs[0]["execution_count"] == 7
    assert "execution_count" not in trigs[1]  # a procedure's count is not a trigger's


def test_stats_never_cross_schemas_when_both_sides_name_one():
    procs = [{"routine_name": "p", "schema_name": "a"}]
    attach_routine_stats(procs, [], [{"routine_name": "p", "schema_name": "b",
                                       "object_type": "PROCEDURE", "execution_count": 5}],
                         source_engine="mysql")  # fmt: skip
    assert "execution_count" not in procs[0]


def test_no_stats_section_changes_nothing():
    procs = [{"routine_name": "p"}]
    for stats in (None, [], "x", [None, {"execution_count": 1}]):
        attach_routine_stats(procs, [], stats, source_engine="mysql")
    assert procs == [{"routine_name": "p"}]


def test_oracle_sources_join_in_line_order():
    procs = [{"routine_name": "pkg", "schema_name": "app", "routine_type": "PACKAGE"},
             {"routine_name": "p", "schema_name": "app", "routine_type": "PROCEDURE"},
             {"routine_name": "kept", "schema_name": "app", "routine_type": "PROCEDURE",
              "definition": "already"}]  # fmt: skip
    trigs = [{"trigger_name": "tg", "schema_name": "app"}]
    sources = [
        {"name": "p", "owner": "app", "type": "PROCEDURE", "line": 2, "text": "BEGIN NULL; END;\n"},
        {"name": "p", "owner": "app", "type": "PROCEDURE", "line": 1, "text": "PROCEDURE p IS\n"},
        {"name": "pkg", "owner": "app", "type": "PACKAGE BODY", "line": 1, "text": "body\n"},
        {"name": "pkg", "owner": "app", "type": "PACKAGE", "line": 1, "text": "spec\n"},
        {"name": "tg", "owner": "app", "type": "TRIGGER", "line": 1, "text": "TRIGGER tg\r\n"},
        {"name": "kept", "owner": "app", "type": "PROCEDURE", "line": 1, "text": "other"},
        {"name": "p", "owner": "other", "type": "PROCEDURE", "line": 1, "text": "not this"},
    ]
    attach_routine_sources(procs, trigs, sources)
    assert procs[0]["definition"] == "spec\n\nbody\n"
    assert procs[1]["definition"] == "PROCEDURE p IS\nBEGIN NULL; END;\n"
    assert procs[2]["definition"] == "already"
    assert trigs[0]["definition"] == "TRIGGER tg\r\n"


def test_stats_status():
    assert routine_stats_status({"status": "tracked"}) == {"status": "tracked", "reason": None}
    assert (
        routine_stats_status({"status": "not_tracked", "reason": "x" * 900})["reason"] == "x" * 500
    )
    assert routine_stats_status({"status": "bogus"}) is None
    assert routine_stats_status(None) is None
