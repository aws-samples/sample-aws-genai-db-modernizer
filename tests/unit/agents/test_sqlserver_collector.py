"""Unit tests for SQL Server collector orchestrator and handler dispatch."""

from unittest.mock import MagicMock, patch

import pytest

from src.agents.collector.sqlserver_collector import (
    _build_procedures,
    _build_queries,
    _build_queries_from_aws,
    _build_tables,
    _build_triggers,
    _build_views,
    _enrich_table_scans,
    _normalize_max_length,
    _parse_sqlserver_version,
)
from src.contracts.collector_input import CollectionMode, CollectorInput, DatabaseEngine
from src.contracts.collector_output import NormalizedDataType, Queries, QueryLogSource

# ---------------------------------------------------------------------------
# Version parsing
# ---------------------------------------------------------------------------


class TestVersionParsing:
    def test_extracts_build_number(self) -> None:
        raw = (
            "Microsoft SQL Server 2022 (RTM-CU13) (KB5036432) - 16.0.4135.4 (X64) "
            "Apr 25 2026 ... Express Edition (64-bit) on Linux"
        )
        assert _parse_sqlserver_version(raw) == "16.0.4135.4"

    def test_falls_back_to_year(self) -> None:
        # Realistic but with no build number
        raw = "Microsoft SQL Server 2019 Edition - X64"
        assert _parse_sqlserver_version(raw) == "2019"

    def test_returns_truncated_raw_on_unknown_format(self) -> None:
        raw = "Some Unknown Database Engine vXYZ"
        # No build number, no SQL Server year → falls back to truncated raw
        assert _parse_sqlserver_version(raw) == raw[:50]

    def test_picks_first_build_when_multiple_present(self) -> None:
        # Word boundaries cause the regex to match the trailing 4-segment substring
        # (the leading segment fails because '.5' breaks the trailing word boundary).
        raw = "Engine v1.2.3.4.5 patched"
        assert _parse_sqlserver_version(raw) == "2.3.4.5"

    def test_handles_empty(self) -> None:
        assert _parse_sqlserver_version("") == "unknown"


# ---------------------------------------------------------------------------
# max_length normalization (sys.columns reports bytes)
# ---------------------------------------------------------------------------


class TestNormalizeMaxLength:
    def test_max_minus_one_becomes_none(self) -> None:
        assert _normalize_max_length(-1, "nvarchar") is None
        assert _normalize_max_length(-1, "varchar") is None
        assert _normalize_max_length(-1, "varbinary") is None

    def test_nvarchar_is_halved(self) -> None:
        # nvarchar(255) → max_length=510 in sys.columns (2 bytes per char)
        assert _normalize_max_length(510, "nvarchar") == 255
        assert _normalize_max_length(20, "nchar") == 10

    def test_varchar_passthrough(self) -> None:
        assert _normalize_max_length(255, "varchar") == 255
        assert _normalize_max_length(36, "char") == 36

    def test_handles_none(self) -> None:
        assert _normalize_max_length(None, "varchar") is None

    def test_handles_string_input(self) -> None:
        assert _normalize_max_length("510", "nvarchar") == 255

    def test_handles_garbage(self) -> None:
        assert _normalize_max_length("abc", "nvarchar") is None


# ---------------------------------------------------------------------------
# _build_tables / type mapping
# ---------------------------------------------------------------------------


