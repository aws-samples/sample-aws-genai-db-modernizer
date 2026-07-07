"""Tests for OracleRemoteCollector and oracle_tools helpers."""

from unittest.mock import MagicMock

import pytest

from src.tools.database.oracle_tools import (
    OracleRemoteCollector,
    _extract_filter_columns,
    _extract_query_type,
    _extract_sort_columns,
    _extract_tables,
    _has_text_search,
    _has_time_range_filter,
    _hash,
    _normalize_fk_action,
    _normalize_index_type,
    _text_search_type,
)


@pytest.fixture
def mock_ssm():
    return MagicMock()


@pytest.fixture
def collector(mock_ssm):
    return OracleRemoteCollector(
        ssm=mock_ssm,
        host="oracle-test.us-east-1.rds.amazonaws.com",
        port=1521,
        database="ORCLPDB1",
        secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:oracle-test",  # pragma: allowlist secret
        region="us-east-1",
    )


class TestSSMDispatch:
    def test_query_passes_engine_oracle(self, collector, mock_ssm):
        mock_ssm.run_sql_json.return_value = []
        collector._query("SELECT 1 FROM DUAL")
        mock_ssm.run_sql_json.assert_called_once()
        call_kwargs = mock_ssm.run_sql_json.call_args.kwargs
        assert call_kwargs["engine"] == "oracle"
        assert call_kwargs["port"] == 1521
        assert call_kwargs["database"] == "ORCLPDB1"


class TestMetadata:
    def test_get_version(self, collector, mock_ssm):
        mock_ssm.run_sql_json.return_value = [
            {"version": "Oracle Database 19c Enterprise Edition Release 19.0.0.0.0"}
        ]
        assert "19c" in collector.get_version()

    def test_get_database_size_gb(self, collector, mock_ssm):
        mock_ssm.run_sql_json.return_value = [{"size_gb": "42.5"}]
        assert collector.get_database_size_gb() == 42.5

    def test_get_current_schema(self, collector, mock_ssm):
        mock_ssm.run_sql_json.return_value = [{"s": "APPUSER"}]
        assert collector.get_current_schema() == "APPUSER"


class TestCollectTables:
    def test_basic_table_list(self, collector, mock_ssm):
        mock_ssm.run_sql_json.side_effect = [
            [{"s": "APPUSER"}],  # get_current_schema
            [  # collect_tables query
                {
                    "schema_name": "APPUSER",
                    "table_name": "ORDERS",
                    "row_count": "1000",
                    "data_size_mb": "5.5",
                }
            ],
        ]
        result = collector.collect_tables()
        assert len(result) == 1
        assert result[0]["table_name"] == "orders"  # lowercased
        assert result[0]["schema_name"] == "appuser"

    def test_explicit_owner(self, collector, mock_ssm):
        mock_ssm.run_sql_json.return_value = []
        collector.collect_tables(owner="HR")
        sql = mock_ssm.run_sql_json.call_args.kwargs["sql"]
        assert "t.OWNER = 'HR'" in sql


class TestCollectColumns:
    def test_columns_lowercased(self, collector, mock_ssm):
        mock_ssm.run_sql_json.side_effect = [
            [{"s": "APPUSER"}],  # get_current_schema
            [
                {
                    "column_name": "ORDER_ID",
                    "ordinal_position": "1",
                    "data_type": "NUMBER",
                    "max_length": "22",
                    "char_used": None,
                    "is_nullable": "N",
                    "column_default": None,
                    "data_precision": "10",
                    "data_scale": "0",
                    "is_identity": "YES",
                }
            ],
        ]
        result = collector.collect_columns("orders")
        assert result[0]["column_name"] == "order_id"
        assert result[0]["data_type"] == "number"

    def test_qualified_name(self, collector, mock_ssm):
        mock_ssm.run_sql_json.return_value = []
        collector.collect_columns("HR.EMPLOYEES")
        sql = mock_ssm.run_sql_json.call_args.kwargs["sql"]
        assert "OWNER = 'HR'" in sql
        assert "TABLE_NAME = 'EMPLOYEES'" in sql


