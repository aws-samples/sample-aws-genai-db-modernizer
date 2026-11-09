# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The collector contract's triggers outside the DML list (#2023), and the routine
sources' event triggers: both additive, so saved collections and routine sources load
unchanged."""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.contracts.collector_output import (
    CollectorOutputContract,
    NonDmlTrigger,
    NonDmlTriggerScope,
    Schema,
)
from src.contracts.routine_sources import RoutineSource, RoutineSourcesChunk
from src.shared.contract_versioning import load_contract

REPO = Path(__file__).resolve().parents[3]
OLD_OUTPUT = REPO / "tests/fixtures/old_jobs/v0.4.0-beta.2/wordpress/aa4e840a/collector/output.json"


def _contract(schema_extra: dict) -> CollectorOutputContract:
    return CollectorOutputContract.model_validate(
        {"job_id": "j",
         "metadata": {"collection_timestamp": "2026-11-09T00:00:00Z", "collector_version": "1.0.0",
                      "source_database": {"engine": "postgresql", "version": "16.2",
                                          "hostname": "h"}},
         "database_schema": {"tables": [{"table_id": "t", "table_name": "t", "row_count": 1,
                                         "columns": [{"column_name": "id", "data_type": "int",
                                                      "nullable": False}]}],
                             **schema_extra},
         "queries": {"query_patterns": []},
         "metrics": {"performance_metrics": {}}}
    )  # fmt: skip


def test_a_trigger_outside_the_dml_list_has_its_own_row():
    t = NonDmlTrigger.model_validate(
        {"trigger_id": "guard", "trigger_name": "guard", "scope": "database",
         "events": ["ddl_command_start"], "tags": ["DROP TABLE"], "is_enabled": True,
         "function_id": "guard_ddl", "owner": "postgres", "body_status": "collected",
         "body_chars": 90, "body_exact": True}
    )  # fmt: skip
    assert (t.scope, t.table_id, t.timing) == (NonDmlTriggerScope.database, None, None)
    assert Schema.model_fields["non_dml_triggers"].default is None


@pytest.mark.parametrize(
    ("field", "value"),
    [("events", []), ("events", ["DROP; x"]), ("tags", ["drop table"]), ("tags", ["A'B"]),
     ("scope", "server")],
)  # fmt: skip
def test_events_tags_and_scope_are_closed(field, value):
    row = {"trigger_id": "t", "trigger_name": "t", "scope": "table", "events": ["TRUNCATE"]}
    with pytest.raises(ValidationError):
        NonDmlTrigger.model_validate({**row, field: value})


def test_their_definitions_hold_no_literal():
    contract = _contract({"non_dml_triggers": [
        {"trigger_id": "tg", "trigger_name": "tg", "scope": "table", "table_id": "t",
         "events": ["TRUNCATE"],
         "definition": "CREATE TRIGGER tg BEFORE TRUNCATE ON t FOR EACH STATEMENT "
                       "EXECUTE FUNCTION f('jane@example.com')"}]})  # fmt: skip
    assert "jane@example.com" not in contract.model_dump_json()


def test_an_event_trigger_is_a_routine_source_kind():
    s = RoutineSource(
        kind="event_trigger", routine_id="guard", specific_name="guard",
        create_statement="CREATE EVENT TRIGGER guard ON ddl_command_start "
                         "EXECUTE FUNCTION public.f()",
        origin="pg_event_trigger",
    )  # fmt: skip
    assert s.key() == ("event_trigger", "", "guard")


@pytest.mark.passes_on_main
def test_a_saved_collection_without_the_list_still_loads():
    old = load_contract(
        CollectorOutputContract, json.loads(OLD_OUTPUT.read_text()), artifact=str(OLD_OUTPUT)
    )
    assert getattr(old.database_schema, "non_dml_triggers", None) is None
    assert old.database_schema.tables


@pytest.mark.passes_on_main
def test_saved_routine_sources_still_load():
    chunk = {"contract_version": "1.0", "part": 1, "sources": [
        {"kind": "trigger", "routine_id": "tg", "schema_name": "public",
         "specific_name": "orders.tg", "origin": "pg_get_triggerdef",
         "create_statement": "CREATE TRIGGER tg AFTER INSERT ON public.orders "
                             "FOR EACH ROW EXECUTE FUNCTION f()"}]}  # fmt: skip
    data = json.loads(gzip.decompress(gzip.compress(json.dumps(chunk).encode())))
    loaded = load_contract(RoutineSourcesChunk, data, artifact="part-0001.json.gz")
    assert loaded.sources[0].kind == "trigger"
