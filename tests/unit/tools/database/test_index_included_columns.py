# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""SQL Server INCLUDE columns are stored in an index, not part of its key (#2227).

Both collection paths keep them out of ``Index.columns`` and record them in
``Index.included_columns``: the offline script by ``key_ordinal`` 0, the live collector by
``sys.index_columns.is_included_column``. So neither the key-length gap rule nor the
Aurora MySQL draft counts them as key parts, which made the DNN sample report long keys
it does not have.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.agents.collector.offline_ingest import build_offline_collector_output
from src.agents.collector.sqlserver_collector import _build_tables
from src.contracts.collector_output import CollectorOutputContract, Index
from src.contracts.schema_design_input import project_schema_design_tables
from src.tools.analysis.relational_gaps import detect_relational_gaps
from src.tools.database.index_keys import is_included_column
from src.tools.database.offline_parser import parse_offline_collection
from src.tools.database.sqlserver_tools import SQLServerRemoteCollector
from src.tools.schema.aurora_common.ddl_generator import generate_mysql_ddl

_ROOT = Path(__file__).resolve().parents[4]
_KEY_LENGTH = "mysql-index-key-length"


def _row(column: str, key_ordinal: int, index: str = "IX_Files") -> dict[str, Any]:
    return {
        "table_name": "Files",
        "schema_name": "dbo",
        "index_name": index,
        "column_name": column,
        "key_ordinal": key_ordinal,
        "non_unique": 1,
        "index_type": "btree",
    }


def _column(name: str, data_type: str, max_length: int) -> dict[str, Any]:
    return {
        "table_name": "Files",
        "schema_name": "dbo",
        "column_name": name,
        "data_type": data_type,
        "max_length": max_length,
        "is_nullable": "YES",
    }


def _offline(index_rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "metadata": {"database_name": "db", "engine": "sqlserver", "version": "16.0.4305.5"},
        "tables": [{"table_name": "Files", "schema_name": "dbo", "row_count": 10}],
        "columns": [
            _column("FolderID", "int", 4),
            _column("FileName", "nvarchar", 246),
            _column("Content", "nvarchar", 3750),
            _column("Path", "nvarchar", 1000),
        ],
        "indexes": index_rows,
        "foreign_keys": [],
        "primary_keys": [],
    }


def _collector_output(data: dict[str, Any]) -> dict[str, Any]:
    return build_offline_collector_output(
        parse_offline_collection(data), job_id="j", database_name="db", s3_key="db.json"
    )


def test_the_offline_parser_keeps_included_columns_out_of_the_key() -> None:
    rows = [_row("Content", 0), _row("FileName", 2), _row("FolderID", 1)]
    [table] = parse_offline_collection(_offline(rows))["tables"]
    [index] = table["indexes"]
    assert index["columns"] == ["FolderID", "FileName"]
    assert index["included_columns"] == ["Content"]


def test_an_index_with_no_included_column_records_none() -> None:
    [table] = parse_offline_collection(_offline([_row("FolderID", 1)]))["tables"]
    [index] = table["indexes"]
    assert index["columns"] == ["FolderID"] and "included_columns" not in index


def test_an_index_whose_every_row_reads_as_included_keeps_them_as_its_columns() -> None:
    """A columnstore index lists its columns with key_ordinal 0: it has no key to keep
    apart, and an index with no column fails the contract."""
    rows = [_row("FolderID", 0, "CCI_Files"), _row("FileName", 0, "CCI_Files")]
    [table] = parse_offline_collection(_offline(rows))["tables"]
    [index] = table["indexes"]
    assert index["columns"] == ["FolderID", "FileName"] and "included_columns" not in index


@pytest.mark.parametrize(
    ("row", "included"),
    [
        ({"is_included_column": 1, "key_ordinal": 0}, True),
        ({"is_included_column": True}, True),
        ({"is_included_column": "True"}, True),
        ({"is_included_column": 0, "key_ordinal": 0}, False),
        ({"key_ordinal": 0}, True),
        ({"key_ordinal": "0"}, True),
        ({"key_ordinal": 1}, False),
        ({"key_ordinal": None}, False),
        ({"seq_in_index": 0}, False),
        ({"seq_in_index": 1, "key_ordinal": 0}, False),
        ({}, False),
    ],
)
def test_is_included_column(row: dict[str, Any], included: bool) -> None:
    assert is_included_column(row) is included


