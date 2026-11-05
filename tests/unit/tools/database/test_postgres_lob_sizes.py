# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The PostgreSQL collection measures each LOB column's longest value (#1918).

Step 1's DMS task runs in limited LOB mode from a PostgreSQL source, and a value above its
LobMaxSize is truncated, so the plan sizes LobMaxSize from ``max_value_bytes``. The script
measures only tables of at most ``lob_scan_max_mb`` MB the user may SELECT (0 turns it off).
A collection written before #1918 has no such key: its columns load with ``None``, and the
plan falls back to the catalog's size with a finding.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.contracts.collector_output import CollectorOutputContract, Column
from src.tools.database.offline_parser import parse_offline_collection

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "collect-postgresql.sql"


def _columns_block() -> str:
    match = re.search(r"\ncolumns_data AS \((.*?)\n\),", SCRIPT.read_text(), re.S)
    assert match, "collect-postgresql.sql has a columns_data block"
    return match.group(1)


def test_the_script_measures_lob_columns_only_when_allowed_and_small_enough():
    block = _columns_block()
    assert "AS max_value_bytes" in block
    # Only what the user may read, only tables up to the cap, and the cap can turn it off.
    assert "has_table_privilege(" in block and "'SELECT')" in block
    assert "pg_total_relation_size(" in block and "lob_scan_max_mb" in block
    assert "CAST(:'lob_scan_max_mb' AS bigint) > 0" in block
    # text and bytea lengths are read without a cast (from the stored header).
    assert "octet_length(%I%s)" in block


def test_the_script_defaults_the_cap_and_documents_it():
    text = SCRIPT.read_text()
    assert re.search(
        r"\\if :\{\?lob_scan_max_mb\}\n\\else\n  \\set lob_scan_max_mb 1024\n\\endif", text
    )
    assert "-v lob_scan_max_mb=0" in text


def _raw(measured: object = None, *, with_key: bool = True) -> dict:
    column: dict[str, object] = {
        "table_name": "t",
        "column_name": "body",
        "data_type": "text",
        "is_nullable": "YES",
    }
    if with_key:
        column["max_value_bytes"] = measured
    return {
        "metadata": {"version": "PostgreSQL 16.4 on x86_64", "database_name": "app"},
        "tables": [{"table_name": "t", "row_count": 10}],
        "columns": [column],
        "queries": [],
    }


def _column(raw: dict) -> Column:
    output = build_offline_collector_output(
        parse_offline_collection(raw), job_id="j1918", database_name="app", s3_key="k"
    )
    return CollectorOutputContract.model_validate(output).database_schema.tables[0].columns[0]


def test_the_offline_builder_keeps_the_measured_size():
    assert _column(_raw(1_866_112)).max_value_bytes == 1_866_112


def test_an_older_collection_loads_with_no_measured_size():
    assert _column(_raw(with_key=False)).max_value_bytes is None
    assert _column(_raw(None)).max_value_bytes is None


def test_a_measured_size_is_never_negative():
    assert (
        Column.model_validate(
            {"column_name": "c", "data_type": "text", "nullable": True}
        ).max_value_bytes
        is None
    )
    with pytest.raises(ValueError):
        Column.model_validate(
            {"column_name": "c", "data_type": "text", "nullable": True, "max_value_bytes": -1}
        )
