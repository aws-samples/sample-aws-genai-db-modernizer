# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each routine's exact CREATE, its body status and the capture (#1652)."""

from __future__ import annotations

import logging

from src.agents.collector.routine_sources import (
    RoutineSourcesBuild,
    apply_body_statuses,
    iter_collection_sources,
    routine_capture,
    routine_sources_from_collection,
)
from tests.unit.agents.routine_collections import (
    PG_FUNCTION_INT,
    PG_FUNCTION_TEXT,
    PG_TRIGGER,
    SEED,
    mysql_collection,
    pg_collection,
)


def test_postgresql_sources_are_the_servers_own_text_and_keep_overloads_apart():
    build = routine_sources_from_collection(pg_collection())
    by_name = {s.specific_name: s for s in build.sources}
    assert set(by_name) == {
        "set_status_16390",
        "set_status_16391",
        "audit_orders_16392",
        "orders.tg_orders",
    }
    assert by_name["set_status_16390"].create_statement == PG_FUNCTION_INT
    assert by_name["set_status_16391"].create_statement == PG_FUNCTION_TEXT
    assert by_name["orders.tg_orders"].create_statement == PG_TRIGGER
    assert by_name["orders.tg_orders"].origin == "pg_get_triggerdef"
    assert by_name["set_status_16390"].routine_id == "set_status"
    assert SEED in by_name["set_status_16390"].create_statement
    assert build.statuses == {}


def test_mysql_sources_are_rebuilt_and_a_procedure_and_function_of_one_name_stay_apart():
    build = routine_sources_from_collection(mysql_collection())
    assert sorted((s.kind, s.specific_name) for s in build.sources) == [
        ("event", "purge"),
        ("function", "place_order"),
        ("procedure", "place_order"),
        ("trigger", "orders.tg_a"),
        ("trigger", "orders.tg_b"),
    ]
    tg_b = next(s for s in build.sources if s.specific_name == "orders.tg_b")
    assert " FOLLOWS `tg_a` " in tg_b.create_statement
    proc = next(s for s in build.sources if s.kind == "procedure")
    assert proc.create_statement.startswith(
        "CREATE DEFINER=`app`@`%` PROCEDURE `place_order`(IN `p_id` int)"
    )
    assert (proc.session.sql_mode, proc.session.character_set_client) == (
        "STRICT_TRANS_TABLES",
        "utf8mb4",
    )


def test_the_stream_gives_the_same_sources_one_at_a_time():
    raw = mysql_collection()
    stream = iter_collection_sources(raw)
    assert next(stream).kind in ("procedure", "function")
    assert [s.sha256 for s in iter_collection_sources(raw)] == [
        s.sha256 for s in routine_sources_from_collection(raw).sources
    ]


def test_a_routine_that_cannot_be_rebuilt_is_not_supported_and_has_no_source():
    build = routine_sources_from_collection(mysql_collection())
    assert build.statuses == {("function", "shop", "js_label"): "not_supported"}
    assert all(s.specific_name != "js_label" for s in build.sources)


def test_unreadable_and_failed_rows_get_their_status_and_reason():
    raw = mysql_collection()
    build = routine_sources_from_collection(raw)
    apply_body_statuses(build, "mysql", procedures=raw["procedures"], triggers=raw["triggers"])
    by_name = {(p["routine_type"], p["routine_name"]): p for p in raw["procedures"]}
    assert by_name[("PROCEDURE", "secret_proc")]["body_status"] == "not_readable"
    assert "SHOW_ROUTINE" in by_name[("PROCEDURE", "secret_proc")]["body_status_reason"]
    assert by_name[("FUNCTION", "js_label")]["body_status"] == "not_supported"
    # a body with string literals, rebuilt from the catalog: collected, not exact
    place = by_name[("PROCEDURE", "place_order")]
    assert (place["body_status"], place["body_exact"]) == ("collected", False)
    assert "SHOW CREATE" in place["body_status_reason"]
    assert by_name[("FUNCTION", "place_order")]["body_exact"] is True  # RETURN 1


def test_opting_out_keeps_no_source_and_says_why():
    raw = mysql_collection(include_bodies=False)
    build = routine_sources_from_collection(raw)
    assert build.sources == []
    apply_body_statuses(
        build, "mysql", procedures=raw["procedures"], triggers=raw["triggers"], events=raw["events"]
    )
    assert {p["body_status"] for p in raw["procedures"]} == {"opted_out"}
    assert "include_routine_bodies = 0" in raw["events"][0]["body_status_reason"]


def test_a_collection_without_body_statuses_has_no_sources():
    old = pg_collection()
    for row in old["procedures"] + old["triggers"]:
        row.pop("body_status")
    assert routine_sources_from_collection(old) == RoutineSourcesBuild()


def test_other_engines_have_none():
    other = {**pg_collection(), "metadata": {"engine": "sqlserver"}}
    assert routine_sources_from_collection(other) == RoutineSourcesBuild()


def test_the_capture_reports_a_list_the_server_cut(caplog):
    with caplog.at_level(logging.WARNING):
        capture = routine_capture({"include_bodies": True, "routines_counted": 7}, listed=3)
    assert capture == {"include_bodies": True, "routines_counted": 7, "routines_listed": 3,
                       "privileges_not_seen": []}  # fmt: skip
    assert "max_allowed_packet" in caplog.text
    assert routine_capture(None, listed=3) is None
