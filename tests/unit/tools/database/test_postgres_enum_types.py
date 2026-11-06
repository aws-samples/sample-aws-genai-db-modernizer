# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""PostgreSQL enum types and their labels reach the collector output (#1834).

``scripts/collect-postgresql.sql`` and the live collector read ``pg_enum`` (each enum's
labels in ``enumsortorder`` order); the offline and live builders keep them on
``database_schema.enum_types``. A collection written before #1834 has none: it loads with
``None`` (not collected), and step 1 then keeps its TEXT fallback for those columns.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any
from unittest.mock import MagicMock

import sqlglot

from src.agents.collector import postgres_collector
from src.agents.collector.mysql_collector import source_enum_types
from src.agents.collector.offline_ingest import build_offline_collector_output
from src.contracts.collector_output import CollectorOutputContract, SourceEnumType
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector
from tests.unit.tools.database.test_postgres_extensions_end_to_end import SCRIPT, _run_live

# As the script writes them: one entry per enum type, labels in their declared order.
ENUMS = [
    {"schema_name": "public", "type_name": "mood", "labels": ["sad", "ok", "happy"]},
    {"schema_name": "public", "type_name": "status", "labels": ["new", "it's done"]},
]


def _enum_block() -> str:
    match = re.search(r"\nenum_types_data AS \((.*?)\n\)", SCRIPT.read_text(), re.S)
    assert match, "collect-postgresql.sql has an enum_types_data block"
    return match.group(1)


def test_the_script_reads_each_enum_with_its_labels_in_sort_order():
    block = _enum_block()
    assert "pg_enum" in block and "pg_type" in block
    assert re.search(r"ORDER BY \w+\.enumsortorder", block)
    assert "'enum_types', (SELECT data FROM enum_types_data)" in SCRIPT.read_text()


def test_the_enum_block_parses_as_postgresql():
    tree = sqlglot.parse_one(_enum_block(), read="postgres")
    assert {"pg_enum", "pg_type", "pg_namespace"} <= {
        t.name for t in tree.find_all(sqlglot.exp.Table)
    }


def _raw(enums: Any = None, *, with_section: bool = True) -> dict:
    raw: dict[str, Any] = {
        "metadata": {"version": "PostgreSQL 16.4 on x86_64", "database_name": "app"},
        "tables": [{"table_name": "t", "row_count": 10}],
        "columns": [
            {"table_name": "t", "column_name": "id", "data_type": "integer", "is_nullable": "NO"}
        ],
        "queries": [],
    }
    if with_section:
        raw["enum_types"] = ENUMS if enums is None else enums
    return raw


def _offline(raw: dict) -> CollectorOutputContract:
    output = build_offline_collector_output(
        parse_offline_collection(raw), job_id="j1834", database_name="app", s3_key="k"
    )
    return CollectorOutputContract.model_validate(output)


def test_the_offline_builder_keeps_each_enum_and_its_labels_in_order():
    enums = _offline(_raw()).database_schema.enum_types
    assert enums == [SourceEnumType(**e) for e in ENUMS]
    assert enums is not None and enums[0].labels == ["sad", "ok", "happy"]


def test_a_collection_without_the_section_has_no_enum_list():
    assert _offline(_raw(with_section=False)).database_schema.enum_types is None


def test_an_entry_without_a_name_or_labels_list_is_dropped(caplog):
    raw = [
        {"schema_name": "public", "labels": ["a"]},
        {"schema_name": "public", "type_name": "x", "labels": "a,b"},
        "mood",
        {"schema_name": "public", "type_name": "badlabels", "labels": ["a", 1]},
        {"schema_name": "public", "type_name": "ok", "labels": ["a", "b"]},
    ]
    with caplog.at_level(logging.WARNING):
        kept = source_enum_types(raw)
    assert kept == [SourceEnumType(schema_name="public", type_name="ok", labels=["a", "b"])]
    assert source_enum_types(None) is None and source_enum_types([]) == []
    # The warning names the position, never the entry's text.
    assert not any("badlabels" in r.getMessage() for r in caplog.records)


def test_an_older_collector_output_loads_without_enum_types():
    output = json.loads(_offline(_raw()).model_dump_json())
    del output["database_schema"]["enum_types"]
    assert CollectorOutputContract.model_validate(output).database_schema.enum_types is None


def test_the_live_tools_read_pg_enum_in_sort_order():
    ssm = MagicMock()
    ssm.run_sql_json.return_value = ENUMS
    db = PostgreSQLRemoteCollector(
        ssm=ssm, host="h", port=5432, database="d", secret_arn="s", region="r"
    )
    assert db.collect_enum_types() == ENUMS
    sql = ssm.run_sql_json.call_args.kwargs["sql"]
    assert re.search(r"\bpg_enum\b", sql) and re.search(r"ORDER BY \w+\.enumsortorder", sql)


def test_a_live_collection_keeps_the_enum_types_it_read():
    result = _run_live({"enum_types": ENUMS})
    assert result.database_schema.enum_types == [SourceEnumType(**e) for e in ENUMS]
    assert _run_live({}).database_schema.enum_types is None


def test_the_schema_stage_reads_the_enum_types_and_survives_a_failure():
    db = MagicMock()
    db.collect_tables.return_value = []
    db.collect_enum_types.return_value = ENUMS
    assert postgres_collector._collect_schema_raw(db)["enum_types"] == ENUMS

    db.collect_enum_types.side_effect = RuntimeError("denied")
    assert postgres_collector._collect_schema_raw(db)["enum_types"] is None


def test_the_live_tools_read_back_the_labels_psql_prints_as_json():
    ssm = MagicMock()
    ssm.run_sql_json.return_value = [
        {"schema_name": "public", "type_name": "mood", "labels": '["sad", "ok"]'},
        {"schema_name": "public", "type_name": "odd", "labels": "not json"},
    ]
    db = PostgreSQLRemoteCollector(
        ssm=ssm, host="h", port=5432, database="d", secret_arn="s", region="r"
    )
    rows = db.collect_enum_types()
    assert rows[0]["labels"] == ["sad", "ok"]
    assert source_enum_types(rows) == [
        SourceEnumType(schema_name="public", type_name="mood", labels=["sad", "ok"])
    ]
