# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Triggers the DML list cannot hold are kept in the collection (#2023): a PostgreSQL
trigger that fires on TRUNCATE alone, PostgreSQL event triggers, and the DDL, LOGON and
STARTUP triggers a live Oracle collector sees. Each is kept with its body status, its
CREATE stays in the routine sources; no design input holds them, and routing never reads
them (``tests/e2e/test_non_dml_triggers_route_nothing.py``)."""

from __future__ import annotations

import copy
import json
import logging
from pathlib import Path

import pytest

from src.agents.collector import oracle_collector, postgres_collector
from src.agents.collector.offline_ingest import build_offline_collector_output
from src.agents.collector.routine_sources import (
    drop_routine_bodies,
    iter_collection_sources,
    non_dml_triggers,
    routine_sources_from_collection,
)
from src.contracts.collector_output import CollectorOutputContract
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.sql.redact import redact_raw_collection, redact_values
from tests.unit.agents.routine_collections import (
    PG_EVENT_TRIGGER,
    PG_EVENT_TRIGGER_NO_TAGS,
    PG_TRUNCATE_TRIGGER,
    SEED,
    pg_collection,
    pg_collection_with_non_dml_triggers,
)

REPO = Path(__file__).resolve().parents[3]


def _output(raw: dict) -> dict:
    return build_offline_collector_output(
        parse_offline_collection(copy.deepcopy(raw)), job_id="j", database_name="shop", s3_key="k"
    )


def _by_name(output: dict) -> dict[str, dict]:
    return {t["trigger_name"]: t for t in output["database_schema"]["non_dml_triggers"]}


def test_a_truncate_only_trigger_and_event_triggers_are_kept_offline():
    output = _output(pg_collection_with_non_dml_triggers())
    schema = output["database_schema"]
    assert [t["trigger_name"] for t in schema["triggers"]] == ["tg_orders"]
    kept = _by_name(output)
    assert set(kept) == {"tg_orders_truncate", "Guard DDL", "log_ddl"}

    truncate = kept["tg_orders_truncate"]
    assert (truncate["scope"], truncate["table_id"], truncate["events"]) == (
        "table", "orders", ["TRUNCATE"],
    )  # fmt: skip
    assert (truncate["timing"], truncate["for_each"], truncate["is_enabled"]) == (
        "BEFORE", "STATEMENT", True,
    )  # fmt: skip
    assert truncate["function_id"] == "audit_orders"
    assert (truncate["body_status"], truncate["body_exact"]) == ("collected", True)

    guard = kept["Guard DDL"]
    assert (guard["scope"], guard["table_id"], guard["schema_name"]) == ("database", None, None)
    assert (guard["events"], guard["tags"]) == (
        ["ddl_command_start"],
        ["DROP TABLE", "ALTER TABLE"],
    )
    assert (guard["function_id"], guard["owner"], guard["is_enabled"]) == (
        "guard_ddl", "postgres", True,
    )  # fmt: skip
    assert guard["depends_on"] == [{"kind": "function", "name": "public.guard_ddl()"}]
    assert (guard["body_status"], guard["body_chars"]) == ("collected", len(PG_EVENT_TRIGGER))
    assert guard["definition"].startswith('CREATE EVENT TRIGGER "Guard DDL" ON ddl_command_start')
    assert kept["log_ddl"]["is_enabled"] is False


def test_their_literals_are_redacted_in_the_collection():
    raw = pg_collection_with_non_dml_triggers()
    assert SEED not in json.dumps(_output(raw))
    assert SEED not in json.dumps(redact_raw_collection(raw))


def test_routine_sources_keep_each_create_as_the_server_wrote_it():
    raw = pg_collection_with_non_dml_triggers()
    by_key = {(s.kind, s.specific_name): s for s in iter_collection_sources(raw)}
    truncate = by_key[("trigger", "orders.tg_orders_truncate")]
    assert (truncate.create_statement, truncate.origin) == (
        PG_TRUNCATE_TRIGGER,
        "pg_get_triggerdef",
    )
    assert SEED in truncate.create_statement
    guard = by_key[("event_trigger", "Guard DDL")]
    assert (guard.create_statement, guard.origin, guard.exact) == (
        PG_EVENT_TRIGGER, "pg_event_trigger", True,
    )  # fmt: skip
    assert (guard.routine_id, guard.schema_name) == ("Guard DDL", None)
    assert by_key[("event_trigger", "log_ddl")].create_statement == PG_EVENT_TRIGGER_NO_TAGS
    # the earlier routines are unchanged
    plain = {(s.kind, s.specific_name) for s in iter_collection_sources(pg_collection())}
    assert plain < set(by_key)


@pytest.mark.parametrize(
    ("key", "value"),
    [("quoted_name", "x ON sql_drop EXECUTE FUNCTION evil(); --"), ("event", "ddl; DROP"),
     ("quoted_function", "public.f(); DROP TABLE t; --"), ("quoted_name", None)],
)  # fmt: skip
def test_an_event_trigger_piece_of_the_wrong_shape_gives_no_create(key, value):
    from src.agents.collector.routine_sources import event_trigger_create

    row = pg_collection_with_non_dml_triggers()["event_triggers"][0]
    assert event_trigger_create(row) == PG_EVENT_TRIGGER
    assert event_trigger_create({**row, key: value}) is None


def test_an_event_trigger_whose_create_cannot_be_rebuilt_is_not_supported():
    raw = pg_collection_with_non_dml_triggers()
    raw["event_triggers"][0]["tags"] = ["DROP TABLE'); DROP TABLE x; --"]
    build = routine_sources_from_collection(raw)
    assert ("event_trigger", "Guard DDL") not in {(s.kind, s.specific_name) for s in build.sources}
    assert build.statuses[("event_trigger", "", "Guard DDL")] == "not_supported"
    guard = _by_name(_output(raw))["Guard DDL"]
    assert (guard["body_status"], guard["definition"], guard["tags"]) == (
        "not_supported", None, [],
    )  # fmt: skip


def test_opting_out_keeps_names_and_sizes_but_no_body_and_no_source():
    raw = pg_collection_with_non_dml_triggers(include_bodies=False)
    kept = _by_name(_output(raw))
    assert {t["body_status"] for t in kept.values()} == {"opted_out"}
    assert all(t["definition"] is None for t in kept.values())
    assert kept["Guard DDL"]["body_chars"] == len(PG_EVENT_TRIGGER)
    assert "include_routine_bodies = 0" in kept["Guard DDL"]["body_status_reason"]
    assert routine_sources_from_collection(raw).sources == []


def test_the_handlers_opt_out_drops_their_bodies_too():
    output = _output(pg_collection_with_non_dml_triggers())
    drop_routine_bodies(output)
    assert {t["body_status"] for t in _by_name(output).values()} == {"opted_out"}
    assert all(t["definition"] is None for t in _by_name(output).values())


def test_the_skip_warning_names_only_the_triggers_left_out(caplog):
    raw = pg_collection_with_non_dml_triggers()
    raw["triggers"].append({"trigger_name": "odd", "table_name": "orders", "event_type": "INSERT",
                            "timing": "SOMETIMES"})  # fmt: skip
    with caplog.at_level(logging.WARNING):
        _output(raw)
    warnings = [r.getMessage() for r in caplog.records if "Skipped" in r.getMessage()]
    assert len(warnings) == 1 and "orders.odd" in warnings[0]
    assert "tg_orders_truncate" not in warnings[0]


def test_a_trigger_on_dml_and_truncate_stays_a_dml_trigger():
    raw = pg_collection()
    raw["triggers"][0]["event_type"] = "INSERT OR TRUNCATE"
    raw["triggers"][0]["events"] = "INSERT OR TRUNCATE"
    output = _output(raw)
    assert [t["trigger_name"] for t in output["database_schema"]["triggers"]] == ["tg_orders"]
    assert output["database_schema"].get("non_dml_triggers") is None


def test_the_live_postgresql_collector_keeps_them_and_their_sources():
    raw = pg_collection_with_non_dml_triggers()

    class Db:
        def collect_procedures(self, include_bodies: bool = True):
            return copy.deepcopy(raw["procedures"])

        def collect_triggers(self, include_bodies: bool = True):
            return copy.deepcopy(raw["triggers"])

        def collect_event_triggers(self, include_bodies: bool = True):
            return copy.deepcopy(raw["event_triggers"])

    schema_raw = redact_values(copy.deepcopy(raw), "postgresql")  # what a checkpoint holds
    sources = postgres_collector._live_routines(Db(), schema_raw, include_bodies=True)
    assert sources == routine_sources_from_collection(raw).sources
    assert {s.kind for s in sources} >= {"event_trigger"}
    kept = non_dml_triggers(schema_raw["triggers"], schema_raw["event_triggers"])
    assert {t.trigger_name for t in kept or []} == {"tg_orders_truncate", "Guard DDL", "log_ddl"}
    assert {t.body_status.value for t in kept or [] if t.body_status} == {"collected"}


def test_the_live_postgresql_opt_out_reads_no_event_trigger_body():
    class Db:
        def __getattr__(self, name):
            raise AssertionError(f"{name} read although the caller opted out")

    assert (
        postgres_collector._live_routines(Db(), {"event_triggers": []}, include_bodies=False) == []
    )


def test_oracle_ddl_logon_and_startup_triggers_are_kept_with_their_scope():
    rows = [
        {"trigger_name": "audit_logon", "table_name": "", "schema_name": "hr",
         "timing": "AFTER", "event_type": "LOGON ", "for_each": "AFTER EVENT",
         "is_enabled": "ENABLED", "base_object_type": "DATABASE   ",
         "definition": "BEGIN log_it('x'); END;"},
        {"trigger_name": "no_drops", "table_name": "", "schema_name": "hr",
         "timing": "BEFORE", "event_type": "DROP OR TRUNCATE", "for_each": "BEFORE EVENT",
         "is_enabled": "DISABLED", "base_object_type": "SCHEMA"},
        {"trigger_name": "orders_biu", "table_name": "orders", "schema_name": "hr",
         "timing": "BEFORE", "event_type": "INSERT OR UPDATE", "for_each": "BEFORE EACH ROW",
         "is_enabled": "ENABLED", "base_object_type": "TABLE"},
    ]  # fmt: skip
    kept = {t.trigger_name: t for t in non_dml_triggers(rows, qualified=True) or []}
    assert set(kept) == {"audit_logon", "no_drops"}
    assert (kept["audit_logon"].trigger_id, kept["audit_logon"].scope.value) == (
        "hr.audit_logon", "database",
    )  # fmt: skip
    assert (kept["audit_logon"].events, kept["audit_logon"].is_enabled) == (["LOGON"], True)
    assert kept["no_drops"].scope.value == "schema"
    assert (kept["no_drops"].events, kept["no_drops"].timing.value) == (
        ["DROP", "TRUNCATE"],
        "BEFORE",
    )
    assert kept["no_drops"].is_enabled is False


def test_the_live_oracle_collector_reads_each_triggers_base_object():
    from src.tools.database.oracle_tools import OracleRemoteCollector

    seen: list[str] = []

    class Db(OracleRemoteCollector):
        def _query(self, sql):
            seen.append(sql)
            return []

        def get_current_schema(self):
            return "HR"

    Db.__new__(Db).collect_triggers()
    assert "BASE_OBJECT_TYPE AS base_object_type" in seen[0]
    assert "non_dml_triggers" in Path(oracle_collector.__file__).read_text()


# -- no design input or redaction path holds them ----------------------------------


def _with_non_dml_triggers(collector: dict) -> dict:
    schema = collector["database_schema"]
    schema["non_dml_triggers"] = [
        {"trigger_id": "tg_products_truncate", "trigger_name": "tg_products_truncate",
         "scope": "table", "table_id": "ecommerce.products", "events": ["TRUNCATE"],
         "definition": "CREATE TRIGGER tg_products_truncate BEFORE TRUNCATE ON products "
                       "FOR EACH STATEMENT EXECUTE FUNCTION keep_products()"},
        {"trigger_id": "guard_ddl", "trigger_name": "guard_ddl", "scope": "database",
         "events": ["ddl_command_start"], "function_id": "guard_ddl"},
    ]  # fmt: skip
    return collector


def test_no_schema_design_input_holds_them():
    """Carried as routines, never designed against (#2023): neither the per-engine nor
    the per-group design input (the model's prompt) holds their names or bodies."""
    from src.agents.schema_design.group_splitter import tables_for_queries
    from src.agents.schema_design.handler import filter_collector_for_assignment
    from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output
    from tests.unit.agents.test_group_input import _analysis, _products_group

    collector = json.loads(
        CollectorOutputContract.model_validate(
            _with_non_dml_triggers(get_ecommerce_collector_output())
        ).model_dump_json()
    )
    assert collector["database_schema"]["non_dml_triggers"]
    queries = collector["queries"]["query_patterns"]
    assignment = {"query_assignments": [
        {"query_id": q["query_id"], "assigned_engine": "dynamodb", "in_scope": True}
        for q in queries]}  # fmt: skip
    inputs = [
        filter_collector_for_assignment(collector, assignment, "dynamodb"),
        _products_group(collector, _analysis(collector)),
    ]
    assert tables_for_queries(queries, collector["database_schema"]["tables"])
    for data in inputs:
        text = json.dumps(data)
        for needle in ("tg_products_truncate", "guard_ddl", "keep_products", "non_dml_triggers"):
            assert needle not in text, needle


def test_every_body_bearing_list_is_redacted_on_both_paths():
    """One list of body-bearing schema lists (#2023): the contract's validator and the
    dict-level redaction (the read transform, the assess() upload staging) both read it,
    so a new one cannot be redacted on one path only."""
    from src.contracts.collector_output import DEFINITION_LISTS, Schema
    from src.tools.sql.redact import redact_collector_output

    with_bodies = set()
    for name, field in Schema.model_fields.items():
        args = getattr(field.annotation, "__args__", ())
        for arg in args:
            item = (getattr(arg, "__args__", None) or [None])[0]
            if isinstance(item, type) and "definition" in getattr(item, "model_fields", {}):
                with_bodies.add(name)
    assert with_bodies == set(DEFINITION_LISTS)

    body = "CREATE TRIGGER tg BEFORE TRUNCATE ON t FOR EACH STATEMENT EXECUTE FUNCTION f('jane@example.com')"
    payload = {
        "metadata": {"source_database": {"engine": "postgresql", "version": "16.2"}},
        "database_schema": {key: [{"definition": body}] for key in DEFINITION_LISTS},
    }
    out = redact_collector_output(payload)
    for key in DEFINITION_LISTS:
        assert "jane@example.com" not in out["database_schema"][key][0]["definition"], key


def test_the_raw_redaction_redacts_a_saved_outputs_non_dml_triggers():
    from src.tools.sql.redact import redact_collector_output

    output = _output(pg_collection_with_non_dml_triggers())
    output["database_schema"]["non_dml_triggers"][0]["definition"] = PG_TRUNCATE_TRIGGER
    assert SEED in json.dumps(output)  # as a dict-only path could hold it
    assert SEED not in json.dumps(redact_collector_output(output))


def test_the_part_cut_drops_a_dropped_tables_truncate_trigger_and_keeps_database_ones():
    from src.assessment.cut import _cut_schema_triggers

    tables = [
        {"table_id": "gone", "table_name": "gone"},
        {"table_id": "kept", "table_name": "kept"},
    ]
    schema = {
        "triggers": [{"trigger_name": "d1", "table_id": "gone"}],
        "non_dml_triggers": [
            {"trigger_name": "t1", "table_id": "gone", "scope": "table"},
            {"trigger_name": "t2", "table_id": "kept", "scope": "table"},
            {"trigger_name": "db", "table_id": None, "scope": "database"},
        ],
    }  # fmt: skip
    _cut_schema_triggers(schema, tables, {"kept"})
    assert schema["triggers"] == []
    assert [r["trigger_name"] for r in schema["non_dml_triggers"]] == ["t2", "db"]
    older: dict = {"tables": []}
    _cut_schema_triggers(older, tables, {"kept"})
    assert older == {"tables": []}  # a collection without the lists gets none
