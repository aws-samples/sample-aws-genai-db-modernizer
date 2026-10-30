# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Static checks on the row counts the collection scripts and live collectors read (#1636).

Each script reads a table's count from its source's catalogue, never from the table
(no ``COUNT(*)``, no scan, #1555), and must pass on the source's own "no statistics"
instead of turning it into 0, with the count's basis and, where the same catalogue
view records it, when the statistics were refreshed. There is no database to run the
scripts against here, so these are checks on the script text, section by section.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from src.tools.database.mysql_tools import MySQLRemoteCollector
from src.tools.database.oracle_tools import OracleRemoteCollector
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector
from src.tools.database.sqlserver_tools import SQLServerRemoteCollector

SCRIPTS = Path(__file__).resolve().parents[3] / "scripts"


def _tables_section(engine: str) -> str:
    """The script's tables section, from its "Tables" banner to the next section's."""
    sql = (SCRIPTS / f"collect-{engine}.sql").read_text()
    start = re.search(r"=+\s*\n?\s*(--\s*)?\d+\.\s*Tables\b", sql)
    assert start is not None, engine
    end = re.search(r"\d+\.\s*Columns\b", sql[start.end() :])
    assert end is not None, engine
    return sql[start.start() : start.end() + end.start()]


def _code(text: str) -> str:
    """``text`` without its ``--`` comments."""
    return "\n".join(line.split("--", 1)[0] for line in text.splitlines())


@pytest.mark.parametrize(
    "engine",
    [
        "mysql",
        "oracle",
        # PostgreSQL's script never coalesced: its unknown was reltuples' own value.
        pytest.param("postgresql", marks=pytest.mark.passes_on_main),
        "sqlserver",
    ],
)
def test_no_script_coalesces_a_row_count_to_zero(engine: str) -> None:
    code = _code(_tables_section(engine))
    assert not re.search(
        r"(IFNULL|NVL|ISNULL|COALESCE)\s*\(\s*[\w.]*(table_rows|NUM_ROWS|rows|reltuples)",
        code,
        re.IGNORECASE,
    )


@pytest.mark.passes_on_main  # guards #1555: the fix must not add a table read
@pytest.mark.parametrize("engine", ["mysql", "oracle", "postgresql", "sqlserver"])
def test_no_script_reads_a_table_to_count_it(engine: str) -> None:
    code = _code(_tables_section(engine))
    assert "COUNT(" not in code.upper()
    assert "TABLESAMPLE" not in code.upper()


@pytest.mark.parametrize("engine", ["mysql", "oracle", "postgresql", "sqlserver"])
def test_every_script_reports_the_basis(engine: str) -> None:
    assert "row_count_basis" in _code(_tables_section(engine))


def test_postgresql_passes_on_its_own_unknown() -> None:
    code = _code(_tables_section("postgresql"))
    # 14+: reltuples -1 for a table never vacuumed or analysed.
    assert "WHEN c.reltuples < 0 THEN NULL" in code
    # Before 14: reltuples 0 and relpages 0 with no analyse or vacuum on record.
    assert "c.reltuples = 0 AND c.relpages = 0" in code
    assert "current_setting('server_version_num')::int < 140000" in code
    for column in ("last_analyze", "last_autoanalyze", "last_vacuum", "last_autovacuum"):
        assert f"s.{column} IS NULL" in code
    assert "LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid" in code
    assert "'estimate' AS row_count_basis" in code
    assert re.search(r"greatest\(s\.last_analyze,.*\)\s*AS stats_analyzed_at", code, re.DOTALL)


def test_oracle_passes_num_rows_and_last_analyzed_through() -> None:
    code = _code(_tables_section("oracle"))
    assert "'row_count' VALUE t.NUM_ROWS," in code
    assert "'row_count_basis' VALUE 'estimate'" in code
    assert "'stats_analyzed_at' VALUE TO_CHAR(t.LAST_ANALYZED" in code
    # A table's size sums its segments, partitions too; 0 only for a deferred segment.
    assert "NVL(s.bytes" not in code
    assert "SEGMENT_TYPE IN ('TABLE', 'TABLE PARTITION', 'TABLE SUBPARTITION')" in code
    assert "WHEN t.SEGMENT_CREATED = 'NO' THEN 0" in code


def test_mysql_passes_table_rows_through_with_the_storage_engines_basis() -> None:
    code = _code(_tables_section("mysql"))
    assert "'row_count', table_rows," in code
    assert "IF(engine IN ('MyISAM', 'Aria'), 'exact', 'estimate')" in code


def test_sql_server_sums_each_tables_partitions_as_an_estimate() -> None:
    code = _code(_tables_section("sqlserver"))
    # Microsoft documents sys.partitions.rows as approximate.
    assert "'estimate' AS row_count_basis" in code
    assert "'exact'" not in code
    # One row per table: its partitions' rows summed before the allocation-units join,
    # never grouped by a partition's own count.
    assert "SELECT object_id, SUM(rows) AS row_total" in code
    assert "r.row_total AS row_count," in code
    assert "GROUP BY s.name, t.name, r.row_total" in code
    assert "p.rows" not in code


# --------------------------------------------------------------------------- live collectors


def _live_sql(cls: Any, *args: object) -> str:
    seen: list[str] = []

    def query(self: object, sql: str) -> list[dict]:
        seen.append(sql)
        return []

    collector = cls.__new__(cls)
    collector._query = query.__get__(collector)  # type: ignore[attr-defined]
    cls.collect_tables(collector, *args)
    return seen[0]


def test_live_postgresql_reads_the_same_catalogue_estimate() -> None:
    sql = _live_sql(PostgreSQLRemoteCollector)
    assert "n_live_tup" not in sql
    assert "WHEN c.reltuples < 0 THEN NULL" in sql
    assert "current_setting('server_version_num')::int < 140000" in sql
    assert "JOIN pg_class c ON c.oid = s.relid" in sql
    assert "AS stats_analyzed_at" in sql
    assert "'estimate' AS row_count_basis" in sql


def test_live_oracle_reads_num_rows_and_last_analyzed() -> None:
    sql = _live_sql(OracleRemoteCollector, "APP")
    assert "t.NUM_ROWS AS row_count" in sql
    assert "TO_CHAR(t.LAST_ANALYZED" in sql
    assert "'estimate' AS row_count_basis" in sql
    assert "TABLE PARTITION" in sql


def test_live_mysql_and_sql_server_report_their_basis() -> None:
    assert "IF(engine IN ('MyISAM', 'Aria'), 'exact', 'estimate')" in _live_sql(
        MySQLRemoteCollector
    )
    sql = _live_sql(SQLServerRemoteCollector)
    assert "'estimate' AS row_count_basis" in sql
    assert "SELECT object_id, SUM(rows) AS row_total" in sql
    assert "GROUP BY s.name, t.name, r.row_total" in sql
