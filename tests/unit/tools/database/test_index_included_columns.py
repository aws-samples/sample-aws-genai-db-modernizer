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
from src.contracts.collector_output import CollectorOutputContract
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


def _column(name: str, data_type: str, max_length: int | None) -> dict[str, Any]:
    return {
        "table_name": "Files",
        "schema_name": "dbo",
        "column_name": name,
        "data_type": data_type,
        "max_length": max_length,
        "is_nullable": "YES",
    }


def _offline(index_rows: list[dict[str, Any]], primary_key: tuple[str, ...] = ()) -> dict[str, Any]:
    return {
        "metadata": {"database_name": "db", "engine": "sqlserver", "version": "16.0.4305.5"},
        "tables": [{"table_name": "Files", "schema_name": "dbo", "row_count": 10}],
        "columns": [
            _column("FolderID", "int", 4),
            _column("FileName", "nvarchar", 246),
            _column("Content", "nvarchar", 3750),
            _column("Path", "nvarchar", 1000),
            _column("Body", "nvarchar", None),
        ],
        "indexes": index_rows,
        "foreign_keys": [],
        "primary_keys": [
            {"table_name": "Files", "schema_name": "dbo", "column_name": c, "ordinal_position": n}
            for n, c in enumerate(primary_key, start=1)
        ],
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
        # the partitioning column SQL Server adds to a non-unique index: stored, not keyed
        ({"is_included_column": 0, "key_ordinal": 0}, True),
        ({"is_included_column": 1}, True),
        ({"is_included_column": 0}, False),
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


def test_the_live_collector_splits_on_key_ordinal_as_the_offline_path_does() -> None:
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
        for name, ordinal, included in [
            ("FolderID", 1, 0),
            ("FileName", 2, 0),
            ("Content", 0, 1),
            # the partitioning column of a partitioned table: key_ordinal 0, not INCLUDE
            ("Path", 0, 0),
        ]
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
    assert "ic.key_ordinal" in ssm.run_sql_json.call_args.kwargs["sql"]
    assert index["columns"] == ["FolderID", "FileName"]
    assert index["included_columns"] == ["Content", "Path"]
    # the offline path reads the same rows the same way
    rows = [_row("FolderID", 1), _row("FileName", 2), _row("Content", 0), _row("Path", 0)]
    [table] = parse_offline_collection(_offline(rows))["tables"]
    [offline] = table["indexes"]
    assert (offline["columns"], offline["included_columns"]) == (
        index["columns"],
        index["included_columns"],
    )

    [table] = _build_tables(
        {
            "tables": [
                {
                    "schema_name": "dbo",
                    "table_name": "Files",
                    "row_count": 1,
                    "columns": [
                        {"column_name": c, "data_type": "int", "is_nullable": "YES"}
                        for c in ("FolderID", "FileName", "Content", "Path")
                    ],
                    "indexes": [index],
                }
            ]
        }
    )
    assert table.indexes is not None
    assert table.indexes[0].included_columns == ["Content", "Path"]


def test_the_offline_path_carries_included_columns_onto_the_contract() -> None:
    output = _collector_output(_offline([_row("FolderID", 1), _row("Content", 0)]))
    [table] = output["database_schema"]["tables"]
    [index] = table["indexes"]
    assert (index["columns"], index["included_columns"]) == (["FolderID"], ["Content"])


def test_a_collector_output_saved_before_2227_still_loads() -> None:
    """A real collector output written by the collector before #2227 (origin/main's, on a
    small synthetic SQL Server collection): it has no included_columns and keeps the
    INCLUDE column in ``columns``, as it did. It still loads, projects and drafts."""
    from src.tools.schema.aurora_common.ddl_generator import generate_pg_ddl

    saved = _ROOT / "tests" / "fixtures" / "old_collector_outputs"
    data = json.loads((saved / "sqlserver-include-before-2227.json").read_text())
    [raw] = data["database_schema"]["tables"][0]["indexes"]
    assert "included_columns" not in raw and raw["columns"] == ["FolderID", "Content"]
    contract = CollectorOutputContract.model_validate(data)
    assert contract.database_schema.tables[0].indexes is not None
    [index] = contract.database_schema.tables[0].indexes
    assert index.included_columns is None and index.columns == ["FolderID", "Content"]
    tables = project_schema_design_tables(contract)
    assert generate_mysql_ddl(tables, source_engine="sqlserver").tables[0].index_sql
    assert generate_pg_ddl(tables, source_engine="sqlserver").tables[0].index_sql
    assert detect_relational_gaps(data, "aurora_mysql") is not None


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


def test_aurora_mysql_notes_the_include_columns_it_drops_and_aurora_postgresql_does_not() -> None:
    """DNN's IX_UserProfile_PropertyDefinitionID shape: an int key that INCLUDEs text. On
    Aurora MySQL the index no longer covers the queries that read those columns: say so."""
    from src.tools.schema.aurora_common.ddl_generator import generate_pg_ddl

    rows = [_row("FolderID", 1), _row("Content", 0), _row("FileName", 0)]
    tables = project_schema_design_tables(
        CollectorOutputContract.model_validate(_collector_output(_offline(rows)))
    )
    mysql = generate_mysql_ddl(tables, source_engine="sqlserver").index_notes
    [note] = [n for n in mysql if n["index"] == "IX_Files"]
    assert note["table"] == "Files"
    for text in ("Content, FileName", "INCLUDE", "trailing key columns", "extra lookups"):
        assert text in note["reason"], text
    pg = generate_pg_ddl(tables, source_engine="sqlserver").index_notes
    # Aurora PostgreSQL keeps INCLUDE: nothing dropped (its long-row note is tested below)
    assert not [n for n in pg if n["index"] == "IX_Files" and "not carried over" in n["reason"]]
    # an index with no INCLUDE columns gets no such note
    plain = project_schema_design_tables(
        CollectorOutputContract.model_validate(_collector_output(_offline([_row("FolderID", 1)])))
    )
    assert generate_mysql_ddl(plain, source_engine="sqlserver").index_notes == []


def _drafts(
    rows: list[dict[str, Any]],
    primary_key: tuple[str, ...] = (),
    index_type: str | None = None,
) -> tuple[Any, Any]:
    from src.tools.schema.aurora_common.ddl_generator import generate_pg_ddl

    if index_type:
        rows = [{**r, "index_type": index_type} for r in rows]
    output = _collector_output(_offline(rows, primary_key))
    tables = project_schema_design_tables(CollectorOutputContract.model_validate(output))
    return (
        generate_mysql_ddl(tables, source_engine="sqlserver"),
        generate_pg_ddl(tables, source_engine="sqlserver"),
    )


def _notes(ddl: Any, index: str = "IX_Files") -> list[str]:
    return [n["reason"] for n in ddl.index_notes if n["index"] == index]


@pytest.mark.parametrize(
    ("include", "named"),
    [
        # DNN's Users.IX_Users_DisplayName INCLUDE (UserID): the key InnoDB stores anyway
        (("FolderID",), None),
        # a mixed INCLUDE names only the column the index no longer covers
        (("FolderID", "Content"), "Content"),
        (("Content", "FolderID"), "Content"),
    ],
)
def test_aurora_mysql_never_names_a_primary_key_column_as_lost_coverage(
    include: tuple[str, ...], named: str | None
) -> None:
    """Every InnoDB secondary index stores the primary key, so a primary-key INCLUDE column
    stays covered on Aurora MySQL."""
    rows = [_row("FileName", 1)] + [_row(c, 0) for c in include]
    mysql, _ = _drafts(rows, primary_key=("FolderID",))
    notes = [n for n in _notes(mysql) if n.startswith("Non-key columns")]
    if named is None:
        assert notes == []
    else:
        assert [n.split(" not carried over")[0] for n in notes] == [f"Non-key columns {named}"]


@pytest.mark.parametrize("index_type", ["NONCLUSTERED COLUMNSTORE", "CLUSTERED COLUMNSTORE"])
def test_the_live_collector_reads_a_columnstore_index_as_columnstore(index_type: str) -> None:
    from src.tools.database.sqlserver_tools import _normalize_index_type

    assert _normalize_index_type(index_type) == "columnstore"
    assert _normalize_index_type("NONCLUSTERED") == "btree"


def test_the_offline_script_writes_columnstore_before_its_btree_case() -> None:
    script = (_ROOT / "scripts" / "collect-sqlserver.sql").read_text()
    columnstore = script.index("WHEN i.type_desc LIKE '%COLUMNSTORE%' THEN 'columnstore'")
    assert columnstore < script.index("WHEN i.type_desc LIKE '%CLUSTERED%'")


def test_a_columnstore_index_is_no_key_and_neither_draft_builds_one() -> None:
    """Every row of a columnstore index has key_ordinal 0: it keeps its columns, is typed
    columnstore, raises no key-length gap and both drafts leave it out with a note."""
    rows = [_row("Path", 0, "CCI_Files"), _row("Content", 0, "CCI_Files")]
    output = _collector_output(_offline([{**r, "index_type": "columnstore"} for r in rows]))
    [index] = output["database_schema"]["tables"][0]["indexes"]
    assert (index["index_type"], index["columns"]) == ("columnstore", ["Path", "Content"])
    result = detect_relational_gaps(output, "aurora_mysql")
    assert _KEY_LENGTH not in {g["rule_id"] for g in (result or {})["gaps"]}
    mysql, pg = _drafts(rows, index_type="columnstore")
    for ddl in (mysql, pg):
        assert not any("CCI_Files" in sql for sql in ddl.tables[0].index_sql)
        [note] = _notes(ddl, "CCI_Files")
        assert note.startswith("COLUMNSTORE index not carried over") and "zero-ETL" in note


def test_a_columnstore_index_is_not_a_routing_key() -> None:
    from src.agents.referee.access_pattern import _table_keys

    table = {
        "primary_key": ["id"],
        "columns": [{"column_name": "id"}, {"column_name": "notes"}],
        "indexes": [{"index_name": "CCI", "columns": ["notes", "id"], "index_type": "columnstore"}],
    }
    assert "notes" not in _table_keys(table).key_columns


def test_a_unique_index_key_leaves_its_include_columns_out_for_routing() -> None:
    """Routing reads unique keys from Index.columns: an INCLUDE column no longer widens one."""
    from src.agents.referee.access_pattern import _table_keys

    output = _collector_output(_offline([_row("FileName", 1), _row("Content", 0)]))
    [table] = output["database_schema"]["tables"]
    table["indexes"][0]["is_unique"] = True
    assert frozenset({"filename"}) in _table_keys(table).unique_keys
    assert frozenset({"filename", "content"}) not in _table_keys(table).unique_keys


@pytest.mark.parametrize(
    ("include", "named"),
    [
        (("Body",), "Body"),  # nvarchar(max): TEXT, no bound
        (("Path",), "Path"),  # nvarchar(1000): 4,000 bytes
        (("FileName",), None),  # nvarchar(246): 984 bytes with the key
    ],
)
def test_aurora_postgresql_names_long_include_columns(
    include: tuple[str, ...], named: str | None
) -> None:
    rows = [_row("FolderID", 1)] + [_row(c, 0) for c in include]
    _, pg = _drafts(rows)
    assert pg.tables[0].index_sql[0].endswith(f'INCLUDE ("{include[0]}");')
    notes = _notes(pg)
    if named is None:
        assert notes == []
    else:
        [note] = notes
        assert note.startswith(f"INCLUDE columns {named} can make an index row longer than")
        assert "2704 bytes" in note


def test_aurora_postgresql_notes_the_include_of_an_index_left_out_as_a_primary_key_copy() -> None:
    """DNN's Folders.IX_Folders_FolderID: unique on the primary key, with INCLUDE columns."""
    rows = [{**r, "non_unique": 0} for r in (_row("FolderID", 1), _row("FileName", 0))]
    mysql, pg = _drafts(rows, primary_key=("FolderID",))
    [note] = _notes(pg)
    assert note.startswith("Index left out as a copy of the primary key") and "FileName" in note
    assert _notes(mysql) == []  # the InnoDB primary key is the table: it covers every column


_DESIGN_COLUMNS: list[tuple[str, str, str, dict[str, Any]]] = [
    ("id", "int", "integer", {"nullable": False}),
    ("name", "nvarchar", "string", {"max_length": 50, "nullable": True}),
    ("size", "int", "integer", {"nullable": True}),
]


def _design_input(engine: str) -> tuple[Any, Any]:
    from datetime import UTC, datetime

    from src.contracts.schema_design_input import AgentCollectorInput
    from src.tools.schema.aurora_common.delta_merge import AuroraDesignBase

    collector = AgentCollectorInput.model_validate(
        {
            "contract_version": "3.0.0",
            "job_id": "j",
            "source_database_name": "db",
            "source_database_engine": "sqlserver",
            "collection_timestamp": datetime(2026, 1, 1, tzinfo=UTC),
            "tables": [
                {
                    "table_id": "db.Files",
                    "table_name": "Files",
                    "row_count": 10,
                    "primary_key": ["id"],
                    "columns": [
                        {"column_name": n, "data_type": t, "normalized_data_type": k, **x}
                        for n, t, k, x in _DESIGN_COLUMNS
                    ],
                    "indexes": [
                        {
                            "index_name": "IX_Files_Name",
                            "columns": ["name"],
                            "included_columns": ["size"],
                            "is_unique": False,
                        }
                    ],
                }
            ],
            "queries": {"query_patterns": []},
        }
    )
    return AuroraDesignBase.from_inputs(engine, collector), collector


def test_the_design_view_shows_the_include_the_aurora_postgresql_draft_renders() -> None:
    from src.tools.schema.aurora_common.design_view import build_design_view

    base, collector = _design_input("aurora_postgresql")
    [line] = build_design_view(base, collector)["tables"][0]["indexes"]
    assert line == "IX_Files_Name (name) INCLUDE (size)"
    base, collector = _design_input("aurora_mysql")
    [line] = build_design_view(base, collector)["tables"][0]["indexes"]
    assert line == "IX_Files_Name (name)"


@pytest.mark.parametrize(
    ("change", "rendered"),
    [
        # an entry replaces the whole definition, as the skill says: no include, none kept
        ({"columns": ["name", "id"]}, 'CREATE INDEX "IX_Files_Name" ON "Files" ("name", "id");'),
        # include [] removes the source's INCLUDE (the long-INCLUDE note asks for that)
        (
            {"columns": ["name"], "include": []},
            'CREATE INDEX "IX_Files_Name" ON "Files" ("name");',
        ),
        # repeating it, as the design view shows it, keeps it
        (
            {"columns": ["name"], "include": ["size"]},
            'CREATE INDEX "IX_Files_Name" ON "Files" ("name") INCLUDE ("size");',
        ),
        # an explicit include wins
        (
            {"columns": ["name"], "include": ["id"]},
            'CREATE INDEX "IX_Files_Name" ON "Files" ("name") INCLUDE ("id");',
        ),
        # a column the model moved into the key is not repeated in INCLUDE
        (
            {"columns": ["name", "size"], "include": ["size", "id"]},
            'CREATE INDEX "IX_Files_Name" ON "Files" ("name", "size") INCLUDE ("id");',
        ),
    ],
)
def test_a_modified_index_has_the_include_its_entry_names_on_aurora_postgresql(
    change: dict[str, Any], rendered: str
) -> None:
    from src.tools.schema.aurora_common.delta_merge import merge_design_delta

    base, _ = _design_input("aurora_postgresql")
    delta = {
        "delta_version": "1.0",
        "tables": [
            {
                "table_name": "Files",
                "modify_indexes": [{"index_name": "IX_Files_Name", "unique": False, **change}],
            }
        ],
    }
    result = merge_design_delta(base, delta)
    assert result.errors == []
    assert result.output["table_definitions"][0]["indexes"] == [rendered]