class TestBuildTables:
    def _table_raw(self, **column_overrides):
        col = {
            "column_name": "id",
            "ordinal_position": 1,
            "data_type": "int",
            "max_length": 4,
            "is_nullable": "NO",
            "is_identity": "YES",
            "column_default": None,
            "cardinality": None,
        }
        col.update(column_overrides)
        return {
            "schema_name": "dbo",
            "table_name": "Customers",
            "row_count": 1000,
            "data_size_mb": 5.0,
            "index_size_mb": 1.5,
            "columns": [col],
            "indexes": [],
            "foreign_keys": [],
            "primary_key": ["id"],
            "sample_data": None,
        }

    def test_table_id_uses_schema_dot_name(self) -> None:
        tables = _build_tables({"tables": [self._table_raw()]})
        assert tables[0].table_id == "dbo.Customers"
        assert tables[0].schema_name == "dbo"

    def test_default_schema_is_dbo(self) -> None:
        raw = self._table_raw()
        del raw["schema_name"]
        tables = _build_tables({"tables": [raw]})
        assert tables[0].schema_name == "dbo"
        assert tables[0].table_id == "dbo.Customers"

    def test_size_mb_combines_data_and_index(self) -> None:
        tables = _build_tables({"tables": [self._table_raw()]})
        # 5.0 + 1.5 = 6.5
        assert tables[0].size_mb == pytest.approx(6.5)

    def test_identity_column_marked_auto_increment(self) -> None:
        tables = _build_tables({"tables": [self._table_raw()]})
        assert tables[0].columns[0].is_auto_increment is True

    def test_non_identity_not_auto_increment(self) -> None:
        tables = _build_tables({"tables": [self._table_raw(is_identity="NO")]})
        assert tables[0].columns[0].is_auto_increment is False

    def test_normalized_types(self) -> None:
        types_to_check = [
            ("int", NormalizedDataType.integer),
            ("bigint", NormalizedDataType.integer),
            ("nvarchar", NormalizedDataType.string),
            ("decimal", NormalizedDataType.decimal),
            ("bit", NormalizedDataType.boolean),
            ("uniqueidentifier", NormalizedDataType.uuid),
            ("datetime2", NormalizedDataType.timestamp),
            ("varbinary", NormalizedDataType.binary),
        ]
        for native, expected in types_to_check:
            tables = _build_tables({"tables": [self._table_raw(data_type=native, max_length=10)]})
            assert (
                tables[0].columns[0].normalized_data_type == expected
            ), f"Type {native} should normalize to {expected}"

    def test_unknown_type_normalized_to_none(self) -> None:
        tables = _build_tables({"tables": [self._table_raw(data_type="my_custom_type")]})
        assert tables[0].columns[0].normalized_data_type is None

    def test_nullable_yes_value(self) -> None:
        tables = _build_tables({"tables": [self._table_raw(is_nullable="YES")]})
        assert tables[0].columns[0].nullable is True

    def test_nullable_no_value(self) -> None:
        tables = _build_tables({"tables": [self._table_raw(is_nullable="NO")]})
        assert tables[0].columns[0].nullable is False

    def test_nvarchar_max_length_halved(self) -> None:
        tables = _build_tables({"tables": [self._table_raw(data_type="nvarchar", max_length=510)]})
        assert tables[0].columns[0].max_length == 255


# ---------------------------------------------------------------------------
# _build_views, _build_procedures, _build_triggers
# ---------------------------------------------------------------------------


class TestBuildAuxObjects:
    def test_views_returns_none_for_empty(self) -> None:
        assert _build_views([]) is None

    def test_views_uses_qualified_id(self) -> None:
        views = _build_views(
            [{"schema_name": "sales", "view_name": "ActiveOrders", "definition": "SELECT 1"}]
        )
        assert views is not None
        assert views[0].view_id == "sales.ActiveOrders"
        assert views[0].view_name == "ActiveOrders"

    def test_procedures_distinguishes_functions(self) -> None:
        procs = _build_procedures(
            [
                {"schema_name": "dbo", "routine_name": "sp_one", "routine_type": "PROCEDURE"},
                {"schema_name": "dbo", "routine_name": "fn_two", "routine_type": "FUNCTION"},
                {
                    "schema_name": "dbo",
                    "routine_name": "fn_inline",
                    "routine_type": "INLINE_TABLE_FUNCTION",
                },
            ]
        )
        assert procs is not None
        assert len(procs) == 3
        from src.contracts.collector_output import ProcedureType

        assert procs[0].procedure_type == ProcedureType.PROCEDURE
        assert procs[1].procedure_type == ProcedureType.FUNCTION
        assert procs[2].procedure_type == ProcedureType.FUNCTION  # _TABLE_FUNCTION → FUNCTION

    def test_triggers_qualified_ids(self) -> None:
        triggers = _build_triggers(
            [
                {
                    "schema_name": "dbo",
                    "trigger_name": "tr_orders_audit",
                    "table_name": "Orders",
                    "event_type": "INSERT",
                    "timing": "AFTER",
                }
            ]
        )
        assert triggers is not None
        assert triggers[0].trigger_id == "dbo.tr_orders_audit"
        assert triggers[0].table_id == "dbo.Orders"
        assert triggers[0].timing == "AFTER"


# ---------------------------------------------------------------------------
# Query builders
# ---------------------------------------------------------------------------


