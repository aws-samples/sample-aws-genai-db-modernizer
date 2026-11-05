# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Trigger timing and events, read once for every collector path (#1651)."""

from __future__ import annotations

import pytest

from src.agents.collector.routine_rows import merge_trigger_rows, trigger_events, trigger_timing
from src.contracts.collector_output import Trigger, TriggerEventType


def test_events_split_every_source_spelling():
    assert trigger_events("INSERT OR UPDATE OR DELETE") == ["INSERT", "UPDATE", "DELETE"]
    assert trigger_events("INSERT,UPDATE") == ["INSERT", "UPDATE"]
    assert trigger_events(" delete ") == ["DELETE"]
    assert trigger_events(["DELETE", "INSERT"]) == ["INSERT", "DELETE"]
    assert trigger_events("UPDATE OR TRUNCATE") == ["UPDATE"]


@pytest.mark.parametrize("raw", ["LOGON", "TRUNCATE", "DDL", "OTHER", "MULTIPLE", "", None, []])
def test_no_dml_event_gives_none(raw):
    assert trigger_events(raw) == []


def test_events_match_whole_words_only():
    assert trigger_events("UPSERTED_ROWS INSERTS") == []


def test_timing_reads_underscore_and_suffixed_forms():
    assert trigger_timing("INSTEAD_OF") == "INSTEAD OF"
    assert trigger_timing("instead of") == "INSTEAD OF"
    assert trigger_timing("INSTEAD OF EACH ROW") == "INSTEAD OF"
    assert trigger_timing("BEFORE EACH ROW") == "BEFORE"
    assert trigger_timing("AFTER STATEMENT") == "AFTER"
    assert trigger_timing("COMPOUND") == "AFTER"


@pytest.mark.parametrize("raw", ["", None, "ON COMMIT", "INSTEAD"])
def test_unknown_timing_gives_none(raw):
    assert trigger_timing(raw) is None


def test_per_event_rows_merge_by_schema_table_and_name():
    rows = [
        {"trigger_name": "tg", "table_name": "orders", "schema_name": "s", "event_type": "UPDATE"},
        {"trigger_name": "tg", "table_name": "orders", "schema_name": "s", "event_type": "INSERT"},
        {"trigger_name": "tg", "table_name": "items", "schema_name": "s", "event_type": "DELETE"},
        {"trigger_name": "tg", "table_name": "orders", "schema_name": "t", "event_type": "DELETE"},
    ]
    merged = merge_trigger_rows(rows)
    assert [(r["schema_name"], r["table_name"], r["events"]) for r in merged] == [
        ("s", "orders", ["INSERT", "UPDATE"]),
        ("s", "items", ["DELETE"]),
        ("t", "orders", ["DELETE"]),
    ]
    assert merged[0]["event_type"] == "UPDATE"  # the first row's own fields are kept


def test_merge_keeps_rows_with_no_dml_event_and_skips_non_mappings():
    rows = [{"trigger_name": "logon_tg", "event_type": "LOGON"}, "not a row", None]
    assert merge_trigger_rows(rows) == [{"trigger_name": "logon_tg", "event_type": "LOGON",
                                         "events": []}]  # fmt: skip
    assert merge_trigger_rows(None) == []


def test_trigger_keeps_every_event():
    t = Trigger.model_validate({"trigger_id": "t", "trigger_name": "t", "table_id": "orders",
                                "event_type": "INSERT", "timing": "AFTER",
                                "events": ["INSERT", "UPDATE"]})  # fmt: skip
    assert t.events == [TriggerEventType.INSERT, TriggerEventType.UPDATE]


@pytest.mark.passes_on_main
def test_old_trigger_without_events_loads():
    t = Trigger.model_validate({"trigger_id": "t", "trigger_name": "t", "table_id": "orders",
                                "event_type": "DELETE", "timing": "AFTER"})  # fmt: skip
    assert t.event_type == TriggerEventType.DELETE
