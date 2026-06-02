"""Unit tests for MariaDB collector tools and orchestrator."""

from unittest.mock import MagicMock, patch

import pytest

from src.tools.database.mariadb_tools import (
    MariaDBRemoteCollector,
    _is_mariadb,
    _mariadb_version_gte,
)

# ---------------------------------------------------------------------------
# Version detection
# ---------------------------------------------------------------------------


class TestMariaDBVersionDetection:
    def test_is_mariadb_true(self):
        assert _is_mariadb("10.6.18-MariaDB") is True
        assert _is_mariadb("11.4.2-MariaDB-log") is True

    def test_is_mariadb_false(self):
        assert _is_mariadb("8.0.43") is False
        assert _is_mariadb("5.7.44-log") is False

    def test_version_gte_mariadb(self):
        assert _mariadb_version_gte("10.6.18-MariaDB", "10.5.4") is True
        assert _mariadb_version_gte("10.5.4-MariaDB", "10.5.4") is True
        assert _mariadb_version_gte("10.4.0-MariaDB", "10.5.4") is False
        assert _mariadb_version_gte("11.0.0-MariaDB", "10.5.4") is True


# ---------------------------------------------------------------------------
# MariaDBRemoteCollector feature flags
# ---------------------------------------------------------------------------


class TestMariaDBRemoteCollector:
    def _make_collector(self, version: str = "10.6.18-MariaDB") -> MariaDBRemoteCollector:
        ssm = MagicMock()
        col = MariaDBRemoteCollector(
            ssm=ssm,
            host="mariadb.example.com",
            port=3306,
            database="testdb",
            secret_arn="arn:aws:secretsmanager:us-east-1:123:secret:test",  # pragma: allowlist secret  # nosec B106
        )
        col.get_version = MagicMock(return_value=version)  # type: ignore[method-assign]
        return col

    def test_no_quantiles_for_mariadb(self):
        """MariaDB should never request QUANTILE_95/QUANTILE_99 columns."""
        col = self._make_collector("10.11.0-MariaDB")
        col._query = MagicMock(return_value=[])  # type: ignore[method-assign]
        col.collect_query_patterns()
        sql = col._query.call_args[0][0]
        assert "QUANTILE_95" not in sql
        assert "QUANTILE_99" not in sql

    def test_errors_included_for_10_5_4(self):
        """MariaDB 10.5.4+ should include SUM_ERRORS/SUM_WARNINGS."""
        col = self._make_collector("10.5.4-MariaDB")
        col._query = MagicMock(return_value=[])  # type: ignore[method-assign]
        col.collect_query_patterns()
        sql = col._query.call_args[0][0]
        assert "SUM_ERRORS" in sql
        assert "SUM_WARNINGS" in sql

    def test_errors_excluded_for_10_4(self):
        """MariaDB 10.4.x should NOT include SUM_ERRORS/SUM_WARNINGS."""
        col = self._make_collector("10.4.30-MariaDB")
        col._query = MagicMock(return_value=[])  # type: ignore[method-assign]
        col.collect_query_patterns()
        sql = col._query.call_args[0][0]
        assert "SUM_ERRORS" not in sql

    def test_first_last_seen_for_10_6(self):
        """MariaDB 10.6+ should include FIRST_SEEN/LAST_SEEN."""
        col = self._make_collector("10.6.18-MariaDB")
        col._query = MagicMock(return_value=[])  # type: ignore[method-assign]
        col.collect_query_patterns()
        sql = col._query.call_args[0][0]
        assert "FIRST_SEEN" in sql
        assert "LAST_SEEN" in sql

    def test_pattern_parsing(self):
        """Verify query pattern dict is built correctly from raw row."""
        col = self._make_collector("10.6.18-MariaDB")
        col._query = MagicMock(  # type: ignore[method-assign]
            return_value=[
                {
                    "digest": "abc123",
                    "query_text": "SELECT * FROM users WHERE id = ?",
                    "schema_name": "testdb",
                    "execution_count": 100,
                    "total_time_ms": 500.0,
                    "avg_time_ms": 5.0,
                    "min_time_ms": 1.0,
                    "max_time_ms": 50.0,
                    "total_rows_sent": 100,
                    "total_rows_examined": 100,
                    "total_rows_affected": 0,
                    "full_table_scans": 0,
                    "range_scans": 0,
                    "no_index_used": 0,
                    "no_good_index_used": 0,
                    "lock_time_ms": 1.0,
                    "sum_errors": 0,
                    "sum_warnings": 0,
                    "FIRST_SEEN": "2026-04-01 00:00:00",
                    "LAST_SEEN": "2026-04-20 00:00:00",
                }
            ]
        )
        patterns = col.collect_query_patterns()
        assert len(patterns) == 1
        p = patterns[0]
        assert p["query_id"] == "abc123"
        assert p["query_type"] == "SELECT"
        assert p["execution_time_ms_avg"] == 5.0
        assert p["execution_time_ms_p95"] is None  # no quantiles for MariaDB
        assert p["execution_time_ms_p99"] is None
        assert p["first_seen"] == "2026-04-01 00:00:00"
        assert p["has_joins"] is False
        assert p["scan_efficiency_pct"] == 100.0

    def test_inherits_schema_collection(self):
        """MariaDB collector inherits all schema methods from MySQL."""
        col = self._make_collector()
        assert hasattr(col, "collect_tables")
        assert hasattr(col, "collect_columns")
        assert hasattr(col, "collect_indexes")
        assert hasattr(col, "collect_foreign_keys")
        assert hasattr(col, "collect_views")
        assert hasattr(col, "collect_procedures")
        assert hasattr(col, "collect_triggers")
        assert hasattr(col, "collect_global_stats")


# ---------------------------------------------------------------------------
# Handler dispatch
# ---------------------------------------------------------------------------


class TestHandlerDispatch:
    def test_dispatch_mariadb(self):
        """Handler dispatches to mariadb_collector for engine=mariadb."""
        from src.agents.collector.handler import _dispatch_collect

        with patch("src.agents.collector.mariadb_collector.collect") as mock_collect:
            mock_collect.return_value = MagicMock()
            inp = MagicMock()
            _dispatch_collect("mariadb", inp)
            mock_collect.assert_called_once_with(inp)

    def test_dispatch_mysql(self):
        """Handler dispatches to mysql_collector for engine=mysql."""
        from src.agents.collector.handler import _dispatch_collect

        with patch("src.agents.collector.mysql_collector.collect") as mock_collect:
            mock_collect.return_value = MagicMock()
            inp = MagicMock()
            _dispatch_collect("mysql", inp)
            mock_collect.assert_called_once_with(inp)

    def test_dispatch_postgresql(self):
        """Handler dispatches to postgres_collector for engine=postgresql."""
        from src.agents.collector.handler import _dispatch_collect

        with patch("src.agents.collector.postgres_collector.collect") as mock_collect:
            mock_collect.return_value = MagicMock()
            inp = MagicMock()
            _dispatch_collect("postgresql", inp)
            mock_collect.assert_called_once_with(inp)

    def test_dispatch_unsupported_raises(self):
        """Handler raises ValueError for unsupported engine."""
        from src.agents.collector.handler import _dispatch_collect

        with pytest.raises(ValueError, match="Unsupported engine"):
            _dispatch_collect("oracle", MagicMock())