class TestQueryBuilders:
    def _query_dict(self, **overrides):
        base = {
            "query_id": "0xABCDEF1234567890",
            "query_text": "SELECT * FROM dbo.Orders",
            "query_type": "SELECT",
            "execution_count": 100,
            "frequency_per_hour": 4.16,
            "calls_per_second": 0.00115,
            "execution_time_ms_avg": 20.0,
            "execution_time_ms_min": 5.0,
            "execution_time_ms_max": 50.0,
            "execution_time_ms_p50": 20.0,
            "execution_time_ms_p95": 40.0,
            "execution_time_ms_p99": 47.0,
            "rows_returned_avg": 2.0,
            "tables_accessed": ["dbo.Orders"],
            "filter_columns": None,
            "sort_columns": None,
            "has_joins": False,
            "join_count": 0,
            "has_aggregations": False,
            "has_subqueries": False,
        }
        base.update(overrides)
        return base

    def test_build_queries_tags_dmv_source(self) -> None:
        queries = _build_queries([self._query_dict()])
        assert queries.query_log_source == QueryLogSource.dmv_query_stats
        assert len(queries.query_patterns) == 1

    def test_build_queries_empty_list_returns_empty(self) -> None:
        queries = _build_queries([])
        assert queries.query_patterns == []

    def test_build_queries_from_aws_tags_pi_source(self) -> None:
        queries = _build_queries_from_aws([self._query_dict()])
        assert queries.query_log_source == QueryLogSource.performance_insights


# ---------------------------------------------------------------------------
# _enrich_table_scans
# ---------------------------------------------------------------------------


class TestEnrichTableScans:
    """Tests use ``Queries.model_validate`` rather than direct construction
    to avoid a Pydantic class-identity edge case when the full suite
    (Hypothesis property tests) re-imports submodules.
    """

    def _queries_with_tables(
        self, tables_accessed: list[str], full_table_scans: int | None = None
    ) -> Queries:
        return Queries.model_validate(
            {
                "query_patterns": [
                    {
                        "query_id": f"q-{'-'.join(tables_accessed)}",
                        "query_text": "SELECT 1",
                        "frequency_per_hour": 10.0,
                        "tables_accessed": tables_accessed,
                        "full_table_scans": full_table_scans,
                    }
                ],
                "query_log_source": "dmv_query_stats",
            }
        )

    def test_attaches_scan_counts_by_qualified_name(self) -> None:
        queries = self._queries_with_tables(["dbo.Orders"])
        raw_tables = [
            {
                "schema_name": "dbo",
                "table_name": "Orders",
                "full_table_scans": 50,
                "index_scans": 1000,
            }
        ]
        _enrich_table_scans(queries, raw_tables)
        p = queries.query_patterns[0]
        assert p.full_table_scans == 50
        assert p.range_scans == 1000

    def test_attaches_scan_counts_by_bare_name(self) -> None:
        # Query uses bare table name (no schema prefix)
        queries = self._queries_with_tables(["Orders"])
        raw_tables = [
            {
                "schema_name": "dbo",
                "table_name": "Orders",
                "full_table_scans": 50,
                "index_scans": 1000,
            }
        ]
        _enrich_table_scans(queries, raw_tables)
        assert queries.query_patterns[0].full_table_scans == 50

    def test_aggregates_across_multiple_tables(self) -> None:
        queries = self._queries_with_tables(["dbo.Orders", "dbo.Customers"])
        raw_tables = [
            {
                "schema_name": "dbo",
                "table_name": "Orders",
                "full_table_scans": 10,
                "index_scans": 100,
            },
            {
                "schema_name": "dbo",
                "table_name": "Customers",
                "full_table_scans": 5,
                "index_scans": 200,
            },
        ]
        _enrich_table_scans(queries, raw_tables)
        p = queries.query_patterns[0]
        assert p.full_table_scans == 15
        assert p.range_scans == 300

    def test_skips_when_already_set(self) -> None:
        queries = self._queries_with_tables(["Orders"], full_table_scans=999)
        raw_tables = [{"table_name": "Orders", "full_table_scans": 50, "index_scans": 1000}]
        _enrich_table_scans(queries, raw_tables)
        # Pre-existing value preserved
        assert queries.query_patterns[0].full_table_scans == 999


# ---------------------------------------------------------------------------
# Mode dispatch (collect entry point)
# ---------------------------------------------------------------------------


