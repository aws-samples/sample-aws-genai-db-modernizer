# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Offline collections carry procedure and trigger bodies, counts, links and their
migration fields to the collector contract, with no body literal (#1652)."""

from __future__ import annotations

import json
import logging

import pytest

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.sql.redact import redact_raw_collection
from tests.unit.agents.routine_collections import (
    SEED,
    mysql_collection,
    old_mysql_collection,
    pg_collection,
)


def _sqlserver(**extra) -> dict:
    base = {
        "metadata": {"database_name": "shop", "engine": "sqlserver"},
        "tables": [{"table_name": "orders", "schema_name": "dbo", "row_count": 10}],
        "columns": [{"table_name": "orders", "column_name": "id", "data_type": "int",
                     "is_nullable": "NO", "ordinal_position": 1}],
        "queries": [{"digest": "0xAA", "query_text": "UPDATE dbo.orders SET id = 1",
                     "execution_count": 40, "total_time_ms": 4}],
        "procedures": [{"routine_name": "usp_place", "schema_name": "dbo",
                        "routine_type": "PROCEDURE", "language": "TSQL",
                        "definition": f"CREATE PROCEDURE dbo.usp_place AS SELECT '{SEED}'"}],
        "triggers": [{"trigger_name": "tr_audit", "table_name": "orders", "schema_name": "dbo",
                      "timing": "INSTEAD OF", "event_type": "INSERT", "events": "INSERT,UPDATE",
                      "for_each": "STATEMENT", "is_enabled": 0,
                      "definition": "CREATE TRIGGER tr_audit ON dbo.orders INSTEAD OF INSERT AS SELECT 1"}],
    }  # fmt: skip
    base.update(extra)
    return base


def _output(raw: dict) -> dict:
    return build_offline_collector_output(
        parse_offline_collection(raw), job_id="j1", database_name="shop", s3_key="c.json"
    )


def test_counts_and_how_they_were_read_reach_the_contract():
    out = _output(_sqlserver(
        routine_stats=[{"routine_name": "usp_place", "schema_name": "dbo",
                        "object_type": "PROCEDURE", "execution_count": 40, "total_time_ms": 8},
                       {"routine_name": "tr_audit", "schema_name": "dbo",
                        "object_type": "TRIGGER", "execution_count": 3}],
        routine_stats_status={"status": "tracked"},
    ))  # fmt: skip
    (proc,) = out["database_schema"]["procedures"]
    assert proc["execution_stats"]["total_executions"] == 40
    assert proc["execution_stats"]["avg_execution_time_ms"] == 0.2
    assert proc["schema_name"] == "dbo"
    (trigger,) = out["database_schema"]["triggers"]
    assert trigger["execution_stats"]["total_executions"] == 3
    assert out["metadata"]["routine_stats_source"] == {"status": "tracked", "reason": None}


def test_a_trigger_keeps_its_orientation_state_and_body():
    (trigger,) = _output(_sqlserver())["database_schema"]["triggers"]
    assert (trigger["timing"], trigger["events"]) == ("INSTEAD OF", ["INSERT", "UPDATE"])
    assert (trigger["for_each"], trigger["is_enabled"]) == ("STATEMENT", False)
    assert trigger["definition"].startswith("CREATE TRIGGER")


@pytest.mark.passes_on_main
def test_a_procedure_body_is_redacted_in_the_contract():
    out = _output(_sqlserver())
    assert out["database_schema"]["procedures"][0]["definition"].startswith("CREATE PROCEDURE")
    assert SEED not in json.dumps(out)


def test_nested_statements_are_marked():
    raw = pg_collection()
    raw["queries"] = [
        {"digest": "1", "query_text": "UPDATE orders SET id = 1", "execution_count": 20, "toplevel": True},
        {"digest": "1", "query_text": "UPDATE orders SET id = 1", "execution_count": 20, "toplevel": False},
        {"digest": "2", "query_text": "SELECT id FROM orders", "execution_count": 20, "toplevel": True},
        {"digest": "3", "query_text": "SELECT 1 FROM orders", "execution_count": 20},
    ]  # fmt: skip
    nested = {q["query_id"]: q["nested"] for q in _output(raw)["queries"]["query_patterns"]}
    assert nested == {"1": True, "2": False, "3": None}


def test_oracle_bodies_join_from_their_source_lines_and_links_reach_the_statements():
    raw = {
        "metadata": {"database_name": "app", "engine": "oracle", "version": "19.0.0"},
        "tables": [{"table_name": "orders", "schema_name": "app", "row_count": 10}],
        "columns": [{"table_name": "orders", "column_name": "id", "data_type": "NUMBER",
                     "is_nullable": "NO", "ordinal_position": 1}],
        "queries": [{"digest": "fms_1", "query_text": "UPDATE orders SET id = :1",
                     "execution_count": 40, "total_time_ms": 4}],
        "procedures": [{"routine_name": "place_order", "schema_name": "app", "routine_type": "PROCEDURE"}],
        "triggers": [{"trigger_name": "tg", "table_name": "orders", "schema_name": "app",
                      "timing": "BEFORE", "event_type": "INSERT", "events": "INSERT OR UPDATE",
                      "for_each": "BEFORE EACH ROW", "is_enabled": "ENABLED"}],
        "routine_sources": [
            {"owner": "app", "name": "place_order", "type": "PROCEDURE", "line": 2,
             "text": f"BEGIN UPDATE orders SET note = '{SEED}'; END;\n"},
            {"owner": "app", "name": "place_order", "type": "PROCEDURE", "line": 1,
             "text": "PROCEDURE place_order IS\n"},
            {"owner": "app", "name": "tg", "type": "TRIGGER", "line": 1, "text": "TRIGGER tg\n"},
        ],
        "query_routines": [{"digest": "fms_1", "routine_schema": "app", "routine_name": "place_order"}],
        "routine_stats_status": {"status": "not_tracked",
                                 "reason": "Oracle keeps no per-routine execution count"},
    }  # fmt: skip
    out = _output(raw)
    (proc,) = out["database_schema"]["procedures"]
    assert proc["definition"].startswith("PROCEDURE place_order IS\nBEGIN UPDATE orders")
    (trigger,) = out["database_schema"]["triggers"]
    assert (trigger["for_each"], trigger["is_enabled"], trigger["definition"]) == (
        "ROW", True, "TRIGGER tg\n",
    )  # fmt: skip
    (query,) = out["queries"]["query_patterns"]
    assert query["parent_routine_ids"] == ["app.place_order"]
    assert out["metadata"]["routine_stats_source"]["status"] == "not_tracked"
    assert SEED not in json.dumps(out)
    stored = redact_raw_collection(raw)
    assert "routine_sources" not in stored
    assert SEED not in json.dumps(stored)
    assert _output(dict(stored))["database_schema"]["procedures"][0]["definition"].startswith(
        "PROCEDURE place_order IS"
    )


def test_postgresql_routines_reach_the_contract_with_their_migration_fields():
    out = _output(pg_collection())
    procs = {p["specific_name"]: p for p in out["database_schema"]["procedures"]}
    p = procs["set_status_16390"]
    assert (p["owner"], p["security_definer"], p["body_status"]) == ("app_owner", True, "collected")
    assert p["depends_on"] == [{"kind": "table", "name": "public.orders"}]
    assert procs["set_status_16391"]["depends_on"] == []
    (t,) = out["database_schema"]["triggers"]
    assert t["depends_on"] == [{"kind": "function", "name": "public.audit_orders()"}]
    assert (t["function_id"], t["is_enabled"], t["for_each"]) == ("audit_orders", True, "ROW")
    assert t["events"] == ["INSERT", "UPDATE"]
    assert t["body_status"] == "collected"
    assert out["metadata"]["routine_capture"]["include_bodies"] is True


def test_mysql_events_unreadable_routines_and_the_capture():
    out = _output(mysql_collection())
    (event,) = out["database_schema"]["events"]
    assert (event["event_id"], event["status"], event["schedule"]) == (
        "purge", "enabled", "EVERY 1 DAY STARTS '2030-01-01 00:00:00'",
    )  # fmt: skip
    statuses = {(p["procedure_type"], p["procedure_name"]): p["body_status"]
                for p in out["database_schema"]["procedures"]}  # fmt: skip
    assert statuses[("PROCEDURE", "secret_proc")] == "not_readable"
    assert statuses[("FUNCTION", "js_label")] == "not_supported"
    tg_b = next(t for t in out["database_schema"]["triggers"] if t["trigger_name"] == "tg_b")
    assert (tg_b["action_order"], tg_b["depends_on"]) == (2, [{"kind": "trigger", "name": "tg_a"}])
    assert tg_b["owner"] == "app@%"
    assert out["metadata"]["routine_capture"] == {
        "include_bodies": True, "routines_counted": 7, "routines_listed": 7,
        "parameters_counted": 3, "privileges_not_seen": ["EVENT"],
    }  # fmt: skip


def test_no_body_literal_reaches_the_collector_output_or_the_parse():
    for raw in (pg_collection(), mysql_collection()):
        parsed = parse_offline_collection(raw)
        assert SEED not in json.dumps(parsed, default=str)
        assert SEED not in json.dumps(_output(raw))


def test_the_raw_files_copy_is_redacted_too():
    for raw in (pg_collection(), mysql_collection()):
        assert SEED not in json.dumps(redact_raw_collection(raw))


def test_a_routine_list_the_server_cut_is_reported(caplog):
    raw = mysql_collection()
    raw["procedures"] = []
    with caplog.at_level(logging.WARNING):
        out = _output(raw)
    assert out["metadata"]["routine_capture"]["routines_listed"] == 3
    assert "max_allowed_packet" in caplog.text


def test_the_new_sections_are_known_to_the_input_check(caplog):
    raw = mysql_collection()
    raw["routine_stats"] = []
    raw["routine_stats_status"] = {"status": "tracked"}
    raw["routine_sources"] = []
    with caplog.at_level(logging.WARNING):
        parse_offline_collection(raw)
    assert "the format does not know" not in caplog.text


@pytest.mark.passes_on_main
def test_a_collection_written_before_the_change_builds_as_before():
    out = _output(old_mysql_collection())
    (p,) = out["database_schema"]["procedures"]
    assert p.get("body_status") is None and p.get("specific_name") is None
    assert out["database_schema"].get("events") is None
    assert out["metadata"].get("routine_capture") is None
    assert out["metadata"].get("routine_stats_source") is None
    (q,) = out["queries"]["query_patterns"]
    assert q.get("nested") is None and q.get("parent_routine_ids") is None