class TestCollectIndexes:
    def test_grouped_by_name(self, collector, mock_ssm):
        mock_ssm.run_sql_json.side_effect = [
            [{"s": "APP"}],
            [
                {
                    "index_name": "IDX_ORDERS_DATE",
                    "column_name": "ORDER_DATE",
                    "key_ordinal": "1",
                    "uniqueness": "NONUNIQUE",
                    "index_type": "NORMAL",
                    "is_primary": "NO",
                },
                {
                    "index_name": "IDX_ORDERS_DATE",
                    "column_name": "CUSTOMER_ID",
                    "key_ordinal": "2",
                    "uniqueness": "NONUNIQUE",
                    "index_type": "NORMAL",
                    "is_primary": "NO",
                },
            ],
        ]
        result = collector.collect_indexes("orders")
        assert len(result) == 1
        assert result[0]["columns"] == ["order_date", "customer_id"]
        assert result[0]["is_unique"] is False
        assert result[0]["index_type"] == "btree"


class TestCollectForeignKeys:
    def test_multi_column_fk(self, collector, mock_ssm):
        mock_ssm.run_sql_json.side_effect = [
            [{"s": "APP"}],
            [
                {
                    "constraint_name": "FK_ITEM_ORDER",
                    "column_name": "ORDER_ID",
                    "ordinal": "1",
                    "referenced_table_name": "ORDERS",
                    "referenced_column_name": "ID",
                    "on_delete": "CASCADE",
                },
                {
                    "constraint_name": "FK_ITEM_ORDER",
                    "column_name": "ITEM_SEQ",
                    "ordinal": "2",
                    "referenced_table_name": "ORDERS",
                    "referenced_column_name": "SEQ",
                    "on_delete": "CASCADE",
                },
            ],
        ]
        result = collector.collect_foreign_keys("order_items")
        assert len(result) == 1
        assert result[0]["columns"] == ["order_id", "item_seq"]
        assert result[0]["referenced_table"] == "orders"
        assert result[0]["on_delete"] == "CASCADE"
        assert result[0]["on_update"] == "NO ACTION"  # Oracle doesn't support ON UPDATE


class TestCollectPrimaryKey:
    def test_pk_columns(self, collector, mock_ssm):
        mock_ssm.run_sql_json.side_effect = [
            [{"s": "APP"}],
            [{"column_name": "ORDER_ID"}],
        ]
        result = collector.collect_primary_key("orders")
        assert result == ["order_id"]


class TestCollectQueryPatterns:
    def test_basic_pattern(self, collector, mock_ssm):
        mock_ssm.run_sql_json.side_effect = [
            [{"s": "APPUSER"}],  # get_current_schema
            [
                {
                    "query_id": "abc123def456",
                    "query_text": "SELECT * FROM ORDERS WHERE ORDER_DATE > SYSDATE - 7",
                    "execution_count": "100",
                    "total_elapsed_us": "5000000",
                    "total_cpu_us": "2000000",
                    "total_logical_reads": "10000",
                    "total_physical_reads": "500",
                    "total_rows": "5000",
                    "first_seen": "2026-01-15 10:00:00",
                    "last_seen": "2026-06-15 14:00:00",
                }
            ],
        ]
        result = collector.collect_query_patterns()
        assert len(result) == 1
        p = result[0]
        assert p["query_id"] == "abc123def456"
        assert p["execution_time_ms_avg"] == 50.0  # 5M us / 1000 / 100
        assert p["avg_cpu_time_ms"] == 20.0
        assert p["avg_logical_reads"] == 100.0
        assert p["avg_physical_reads"] == 5.0
        assert p["has_time_range_filter"] is True  # SYSDATE
        assert p["execution_time_ms_p50"] is None  # Oracle can't estimate
        assert p["execution_time_ms_p95"] is None

    def test_cache_hit_ratio(self, collector, mock_ssm):
        mock_ssm.run_sql_json.side_effect = [
            [{"s": "APPUSER"}],
            [
                {
                    "query_id": "q1",
                    "query_text": "SELECT 1",
                    "execution_count": "10",
                    "total_elapsed_us": "1000",
                    "total_cpu_us": "500",
                    "total_logical_reads": "100",
                    "total_physical_reads": "10",
                    "total_rows": "10",
                    "first_seen": None,
                    "last_seen": None,
                }
            ],
        ]
        result = collector.collect_query_patterns()
        # (100 - 10) / 100 * 100 = 90%
        assert result[0]["cache_hit_ratio_pct"] == 90.0

    def test_zero_logical_reads_no_ratio(self, collector, mock_ssm):
        mock_ssm.run_sql_json.side_effect = [
            [{"s": "APPUSER"}],
            [
                {
                    "query_id": "q1",
                    "query_text": "BEGIN NULL; END;",
                    "execution_count": "1",
                    "total_elapsed_us": "100",
                    "total_cpu_us": "50",
                    "total_logical_reads": "0",
                    "total_physical_reads": "0",
                    "total_rows": "0",
                    "first_seen": None,
                    "last_seen": None,
                }
            ],
        ]
        result = collector.collect_query_patterns(min_executions=1)
        assert result[0]["cache_hit_ratio_pct"] is None


