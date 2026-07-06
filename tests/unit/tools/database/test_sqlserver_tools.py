"""Tests for SQLServerRemoteCollector and helpers.

Mocks SSMExecutor so no AWS calls are made. Each method is tested for:
  - Correct SQL is dispatched (engine="sqlserver")
  - Returned shape matches the contract expectations
  - SQL Server-specific behaviors (TOP N vs LIMIT N, sys catalog filter, etc.)
"""

from unittest.mock import MagicMock

import pytest

from src.tools.aws.ssm_executor import _parse_tabular_output
from src.tools.database.sqlserver_tools import (
    SQLServerRemoteCollector,
    _estimate_percentiles_from_min_max,
    _estimate_rows_p95,
    _extract_filter_columns,
    _extract_query_type,
    _extract_sort_columns,
    _extract_tables,
    _has_text_search,
    _has_time_range_filter,
    _hash,
    _text_search_type,
)


@pytest.fixture
def mock_ssm() -> MagicMock:
    return MagicMock()


@pytest.fixture
def collector(mock_ssm: MagicMock) -> SQLServerRemoteCollector:
    return SQLServerRemoteCollector(
        ssm=mock_ssm,
        host="sqlserver-test.example.com",
        port=1433,
        database="testdb",
        secret_arn="arn:aws:secretsmanager:us-east-1:123456789012:secret:test-AAAAAA",  # pragma: allowlist secret
        region="us-east-1",
    )


# ---------------------------------------------------------------------
# SSM dispatch
# ---------------------------------------------------------------------


