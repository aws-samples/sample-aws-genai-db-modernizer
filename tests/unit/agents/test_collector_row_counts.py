"""Every collector path keeps a row count unknown when the source has none (#1636).

Before #1636 the offline builder clamped every count with ``max(row_count or 0, 0)``,
the live collectors with ``or 0``, and a DDL upload wrote 0: an unknown table reached
every figure as an empty one. Each path now reads the count, and the size, by the one
per-engine rule (``src.shared.row_counts``).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src.agents.collector import oracle_collector, postgres_collector, sqlserver_collector
from src.agents.collector.mysql_collector import _build_tables, _build_tables_from_ddl
from src.agents.collector.offline_ingest import build_offline_collector_output
from src.tools.database.collection_text import extract_json_object
from src.tools.database.ddl_parser import parse_ddl
from src.tools.database.offline_parser import parse_offline_collection

_REPO = Path(__file__).resolve().parents[3]
_COLUMN = {"column_name": "id", "ordinal_position": 1, "data_type": "int", "is_nullable": "NO"}


def _raw(**fields: object) -> dict:
    return {"table_name": "t", "columns": [dict(_COLUMN)], **fields}


def _sample(name: str) -> dict:
    with zipfile.ZipFile(_REPO / "docs" / "examples" / name / f"{name}.zip") as z:
        member = next(n for n in z.namelist() if n.endswith("-collection.json"))
        data: dict = json.loads(extract_json_object(z.read(member).decode("utf-8")))
    return data


def _offline(name: str) -> dict:
    return build_offline_collector_output(
        parse_offline_collection(_sample(name)), job_id="j", database_name=name, s3_key="k"
    )


# --------------------------------------------------------------------------- offline builder


@pytest.mark.parametrize(
    ("engine", "raw", "expected"),
    [
        # A collection written since #1636: null is unknown, 0 is an empty table.
        ("postgresql", _raw(row_count=None, row_count_basis="estimate"), (None, None)),
        ("postgresql", _raw(row_count=0, row_count_basis="estimate"), (0, "estimate")),
        ("oracle", _raw(row_count=None, row_count_basis="estimate"), (None, None)),
        ("mysql", _raw(row_count=0, row_count_basis="exact", engine="MyISAM"), (0, "exact")),
        ("sqlserver", _raw(row_count=0, row_count_basis="estimate"), (0, "estimate")),
        # One written before: PostgreSQL 14+'s -1, and the 0 that hid an unknown.
        ("postgresql", _raw(row_count=-1), (None, None)),
        ("postgresql", _raw(row_count=0), (None, None)),
        ("oracle", _raw(row_count=0), (None, None)),
        # The MySQL family's 0: an empty one-page InnoDB table, or stale statistics.
        ("mysql", _raw(row_count=0, engine="InnoDB", data_size_mb=0.02), (0, "estimate")),
        ("mariadb", _raw(row_count=0, engine="InnoDB", data_size_mb=0.02), (0, "estimate")),
        ("mysql", _raw(row_count=0, engine="InnoDB", data_size_mb=48.0), (None, None)),
        ("mysql", _raw(row_count=0, engine="MyISAM"), (0, "exact")),
        ("sqlserver", _raw(row_count=0), (0, "estimate")),
        ("db2", _raw(row_count=-1), (None, None)),
        ("db2", _raw(row_count=0), (0, "estimate")),
        ("mysql", _raw(row_count=4521, engine="InnoDB"), (4521, "estimate")),
    ],
)
def test_the_offline_builder_reads_each_count_by_its_engines_rule(
    engine: str, raw: dict, expected: tuple
) -> None:
    [table] = _build_tables([raw], "db", source_engine=engine)
    assert (table.row_count, table.row_count_basis) == expected


def test_the_statistics_time_reaches_the_collector_output() -> None:
    raw = _raw(row_count=10, row_count_basis="estimate", stats_analyzed_at="2026-10-20T08:15:00")
    [table] = _build_tables([raw], "db", source_engine="oracle")
    assert table.stats_analyzed_at is not None
    assert table.stats_analyzed_at.isoformat() == "2026-10-20T08:15:00"


@pytest.mark.parametrize(
    ("engine", "raw", "size"),
    [
        ("postgresql", _raw(data_size_mb=1.0, index_size_mb=0.5), 1.5),
        ("postgresql", _raw(data_size_mb=0, index_size_mb=0), 0.0),
        ("mysql", _raw(index_size_mb=0.5), None),
        ("oracle", _raw(data_size_mb=None, row_count_basis="estimate"), None),
        ("oracle", _raw(data_size_mb=0, row_count_basis="estimate"), 0.0),
        ("oracle", _raw(data_size_mb=0), None),  # before #1636: no TABLE segment found
    ],
)
def test_the_offline_builder_reads_each_size_by_the_same_rule(
    engine: str, raw: dict, size: float | None
) -> None:
    [table] = _build_tables([raw], "db", source_engine=engine)
    assert table.size_mb == size
    # The data size travels apart: the row-count assumption divides it alone.
    assert table.data_size_mb == (None if size is None else raw.get("data_size_mb"))


def test_an_old_postgresql_zero_reads_by_the_collections_version() -> None:
    [old_13] = _build_tables(
        [_raw(row_count=0)], "db", source_engine="postgresql", source_version="PostgreSQL 13.4"
    )
    [old_16] = _build_tables(
        [_raw(row_count=0)], "db", source_engine="postgresql", source_version="PostgreSQL 16.1"
    )
    assert (old_13.row_count, old_16.row_count) == (None, 0)


def test_the_discourse_samples_unknown_counts_stay_unknown() -> None:
    raw = {t["table_name"]: t["row_count"] for t in _sample("discourse")["tables"]}
    out = _offline("discourse")
    tables = out["database_schema"]["tables"]
    unknown = [t for t in tables if t["row_count"] is None]
    assert len(tables) == 311 and len(unknown) == 265
    assert all(raw[t["table_name"]] == -1 for t in unknown)
    known = [t for t in tables if t["row_count"] is not None]
    assert all(t["row_count"] == raw[t["table_name"]] > 0 for t in known)
    assert {t["row_count_basis"] for t in known} == {"estimate"}
    assert {t["row_count_basis"] for t in unknown} == {None}


@pytest.mark.passes_on_main  # main already kept these zeros (as it kept every unknown)
def test_the_wordpress_samples_one_page_zeros_are_empty_tables() -> None:
    # Each of wordpress's 31 InnoDB zeros is a one-page (0.02 MB) table: empty, not
    # unknown, so no deliverable may say its count is missing.
    raw = {t["table_name"]: t for t in _sample("wordpress")["tables"]}
    tables = _offline("wordpress")["database_schema"]["tables"]
    zeros = [t for t in tables if t["row_count"] == 0]
    assert len(zeros) == 31
    assert all(raw[t["table_name"]]["data_size_mb"] <= 0.02 for t in zeros)
    assert not [t for t in tables if t["row_count"] is None]


@pytest.mark.passes_on_main  # SQL Server's counts were already right
def test_the_trailgear_samples_exact_counts_are_kept() -> None:
    raw = {t["table_name"]: t["row_count"] for t in _sample("trailgear")["tables"]}
    tables = _offline("trailgear")["database_schema"]["tables"]
    assert {t["table_name"]: t["row_count"] for t in tables} == raw


# --------------------------------------------------------------------------- live collectors


@pytest.mark.parametrize(
    ("module", "engine"),
    [
        (postgres_collector, "postgresql"),
        (sqlserver_collector, "sqlserver"),
        (oracle_collector, "oracle"),
    ],
)
def test_the_live_collectors_keep_a_null_count_unknown(module, engine: str) -> None:
    unknown = _raw(row_count=None, row_count_basis="estimate", data_size_mb=None)
    empty = {**_raw(row_count=0, row_count_basis="exact", data_size_mb=0), "table_name": "e"}
    tables = module._build_tables({"tables": [unknown, empty]})
    by_name = {t.table_name: t for t in tables}
    assert (by_name["t"].row_count, by_name["t"].size_mb) == (None, None)
    assert (by_name["e"].row_count, by_name["e"].size_mb) == (0, 0.0)


# --------------------------------------------------------------------------- DDL


def test_a_ddl_upload_has_no_counts_or_sizes() -> None:
    parsed = parse_ddl("CREATE TABLE t (id INT PRIMARY KEY);", "db")
    assert all(t["row_count"] is None and t["data_size_mb"] is None for t in parsed)
    [table] = _build_tables_from_ddl(parsed, "db")
    assert (table.row_count, table.size_mb, table.row_count_basis) == (None, None, None)