class TestCollectGlobalStats:
    def test_basic_stats(self, collector, mock_ssm):
        mock_ssm.run_sql_json.return_value = [
            {
                "active_connections": "25",
                "db_block_gets": "1000",
                "consistent_gets": "9000",
                "physical_reads": "500",
                "user_commits": "2000",
            }
        ]
        result = collector.collect_global_stats()
        # (1000+9000-500) / (1000+9000) * 100 = 95%
        assert result["cache_hit_ratio_pct"] == 95.0
        assert result["active_connections"] == 25
        assert result["total_transactions"] == 2000


class TestCollectViews:
    def test_views_lowercased(self, collector, mock_ssm):
        mock_ssm.run_sql_json.side_effect = [
            [{"s": "APP"}],
            [{"view_name": "V_ACTIVE_ORDERS", "schema_name": "APP", "text_length": "200"}],
        ]
        result = collector.collect_views()
        assert result[0]["view_name"] == "v_active_orders"


class TestCollectProcedures:
    def test_procedure_type(self, collector, mock_ssm):
        mock_ssm.run_sql_json.side_effect = [
            [{"s": "APP"}],
            [
                {
                    "routine_name": "PROCESS_ORDER",
                    "schema_name": "APP",
                    "routine_type": "PROCEDURE",
                },
                {"routine_name": "CALC_TOTAL", "schema_name": "APP", "routine_type": "FUNCTION"},
            ],
        ]
        result = collector.collect_procedures()
        assert result[0]["routine_name"] == "process_order"
        assert result[0]["language"] == "PL/SQL"
        assert result[1]["routine_type"] == "FUNCTION"


class TestCollectTriggers:
    def test_timing_normalization(self, collector, mock_ssm):
        mock_ssm.run_sql_json.side_effect = [
            [{"s": "APP"}],
            [
                {
                    "trigger_name": "TRG_AUDIT",
                    "table_name": "ORDERS",
                    "schema_name": "APP",
                    "timing": "BEFORE EACH ROW",
                    "event_type": "INSERT OR UPDATE",
                },
            ],
        ]
        result = collector.collect_triggers()
        assert result[0]["trigger_name"] == "trg_audit"
        assert result[0]["timing"] == "BEFORE"
        # Raw event_type preserved — splitting happens in orchestrator _build_triggers
        assert result[0]["event_type"] == "INSERT OR UPDATE"


class TestSplitQualified:
    def test_with_owner(self):
        assert OracleRemoteCollector._split_qualified("hr.employees") == ("HR", "EMPLOYEES")

    def test_without_owner(self):
        assert OracleRemoteCollector._split_qualified("orders") == ("", "ORDERS")


# -----------------------------------------------------------------------
# Helper function tests
# -----------------------------------------------------------------------