class TestSSMDispatch:
    def test_query_dispatches_with_sqlserver_engine(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql_json.return_value = []
        collector._query("SELECT 1")
        kwargs = mock_ssm.run_sql_json.call_args.kwargs
        assert kwargs["engine"] == "sqlserver"
        assert kwargs["host"] == "sqlserver-test.example.com"
        assert kwargs["port"] == 1433
        assert kwargs["database"] == "testdb"
        assert kwargs["region"] == "us-east-1"

    def test_query_raw_uses_run_sql(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql.return_value = "raw output"
        result = collector._query_raw("SELECT 1")
        assert result == "raw output"
        assert mock_ssm.run_sql.call_args.kwargs["engine"] == "sqlserver"


# ---------------------------------------------------------------------
# Schema methods
# ---------------------------------------------------------------------


class TestSchemaCollection:
    def test_get_version_returns_string(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql_json.return_value = [{"version": "Microsoft SQL Server 2022 ..."}]
        assert "SQL Server" in collector.get_version()

    def test_get_version_falls_back_when_empty(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql_json.return_value = []
        assert collector.get_version() == "unknown"

    def test_get_database_size_gb(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql_json.return_value = [{"size_gb": 12.5}]
        assert collector.get_database_size_gb() == 12.5

    def test_get_database_size_gb_handles_none(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql_json.return_value = [{"size_gb": None}]
        assert collector.get_database_size_gb() == 0

    def test_collect_tables_excludes_system_schemas(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql_json.return_value = []
        collector.collect_tables()
        sent_sql = mock_ssm.run_sql_json.call_args.kwargs["sql"]
        # The filter constant must be applied
        assert "NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest')" in sent_sql
        assert "NOT LIKE 'db[_]%'" in sent_sql
        # Hits sys.tables (not INFORMATION_SCHEMA or sys.objects)
        assert "FROM sys.tables" in sent_sql

    def test_collect_columns_qualified_name(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql_json.return_value = []
        collector.collect_columns("dbo.Customers")
        sent_sql = mock_ssm.run_sql_json.call_args.kwargs["sql"]
        assert "s.name = 'dbo'" in sent_sql
        assert "t.name = 'Customers'" in sent_sql

    def test_collect_columns_default_schema_is_dbo(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql_json.return_value = []
        collector.collect_columns("OrdersTable")
        sent_sql = mock_ssm.run_sql_json.call_args.kwargs["sql"]
        assert "s.name = 'dbo'" in sent_sql
        assert "t.name = 'OrdersTable'" in sent_sql

    def test_collect_indexes_groups_by_index_name(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql_json.return_value = [
            {
                "index_name": "PK_Customers",
                "column_name": "id",
                "key_ordinal": 1,
                "is_unique": 1,
                "is_primary_key": 1,
                "index_type": "CLUSTERED",
            },
            {
                "index_name": "IX_Customers_Email",
                "column_name": "email",
                "key_ordinal": 1,
                "is_unique": 1,
                "is_primary_key": 0,
                "index_type": "NONCLUSTERED",
            },
            {
                "index_name": "IX_Customers_NameAge",
                "column_name": "name",
                "key_ordinal": 1,
                "is_unique": 0,
                "is_primary_key": 0,
                "index_type": "NONCLUSTERED",
            },
            {
                "index_name": "IX_Customers_NameAge",
                "column_name": "age",
                "key_ordinal": 2,
                "is_unique": 0,
                "is_primary_key": 0,
                "index_type": "NONCLUSTERED",
            },
        ]
        result = collector.collect_indexes("dbo.Customers")
        assert len(result) == 3
        compound = next(idx for idx in result if idx["index_name"] == "IX_Customers_NameAge")
        assert compound["columns"] == ["name", "age"]
        assert compound["is_unique"] is False
        assert compound["is_primary"] is False
        pk = next(idx for idx in result if idx["index_name"] == "PK_Customers")
        assert pk["is_primary"] is True

    def test_collect_indexes_handles_string_bool_values(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql_json.return_value = [
            {
                "index_name": "IX_Test",
                "column_name": "col1",
                "key_ordinal": 1,
                "is_unique": "True",
                "is_primary_key": "false",
                "index_type": "NONCLUSTERED",
            }
        ]
        result = collector.collect_indexes("dbo.Test")
        assert result[0]["is_unique"] is True
        assert result[0]["is_primary"] is False

    def test_collect_foreign_keys_groups_by_constraint(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql_json.return_value = [
            {
                "constraint_name": "FK_Orders_CustOrder",
                "column_name": "customer_id",
                "referenced_table_name": "Customers",
                "referenced_column_name": "id",
                "on_update": "CASCADE",
                "on_delete": "NO_ACTION",
                "constraint_column_id": 1,
            },
            {
                "constraint_name": "FK_Orders_CustOrder",
                "column_name": "order_seq",
                "referenced_table_name": "Customers",
                "referenced_column_name": "seq",
                "on_update": "CASCADE",
                "on_delete": "NO_ACTION",
                "constraint_column_id": 2,
            },
        ]
        result = collector.collect_foreign_keys("dbo.Orders")
        assert len(result) == 1
        assert result[0]["columns"] == ["customer_id", "order_seq"]
        assert result[0]["referenced_columns"] == ["id", "seq"]
        assert result[0]["referenced_table"] == "Customers"

    def test_collect_primary_key_returns_columns_in_order(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql_json.return_value = [
            {"column_name": "tenant_id"},
            {"column_name": "user_id"},
        ]
        assert collector.collect_primary_key("dbo.Users") == ["tenant_id", "user_id"]

    def test_collect_views_excludes_system_schemas(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql_json.return_value = []
        collector.collect_views()
        sent_sql = mock_ssm.run_sql_json.call_args.kwargs["sql"]
        assert "NOT IN ('sys', 'INFORMATION_SCHEMA', 'guest')" in sent_sql
        assert "FROM sys.views" in sent_sql

    def test_collect_procedures_filters_to_routines(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql_json.return_value = []
        collector.collect_procedures()
        sent_sql = mock_ssm.run_sql_json.call_args.kwargs["sql"]
        assert "o.type IN ('P', 'FN', 'IF', 'TF')" in sent_sql

    def test_collect_triggers_uses_object_property(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql_json.return_value = []
        collector.collect_triggers()
        sent_sql = mock_ssm.run_sql_json.call_args.kwargs["sql"]
        assert "OBJECTPROPERTY" in sent_sql
        assert "is_ms_shipped = 0" in sent_sql

    def test_collect_sample_data_uses_top_n(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql_json.return_value = []
        collector.collect_sample_data("dbo.Orders", limit=25)
        sent_sql = mock_ssm.run_sql_json.call_args.kwargs["sql"]
        assert "TOP 25" in sent_sql
        assert "LIMIT" not in sent_sql
        assert "[dbo].[Orders]" in sent_sql


# ---------------------------------------------------------------------
# Query patterns
# ---------------------------------------------------------------------


class TestQueryPatterns:
    def _row(self, **overrides: object) -> dict:
        base = {
            "query_id": "0xABCDEF1234567890",
            "query_text": "SELECT * FROM Orders WHERE customer_id = @id",
            "execution_count": 100,
            "total_logical_reads": 5000,
            "total_physical_reads": 10,
            "total_worker_time": 1_000_000,  # microseconds → 1000ms total
            "total_elapsed_time": 2_000_000,  # microseconds → 2000ms total
            "total_rows": 200,
            "min_elapsed_time": 5_000,  # 5ms
            "max_elapsed_time": 50_000,  # 50ms
            "creation_time": "2026-01-01 00:00:00.000",
            "last_execution_time": "2026-06-15 14:00:00.000",
        }
        base.update(overrides)
        return base

    def test_collect_query_patterns_per_execution_averages(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql_json.return_value = [self._row()]
        patterns = collector.collect_query_patterns()
        assert len(patterns) == 1
        p = patterns[0]
        # avg_elapsed_ms = total_elapsed_us / 1000 / count = 2_000_000 / 1000 / 100 = 20ms
        assert p["execution_time_ms_avg"] == pytest.approx(20.0)
        # avg_cpu_ms = total_worker_us / 1000 / count = 1_000_000 / 1000 / 100 = 10ms
        assert p["avg_cpu_time_ms"] == pytest.approx(10.0)
        # avg_logical = 5000 / 100 = 50
        assert p["avg_logical_reads"] == pytest.approx(50.0)
        # avg_physical = 10 / 100 = 0.1
        assert p["avg_physical_reads"] == pytest.approx(0.1)
        # rows = 200 / 100 = 2
        assert p["rows_returned_avg"] == pytest.approx(2.0)
        # cache hit ratio = (5000 - 10) / 5000 * 100 = 99.8%
        assert p["cache_hit_ratio_pct"] == pytest.approx(99.8)

    def test_collect_query_patterns_min_max_in_milliseconds(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql_json.return_value = [self._row()]
        patterns = collector.collect_query_patterns()
        # min_elapsed_time was 5_000us → 5ms
        assert patterns[0]["execution_time_ms_min"] == pytest.approx(5.0)
        # max_elapsed_time was 50_000us → 50ms
        assert patterns[0]["execution_time_ms_max"] == pytest.approx(50.0)

    def test_collect_query_patterns_query_text_features(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql_json.return_value = [
            self._row(
                query_text="SELECT a.* FROM Orders a JOIN Customers c ON a.cid = c.id "
                "WHERE c.name LIKE '%foo%' GROUP BY a.id"
            )
        ]
        p = collector.collect_query_patterns()[0]
        assert p["has_joins"] is True
        assert p["join_count"] == 1
        assert p["has_aggregations"] is True
        assert p["has_text_search"] is True
        assert p["text_search_type"] == "like_wildcard"

    def test_collect_query_patterns_passes_thresholds_to_sql(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql_json.return_value = []
        collector.collect_query_patterns(min_executions=50, limit=200)
        sent_sql = mock_ssm.run_sql_json.call_args.kwargs["sql"]
        assert "TOP 200" in sent_sql
        assert "execution_count >= 50" in sent_sql

    def test_collect_query_patterns_handles_zero_logical_reads(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        # Cache-hit-ratio formula must not divide by zero
        mock_ssm.run_sql_json.return_value = [
            self._row(total_logical_reads=0, total_physical_reads=0)
        ]
        p = collector.collect_query_patterns()[0]
        assert p["cache_hit_ratio_pct"] is None

    def test_collect_query_patterns_query_id_from_hash(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql_json.return_value = [self._row(query_id="0xDEADBEEF12345678")]
        p = collector.collect_query_patterns()[0]
        assert p["query_id"] == "0xDEADBEEF12345678"

    def test_collect_query_patterns_falls_back_to_text_hash(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        # Empty query_id → fallback to hash of query text
        mock_ssm.run_sql_json.return_value = [self._row(query_id=None)]
        p = collector.collect_query_patterns()[0]
        assert p["query_id"]
        # 16 hex chars from sha256
        assert len(p["query_id"]) == 16


# ---------------------------------------------------------------------
# Global stats
# ---------------------------------------------------------------------


class TestGlobalStats:
    def test_collect_global_stats_buffer_hit_ratio(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql_json.return_value = [
            {
                "active_connections": 12,
                "buffer_hit_raw": 9_950_000,
                "buffer_hit_base": 10_000_000,
                "total_transactions": 50_000,
            }
        ]
        stats = collector.collect_global_stats()
        # hit_ratio = 9_950_000 / 10_000_000 * 100 = 99.5%
        assert stats["cache_hit_ratio_pct"] == pytest.approx(99.5)
        assert stats["active_connections"] == 12
        assert stats["total_transactions"] == 50_000

    def test_collect_global_stats_handles_zero_base(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql_json.return_value = [
            {
                "active_connections": 0,
                "buffer_hit_raw": 0,
                "buffer_hit_base": 0,
                "total_transactions": 0,
            }
        ]
        stats = collector.collect_global_stats()
        assert stats["cache_hit_ratio_pct"] == 0.0

    def test_collect_global_stats_empty_returns_empty_dict(
        self, collector: SQLServerRemoteCollector, mock_ssm: MagicMock
    ) -> None:
        mock_ssm.run_sql_json.return_value = []
        assert collector.collect_global_stats() == {}


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


class TestHelpers:
    @pytest.mark.parametrize(
        "sql,expected",
        [
            ("SELECT * FROM t", "SELECT"),
            ("  insert into t values(1)", "INSERT"),
            ("UPDATE t SET x = 1", "UPDATE"),
            ("DELETE FROM t WHERE x = 1", "DELETE"),
            ("MERGE INTO t USING s ON ...", "MERGE"),
            ("EXEC sp_helpdb", "OTHER"),
        ],
    )
    def test_extract_query_type(self, sql: str, expected: str) -> None:
        assert _extract_query_type(sql) == expected

    def test_extract_tables_handles_brackets(self) -> None:
        sql = "SELECT * FROM [dbo].[Orders] o JOIN [Customers] c ON o.cid = c.id"
        tables = _extract_tables(sql)
        assert "dbo.Orders" in tables
        assert "Customers" in tables

    def test_extract_tables_excludes_system(self) -> None:
        # 'sys' should be filtered out as a referenced table name
        sql = "SELECT * FROM sys.tables WHERE schema_id = 1"
        assert _extract_tables(sql) == []

    def test_extract_filter_columns_from_where(self) -> None:
        sql = "SELECT * FROM Orders WHERE customer_id = 5 AND status LIKE 'open' AND total > 100"
        cols = _extract_filter_columns(sql)
        assert cols is not None
        assert "customer_id" in cols
        assert "status" in cols
        assert "total" in cols

    def test_extract_filter_columns_no_where(self) -> None:
        assert _extract_filter_columns("SELECT * FROM t") is None

    def test_extract_sort_columns(self) -> None:
        cols = _extract_sort_columns("SELECT * FROM t ORDER BY name DESC, created_at ASC")
        assert cols is not None
        assert "name" in cols
        assert "created_at" in cols
        assert "ASC" not in cols
        assert "DESC" not in cols

    def test_hash_is_stable_and_short(self) -> None:
        h1 = _hash("SELECT 1")
        h2 = _hash("SELECT 1")
        assert h1 == h2
        assert len(h1) == 16
        assert _hash("SELECT 2") != h1

    def test_estimate_percentiles_from_min_max_orders(self) -> None:
        p50, p95, p99 = _estimate_percentiles_from_min_max(avg=10, min_v=2, max_v=100)
        assert p50 == 10
        # p95 between avg and max
        assert p50 < p95 < p99 <= 100

    def test_estimate_percentiles_when_max_equals_avg(self) -> None:
        # No spread → all percentiles equal avg
        p50, p95, p99 = _estimate_percentiles_from_min_max(avg=10, min_v=10, max_v=10)
        assert p50 == p95 == p99 == 10

    def test_estimate_rows_p95_returns_none_on_zero_avg(self) -> None:
        assert _estimate_rows_p95(10, 0, 5) is None

    def test_estimate_rows_p95_scales_by_latency_ratio(self) -> None:
        # avg_rows=10, avg_ms=5, p95_ms=15 → expected = 10 * (15/5) = 30
        assert _estimate_rows_p95(10, 5, 15) == 30.0

    def test_has_text_search_recognizes_like_wildcard(self) -> None:
        assert _has_text_search("SELECT * FROM t WHERE name LIKE '%foo%'") is True
        assert _has_text_search("SELECT * FROM t WHERE name LIKE N'%foo%'") is True

    def test_has_text_search_recognizes_fulltext(self) -> None:
        assert _has_text_search("SELECT * FROM t WHERE CONTAINS(notes, 'urgent')") is True
        assert _has_text_search("SELECT * FROM t WHERE FREETEXT(notes, 'urgent')") is True

    def test_has_text_search_negative(self) -> None:
        assert _has_text_search("SELECT id FROM t WHERE id = 1") is False

    def test_text_search_type_returns_specific(self) -> None:
        assert _text_search_type("WHERE name LIKE '%foo%'") == "like_wildcard"
        assert _text_search_type("WHERE CONTAINS(c, 'q')") == "fulltext"
        assert _text_search_type("WHERE id = 1") is None

    def test_has_time_range_filter_recognizes_functions(self) -> None:
        assert _has_time_range_filter("WHERE created >= GETDATE()")
        assert _has_time_range_filter("WHERE updated > SYSDATETIME()")

    def test_has_time_range_filter_recognizes_column_compare(self) -> None:
        assert _has_time_range_filter("WHERE created_at >= '2026-01-01'")

    def test_has_time_range_filter_negative(self) -> None:
        assert _has_time_range_filter("WHERE id = 1") is False


# ---------------------------------------------------------------------
# SSM executor parser updates (sqlcmd output)
# ---------------------------------------------------------------------


class TestSSMExecutorParser:
    """Verify _parse_tabular_output handles SQL Server's quirks."""

    def test_strips_dashes_separator_line(self) -> None:
        # sqlcmd default output: header, dashes line, data, footer
        raw = (
            "id\tname\tcreated\n"
            "----\t----\t-------------------\n"
            "1\tAlice\t2026-01-01\n"
            "2\tBob\t2026-01-02\n"
            "(2 rows affected)"
        )
        rows = _parse_tabular_output(raw)
        assert rows == [
            {"id": 1, "name": "Alice", "created": "2026-01-01"},
            {"id": 2, "name": "Bob", "created": "2026-01-02"},
        ]

    def test_strips_postgres_footer_too(self) -> None:
        raw = "id\tname\n1\tAlice\n(1 row)"
        rows = _parse_tabular_output(raw)
        assert rows == [{"id": 1, "name": "Alice"}]

    def test_strips_sqlserver_footer(self) -> None:
        raw = "id\tname\n1\tAlice\n(1 row affected)"
        rows = _parse_tabular_output(raw)
        assert rows == [{"id": 1, "name": "Alice"}]

    def test_handles_null_values(self) -> None:
        raw = "id\tname\n----\t----\n1\tAlice\n2\tNULL"
        rows = _parse_tabular_output(raw)
        assert rows[0]["name"] == "Alice"
        assert rows[1]["name"] is None

    def test_returns_empty_when_no_data(self) -> None:
        raw = "id\tname\n----\t----\n(0 rows affected)"
        rows = _parse_tabular_output(raw)
        assert rows == []
