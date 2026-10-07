"""Static checks on scripts/collect-sqlserver.sql's query-patterns section.

Regression for #386: sys.dm_exec_query_stats keeps one row per cached
statement, so a non-parameterized query gets a row per literal value, all
sharing the same query_hash. The script must group by query_hash so one row
== one query shape (matching the queryid/DIGEST grouping already done for
PostgreSQL and MySQL), summing/min/max-ing the per-variant counters, and
apply TOP/the execution_count floor after grouping.

There is no SQL Server to run the script against here, so these are string
checks on the script text rather than an execution test.
"""

from __future__ import annotations

from pathlib import Path


def _script_text() -> str:
    repo = Path(__file__).resolve().parents[3]
    path = repo / "scripts" / "collect-sqlserver.sql"
    return path.read_text()


def test_groups_by_query_hash() -> None:
    sql = _script_text()
    assert "GROUP BY v.query_hash" in sql


def test_sums_the_counters() -> None:
    sql = _script_text()
    assert "SUM(v.execution_count) AS execution_count" in sql
    assert "SUM(v.total_elapsed_time) AS total_elapsed_time" in sql
    assert "SUM(v.total_worker_time) AS total_worker_time" in sql
    assert "SUM(v.total_rows) AS total_rows" in sql
    assert "SUM(v.total_logical_reads) AS total_logical_reads" in sql
    assert "SUM(v.total_physical_reads) AS total_physical_reads" in sql


def test_min_max_the_timestamps_and_extremes() -> None:
    sql = _script_text()
    assert "MIN(v.min_elapsed_time) AS min_elapsed_time" in sql
    assert "MAX(v.max_elapsed_time) AS max_elapsed_time" in sql
    assert "MIN(v.creation_time) AS creation_time" in sql
    assert "MAX(v.last_execution_time) AS last_execution_time" in sql


def test_keeps_one_sample_text_from_highest_elapsed_variant() -> None:
    sql = _script_text()
    assert "ROW_NUMBER() OVER (" in sql
    assert "PARTITION BY qs.query_hash" in sql
    # Stable tiebreak so the sample text is deterministic when two variants
    # have identical elapsed time (#386 review).
    assert "ORDER BY qs.total_elapsed_time DESC, qs.plan_handle, qs.statement_start_offset" in sql
    assert "MAX(CASE WHEN v.variant_rank = 1 THEN v.query_text END) AS query_text" in sql


def test_top_and_execution_count_floor_apply_after_grouping() -> None:
    sql = _script_text()
    # The outer SELECT TOP applies to the grouped alias `g`, after the
    # GROUP BY query_hash subquery — not to the raw per-variant rows.
    assert "SELECT TOP 1000" in sql
    assert "WHERE g.execution_count >= 10" in sql
    # Field names in the FOR JSON PATH output must stay stable so the
    # offline parser keeps working unchanged.
    assert "AS digest" in sql
    assert "FOR JSON PATH" in sql


def test_output_field_names_unchanged() -> None:
    sql = _script_text()
    for field in (
        "digest",
        "query_text",
        "execution_count",
        "total_time_ms",
        "avg_time_ms",
        "min_time_ms",
        "max_time_ms",
        "total_rows_sent",
        "total_rows_examined",
        "total_rows_affected",
        "total_cpu_ms",
        "avg_cpu_time_ms",
        "avg_logical_reads",
        "avg_physical_reads",
        "first_seen",
        "last_seen",
    ):
        assert f"AS {field}" in sql, f"missing output field: {field}"