class TestHash:
    def test_deterministic(self):
        assert _hash("SELECT 1") == _hash("SELECT 1")
        assert len(_hash("SELECT 1")) == 16


class TestNormalizeIndexType:
    def test_normal(self):
        assert _normalize_index_type("NORMAL") == "btree"

    def test_bitmap(self):
        assert _normalize_index_type("BITMAP") == "bitmap"

    def test_function_based(self):
        assert _normalize_index_type("FUNCTION-BASED NORMAL") == "functional"

    def test_none(self):
        assert _normalize_index_type(None) == "btree"


class TestNormalizeFkAction:
    def test_cascade(self):
        assert _normalize_fk_action("CASCADE") == "CASCADE"

    def test_set_null(self):
        assert _normalize_fk_action("SET NULL") == "SET NULL"

    def test_no_action(self):
        assert _normalize_fk_action("NO ACTION") == "NO ACTION"

    def test_none_default(self):
        assert _normalize_fk_action(None) == "NO ACTION"


class TestExtractQueryType:
    def test_select(self):
        assert _extract_query_type("SELECT * FROM t") == "SELECT"

    def test_merge(self):
        assert _extract_query_type("MERGE INTO t USING s ON ...") == "MERGE"

    def test_plsql(self):
        assert _extract_query_type("BEGIN NULL; END;") == "OTHER"


class TestExtractTables:
    def test_from_and_join(self):
        sql = "SELECT * FROM orders o JOIN customers c ON o.cid = c.id"
        assert _extract_tables(sql) == ["orders", "customers"]

    def test_schema_qualified(self):
        sql = "SELECT * FROM hr.employees JOIN hr.departments ON ..."
        result = _extract_tables(sql)
        assert "hr.employees" in result
        assert "hr.departments" in result

    def test_deduplication(self):
        sql = "SELECT * FROM orders o JOIN orders o2 ON o.id = o2.parent_id"
        assert _extract_tables(sql) == ["orders"]


class TestExtractFilterColumns:
    def test_where_clause(self):
        assert _extract_filter_columns("WHERE status = 'ACTIVE' AND amount > 100") == [
            "status",
            "amount",
        ]

    def test_none_when_no_where(self):
        assert _extract_filter_columns("SELECT * FROM t") is None


class TestExtractSortColumns:
    def test_order_by(self):
        assert _extract_sort_columns("SELECT * FROM t ORDER BY created_at DESC, name ASC") == [
            "created_at",
            "name",
        ]

    def test_none_when_no_order(self):
        assert _extract_sort_columns("SELECT * FROM t") is None


class TestTextSearch:
    def test_contains(self):
        assert _has_text_search("SELECT * FROM t WHERE CONTAINS(body, 'test')") is True
        assert _text_search_type("SELECT * FROM t WHERE CONTAINS(body, 'test')") == "oracle_text"

    def test_like_wildcard(self):
        assert _has_text_search("SELECT * FROM t WHERE name LIKE '%smith%'") is True
        assert _text_search_type("SELECT * FROM t WHERE name LIKE '%smith%'") == "like_wildcard"

    def test_ctx(self):
        assert _has_text_search("SELECT * FROM t WHERE ctx_query(...)") is True

    def test_no_text_search(self):
        assert _has_text_search("SELECT * FROM t WHERE id = 1") is False
        assert _text_search_type("SELECT * FROM t WHERE id = 1") is None


class TestTimeRangeFilter:
    def test_sysdate(self):
        assert _has_time_range_filter("WHERE order_date > SYSDATE - 7") is True

    def test_interval(self):
        assert _has_time_range_filter("WHERE ts > SYSTIMESTAMP - INTERVAL '1' DAY") is True

    def test_between_date(self):
        assert (
            _has_time_range_filter(
                "WHERE d BETWEEN TO_DATE('2026-01-01') AND TO_DATE('2026-12-31')"
            )
            is True
        )

    def test_no_time_range(self):
        assert _has_time_range_filter("WHERE id = 42") is False
