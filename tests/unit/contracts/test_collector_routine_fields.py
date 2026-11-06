# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Optional routine fields on the collector contract (#1652): how routine execution
counts were read, the function a trigger runs, a nested statement, and the routine
migration fields (owner, dependencies, body status, scheduled events, the capture)."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from src.contracts.collector_input import CollectionOptions
from src.contracts.collector_output import (
    CollectorOutputContract,
    EventStatus,
    Metadata,
    Procedure,
    QueryPattern,
    RoutineBodyStatus,
    RoutineCapture,
    RoutineDependency,
    RoutineStatsSource,
    RoutineStatsStatus,
    ScheduledEvent,
    Schema,
    Trigger,
)

_SOURCE = {"engine": "postgresql", "version": "16.2", "hostname": "h"}


def _metadata(**extra) -> Metadata:
    return Metadata.model_validate(
        {"collection_timestamp": "2026-11-06T00:00:00Z", "collector_version": "1.0.0",
         "source_database": _SOURCE, **extra}
    )  # fmt: skip


def test_metadata_records_how_routine_counts_were_read():
    m = _metadata(
        routine_stats_source={"status": "not_tracked", "reason": "track_functions is none"}
    )
    assert m.routine_stats_source == RoutineStatsSource(
        status=RoutineStatsStatus.not_tracked, reason="track_functions is none"
    )


def test_the_stats_reason_is_capped_and_the_status_closed():
    with pytest.raises(ValidationError):
        RoutineStatsSource(status=RoutineStatsStatus.tracked, reason="x" * 501)
    with pytest.raises(ValidationError):
        RoutineStatsSource.model_validate({"status": "sometimes"})


def test_a_trigger_names_its_function_owner_order_and_body_status():
    t = Trigger.model_validate(
        {"trigger_id": "tg", "trigger_name": "tg", "table_id": "orders", "event_type": "INSERT",
         "timing": "BEFORE", "function_id": "log_order", "owner": "app@%", "action_order": 2,
         "depends_on": [{"kind": "trigger", "name": "tg_first"}],
         "body_status": "not_readable", "body_status_reason": "grant TRIGGER"}
    )  # fmt: skip
    assert (t.function_id, t.owner, t.action_order) == ("log_order", "app@%", 2)
    assert t.depends_on == [RoutineDependency(kind="trigger", name="tg_first")]
    assert (t.body_status, t.definition) == (RoutineBodyStatus.not_readable, None)


def test_a_procedure_carries_its_migration_fields():
    p = Procedure.model_validate(
        {"procedure_id": "f", "procedure_name": "f", "procedure_type": "FUNCTION",
         "specific_name": "f_16390", "owner": "app_owner", "security_definer": True,
         "depends_on": [{"kind": "table", "name": "public.orders"}],
         "body_status": "collected", "body_chars": 8}
    )  # fmt: skip
    assert (p.specific_name, p.owner, p.security_definer) == ("f_16390", "app_owner", True)
    assert (p.body_status, p.body_chars) == (RoutineBodyStatus.collected, 8)


def test_a_dependency_kind_is_one_the_contract_knows():
    with pytest.raises(ValidationError):
        RoutineDependency.model_validate({"kind": "package", "name": "x"})


def test_a_query_pattern_says_it_ran_nested():
    q = QueryPattern.model_validate(
        {"query_id": "q1", "query_text": "UPDATE stock SET n = n - $1", "frequency_per_hour": 1,
         "tables_accessed": ["stock"], "nested": True}
    )  # fmt: skip
    assert q.nested is True


def test_scheduled_events_are_their_own_list_and_redacted():
    e = ScheduledEvent.model_validate(
        {"event_id": "purge", "event_name": "purge", "schema_name": "shop", "status": "disabled",
         "schedule": "EVERY 1 DAY", "owner": "app@%", "body_status": "opted_out", "body_chars": 40}
    )  # fmt: skip
    assert (e.status, e.definition) == (EventStatus.disabled, None)
    assert Schema.model_fields["events"].default is None
    contract = CollectorOutputContract.model_validate(
        {"job_id": "j", "metadata": _metadata(source_database={**_SOURCE, "engine": "mysql",
                                                             "version": "8.0.36"}).model_dump(),
         "database_schema": {"tables": [{"table_id": "t", "table_name": "t", "row_count": 1,
                                         "columns": [{"column_name": "id", "data_type": "int",
                                                      "nullable": False}]}],
                             "events": [{"event_id": "e", "event_name": "e",
                                         "definition": "DELETE FROM t WHERE c = 'jane@example.com'"}]},
         "queries": {"query_patterns": []},
         "metrics": {"performance_metrics": {}}}
    )  # fmt: skip
    assert "jane@example.com" not in json.dumps(contract.model_dump(mode="json"))


def test_metadata_records_the_routine_capture_and_privileges_are_names():
    m = _metadata(routine_capture={"include_bodies": True, "routines_counted": 7,
                                   "routines_listed": 7,
                                   "privileges_not_seen": ["trigger", "SHOW_ROUTINE", "TRIGGER"]})  # fmt: skip
    assert m.routine_capture == RoutineCapture(
        include_bodies=True, routines_counted=7, routines_listed=7,
        privileges_not_seen=["SHOW_ROUTINE", "TRIGGER"],
    )  # fmt: skip
    with pytest.raises(ValidationError):
        RoutineCapture(privileges_not_seen=["SELECT; DROP"])


def test_the_sources_ride_on_the_contract_but_are_never_serialised():
    assert "_routine_sources" in CollectorOutputContract.__private_attributes__
    assert "_routine_sources" not in json.dumps(CollectorOutputContract.model_json_schema())


def test_routine_bodies_are_collected_unless_the_caller_opts_out():
    assert CollectionOptions().include_routine_bodies is True
    assert CollectionOptions(include_routine_bodies=False).include_routine_bodies is False
    with pytest.raises(ValidationError):
        CollectionOptions(include_routine_bodies="no")


@pytest.mark.passes_on_main
def test_rows_written_before_the_fields_still_load():
    t = Trigger.model_validate(
        {"trigger_id": "t", "trigger_name": "t", "table_id": "orders",
         "event_type": "INSERT", "timing": "AFTER"}
    )  # fmt: skip
    q = QueryPattern.model_validate(
        {"query_id": "q1", "query_text": "SELECT 1", "frequency_per_hour": 1,
         "tables_accessed": ["unknown"]}
    )  # fmt: skip
    p = Procedure.model_validate(
        {"procedure_id": "f", "procedure_name": "f", "procedure_type": "FUNCTION"}
    )
    m = _metadata()
    assert getattr(t, "function_id", None) is None and getattr(t, "body_status", None) is None
    assert getattr(q, "nested", None) is None and q.parent_routine_ids is None
    assert getattr(p, "body_status", None) is None
    assert getattr(m, "routine_stats_source", None) is None
    assert getattr(m, "routine_capture", None) is None