def test_the_live_collector_reads_is_included_column() -> None:
    ssm = MagicMock()
    ssm.run_sql_json.return_value = [
        {
            "index_name": "IX_Files",
            "column_name": name,
            "key_ordinal": ordinal,
            "is_included_column": included,
            "is_unique": 0,
            "is_primary_key": 0,
            "index_type": "NONCLUSTERED",
        }
        for name, ordinal, included in [("FolderID", 1, 0), ("FileName", 2, 0), ("Content", 0, 1)]
    ]
    live = SQLServerRemoteCollector(
        ssm=ssm,
        host="sqlserver-test.example.com",
        port=1433,
        database="testdb",
        secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-AAAAAA",  # pragma: allowlist secret  # nosec B106 — test fixture ARN, not a real credential
        region="us-east-1",
    )
    [index] = live.collect_indexes("dbo.Files")
    assert "ic.is_included_column" in ssm.run_sql_json.call_args.kwargs["sql"]
    assert index["columns"] == ["FolderID", "FileName"]
    assert index["included_columns"] == ["Content"]

    [table] = _build_tables(
        {
            "tables": [
                {
                    "schema_name": "dbo",
                    "table_name": "Files",
                    "row_count": 1,
                    "columns": [
                        {"column_name": c, "data_type": "int", "is_nullable": "YES"}
                        for c in ("FolderID", "FileName", "Content")
                    ],
                    "indexes": [index],
                }
            ]
        }
    )
    assert table.indexes is not None
    assert table.indexes[0].included_columns == ["Content"]


def test_the_offline_path_carries_included_columns_onto_the_contract() -> None:
    output = _collector_output(_offline([_row("FolderID", 1), _row("Content", 0)]))
    [table] = output["database_schema"]["tables"]
    [index] = table["indexes"]
    assert (index["columns"], index["included_columns"]) == (["FolderID"], ["Content"])


def test_an_index_saved_before_2227_still_loads() -> None:
    """A collection saved before #2227 has no included_columns (they sat in columns)."""
    index = Index.model_validate({"index_name": "ix", "columns": ["a", "b"], "is_unique": False})
    assert index.included_columns is None and index.columns == ["a", "b"]


def _gaps_and_notes(data: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict]]:
    output = _collector_output(data)
    result = detect_relational_gaps(output, "aurora_mysql")
    gap = next((g for g in (result or {})["gaps"] if g["rule_id"] == _KEY_LENGTH), None)
    tables = project_schema_design_tables(CollectorOutputContract.model_validate(output))
    notes = [
        n
        for n in generate_mysql_ddl(tables, source_engine="sqlserver").index_notes
        if n.get("kind") == "key_prefix"
    ]
    return gap, notes


def test_a_long_included_column_is_not_a_long_key() -> None:
    """DNN's IX_UserProfile_PropertyDefinitionID: an int key that INCLUDEs nvarchar(3750)."""
    gap, notes = _gaps_and_notes(_offline([_row("FolderID", 1), _row("Content", 0)]))
    assert gap is None and notes == []


def test_a_real_long_key_still_fires_and_names_only_its_key_columns() -> None:
    rows = [_row("Path", 1), _row("FolderID", 2), _row("Content", 0)]
    gap, notes = _gaps_and_notes(_offline(rows))
    assert gap is not None
    assert [o["id"] for o in gap["objects"]] == ["db.Files.Path"]
    assert [n["index"] for n in notes] == ["IX_Files"]


def _dnn() -> dict[str, Any]:
    with zipfile.ZipFile(_ROOT / "docs" / "examples" / "dnn" / "dnn.zip") as z:
        data: dict[str, Any] = json.loads(z.read("dnn-collection.json"))
    return data


def test_the_dnn_sample_has_no_key_over_the_aurora_mysql_limit() -> None:
    """Every DNN index the key-length rule and the draft flagged was a key plus INCLUDE
    columns: none of its keys passes 3,072 bytes, so neither flags one now."""
    data = _dnn()
    output = _collector_output(data)
    indexes = [i for t in output["database_schema"]["tables"] for i in t.get("indexes") or []]
    assert any(i.get("included_columns") for i in indexes)
    gap, notes = _gaps_and_notes(data)
    assert gap is None
    over = [n for n in notes if "over the" in n["reason"] or "not created" in n["reason"]]
    assert over == []


def test_aurora_postgresql_keeps_included_columns_as_include_and_aurora_mysql_drops_them() -> None:
    from src.tools.schema.aurora_common.ddl_generator import generate_pg_ddl

    output = _collector_output(_offline([_row("FolderID", 1), _row("Content", 0)]))
    tables = project_schema_design_tables(CollectorOutputContract.model_validate(output))
    assert tables[0].indexes is not None
    assert tables[0].indexes[0].included_columns == ["Content"]
    pg = generate_pg_ddl(tables, source_engine="sqlserver").tables[0].index_sql
    assert pg == ['CREATE INDEX "IX_Files" ON "Files" ("FolderID") INCLUDE ("Content");']
    mysql = generate_mysql_ddl(tables, source_engine="sqlserver").tables[0].index_sql
    assert mysql == ["CREATE INDEX `IX_Files` ON `Files` (`FolderID`);"]