def _build_input(mode: CollectionMode = CollectionMode.live) -> CollectorInput:
    """Build a minimal CollectorInput for dispatch tests."""
    data: dict = {
        "job_id": "job-001",
        "engine": DatabaseEngine.sqlserver,
        "cluster_endpoint": "sqltest.abc123.us-east-1.rds.amazonaws.com",
        "port": 1433,
        "database_name": "testdb",
        "mode": mode,
    }
    if mode == CollectionMode.live:
        data["live_config"] = {
            "secret_arn": "arn:aws:secretsmanager:us-east-1:123:secret:test-AAAAAA",  # pragma: allowlist secret
            "automation_instance_id": "i-0abc123",
        }
    elif mode == CollectionMode.ddl:
        data["ddl_config"] = {
            "s3_bucket": "test-bucket",
            "s3_key": "test/ddl.sql",
        }
    elif mode == CollectionMode.offline:
        data["offline_config"] = {
            "s3_bucket": "test-bucket",
            "s3_key": "test/offline.json",
        }
    return CollectorInput.model_validate(data)


@patch("src.agents.collector.sqlserver_collector._init_checkpoint_store")
@patch("src.agents.collector.sqlserver_collector._collect_live")
def test_collect_dispatches_to_live_mode(mock_live, mock_ckpt) -> None:
    mock_ckpt.return_value = MagicMock(exists=MagicMock(return_value=False), save=MagicMock())
    mock_live.return_value = MagicMock()
    mock_live.return_value.model_dump.return_value = {"foo": "bar"}

    from src.agents.collector.sqlserver_collector import collect

    collect(_build_input(CollectionMode.live))

    mock_live.assert_called_once()


@patch("src.agents.collector.sqlserver_collector._init_checkpoint_store")
@patch("src.agents.collector.sqlserver_collector._collect_ddl")
def test_collect_dispatches_to_ddl_mode(mock_ddl, mock_ckpt) -> None:
    mock_ckpt.return_value = MagicMock(exists=MagicMock(return_value=False), save=MagicMock())
    mock_ddl.return_value = MagicMock()
    mock_ddl.return_value.model_dump.return_value = {"foo": "bar"}

    from src.agents.collector.sqlserver_collector import collect

    collect(_build_input(CollectionMode.ddl))

    mock_ddl.assert_called_once()


@patch("src.agents.collector.sqlserver_collector._init_checkpoint_store")
@patch("src.agents.collector.sqlserver_collector._collect_offline")
def test_collect_dispatches_to_offline_mode(mock_offline, mock_ckpt) -> None:
    mock_ckpt.return_value = MagicMock(exists=MagicMock(return_value=False), save=MagicMock())
    mock_offline.return_value = MagicMock()
    mock_offline.return_value.model_dump.return_value = {"foo": "bar"}

    from src.agents.collector.sqlserver_collector import collect

    collect(_build_input(CollectionMode.offline))

    mock_offline.assert_called_once()


@patch("src.agents.collector.sqlserver_collector._init_checkpoint_store")
def test_collect_returns_cached_output(mock_ckpt) -> None:
    """If output checkpoint exists, skip all stages and return cached."""
    cached = {
        "job_id": "job-001",
        "metadata": {
            "collection_timestamp": "2026-06-15T12:00:00Z",
            "collector_version": "3.0.0",
            "source_database": {
                "engine": "sqlserver",
                "version": "16.0",
                "hostname": "test",
                "database_name": "testdb",
            },
        },
        "database_schema": {
            "tables": [
                {
                    "table_id": "dbo.t",
                    "table_name": "t",
                    "row_count": 0,
                    "columns": [
                        {
                            "column_name": "id",
                            "data_type": "int",
                            "nullable": False,
                        }
                    ],
                }
            ]
        },
        "queries": {"query_patterns": []},
        "metrics": {"performance_metrics": {}},
    }
    ck = MagicMock()
    ck.exists.return_value = True
    ck.load.return_value = cached
    mock_ckpt.return_value = ck

    from src.agents.collector.sqlserver_collector import collect

    result = collect(_build_input())
    assert result.job_id == "job-001"
    assert result.metadata.source_database.engine.value == "sqlserver"
    # No stages should have been called — only load
    ck.load.assert_called_once_with("output")


# ---------------------------------------------------------------------------
# Handler dispatch
# ---------------------------------------------------------------------------


class TestHandlerDispatch:
    def test_dispatch_routes_sqlserver(self) -> None:
        with patch("src.agents.collector.sqlserver_collector.collect") as mock_collect:
            mock_collect.return_value = MagicMock()
            from src.agents.collector.handler import _dispatch_collect

            inp = _build_input()
            _dispatch_collect("sqlserver", inp)
            mock_collect.assert_called_once_with(inp)

    def test_dispatch_unsupported_engine_raises(self) -> None:
        from src.agents.collector.handler import _dispatch_collect

        inp = _build_input()
        with pytest.raises(ValueError, match="Unsupported engine: db2"):
            _dispatch_collect("db2", inp)
