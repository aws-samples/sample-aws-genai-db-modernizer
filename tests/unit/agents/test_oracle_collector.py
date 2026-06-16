"""Tests for Oracle collector orchestrator."""

from unittest.mock import MagicMock, patch

from src.agents.collector.oracle_collector import (
    _ORACLE_TYPE_MAP,
    _build_procedures,
    _build_queries,
    _build_tables,
    _build_triggers,
    _build_views,
    _merge_queries,
    _normalize_max_length,
    _parse_oracle_version,
    collect,
)
from src.contracts.collector_input import CollectionMode, CollectorInput
from src.contracts.collector_output import NormalizedDataType, Queries, QueryLogSource


class TestParseOracleVersion:
    def test_full_version(self):
        banner = "Oracle Database 19c Enterprise Edition Release 19.0.0.0.0 - Production"
        assert _parse_oracle_version(banner) == "19.0.0.0.0"

    def test_short_version(self):
        assert _parse_oracle_version("Oracle Database 21c") == "21c"

    def test_unknown(self):
        assert _parse_oracle_version("") == "unknown"

    def test_fallback(self):
        assert _parse_oracle_version("Some other banner text 12.2.0.1.0") == "12.2.0.1.0"


class TestNormalizeMaxLength:
    def test_number_uses_precision(self):
        col = {"data_type": "NUMBER", "max_length": "22", "data_precision": "10", "char_used": None}
        assert _normalize_max_length(col) == 10

    def test_number_no_precision(self):
        col = {"data_type": "NUMBER", "max_length": "22", "data_precision": None, "char_used": None}
        assert _normalize_max_length(col) is None

    def test_varchar2_byte_semantic(self):
        col = {
            "data_type": "VARCHAR2",
            "max_length": "100",
            "char_used": "B",
            "data_precision": None,
        }
        assert _normalize_max_length(col) == 100

    def test_varchar2_char_semantic(self):
        col = {
            "data_type": "VARCHAR2",
            "max_length": "100",
            "char_used": "C",
            "data_precision": None,
        }
        assert _normalize_max_length(col) == 100  # char_used=C means length IS in chars

    def test_nvarchar2_byte_semantic(self):
        col = {
            "data_type": "NVARCHAR2",
            "max_length": "200",
            "char_used": "B",
            "data_precision": None,
        }
        assert _normalize_max_length(col) == 100  # /2 for nvarchar2

    def test_nvarchar2_char_semantic(self):
        col = {
            "data_type": "NVARCHAR2",
            "max_length": "200",
            "char_used": "C",
            "data_precision": None,
        }
        assert _normalize_max_length(col) == 50  # /4 for UTF-8 worst case

    def test_none_length(self):
        col = {"data_type": "CLOB", "max_length": None, "char_used": None, "data_precision": None}
        assert _normalize_max_length(col) is None


class TestOracleTypeMap:
    def test_common_types(self):
        assert _ORACLE_TYPE_MAP["number"] == NormalizedDataType.decimal
        assert _ORACLE_TYPE_MAP["varchar2"] == NormalizedDataType.string
        assert _ORACLE_TYPE_MAP["date"] == NormalizedDataType.timestamp
        assert _ORACLE_TYPE_MAP["clob"] == NormalizedDataType.text
        assert _ORACLE_TYPE_MAP["blob"] == NormalizedDataType.binary
        assert _ORACLE_TYPE_MAP["xmltype"] == NormalizedDataType.xml


class TestBuildTables:
    def test_basic_table(self):
        schema_raw = {
            "tables": [
                {
                    "table_name": "orders",
                    "schema_name": "app",
                    "row_count": "5000",
                    "data_size_mb": "12.5",
                    "columns": [
                        {
                            "column_name": "order_id",
                            "ordinal_position": "1",
                            "data_type": "number",
                            "max_length": "22",
                            "char_used": None,
                            "is_nullable": "N",
                            "column_default": None,
                            "data_precision": "10",
                            "data_scale": "0",
                            "is_identity": "YES",
                        }
                    ],
                    "indexes": [],
                    "foreign_keys": [],
                    "primary_key": ["order_id"],
                    "sample_data": None,
                }
            ]
        }
        tables = _build_tables(schema_raw)
        assert len(tables) == 1
        t = tables[0]
        assert t.table_id == "app.orders"
        assert t.table_name == "orders"
        assert t.row_count == 5000
        assert t.columns[0].column_name == "order_id"
        assert t.columns[0].is_auto_increment is True
        assert t.columns[0].normalized_data_type == NormalizedDataType.decimal


class TestBuildViews:
    def test_none_when_empty(self):
        assert _build_views([]) is None

    def test_basic_view(self):
        views = _build_views([{"view_name": "v_orders", "schema_name": "app"}])
        assert views is not None
        assert views[0].view_name == "v_orders"


class TestBuildProcedures:
    def test_function_type(self):
        procs = _build_procedures(
            [
                {
                    "routine_name": "calc_total",
                    "schema_name": "app",
                    "routine_type": "FUNCTION",
                    "language": "PL/SQL",
                }
            ]
        )
        assert procs is not None
        from src.contracts.collector_output import ProcedureType

        assert procs[0].procedure_type == ProcedureType.FUNCTION


class TestBuildTriggers:
    def test_event_split(self):
        triggers = _build_triggers(
            [
                {
                    "trigger_name": "trg_audit",
                    "table_name": "orders",
                    "schema_name": "app",
                    "timing": "AFTER",
                    "event_type": "INSERT OR UPDATE",
                }
            ]
        )
        assert triggers is not None
        from src.contracts.collector_output import TriggerEventType

        assert triggers[0].event_type == TriggerEventType.INSERT


class TestBuildQueries:
    def test_query_log_source(self):
        q = _build_queries([{"query_id": "q1", "query_text": "SELECT 1", "execution_count": 10}])
        assert q.query_log_source == QueryLogSource.v_dollar_sql


class TestMergeQueries:
    def test_pi_enriches_existing(self):
        live = Queries.model_validate(
            {
                "query_patterns": [
                    {
                        "query_id": "abc",
                        "query_text": "SELECT...",
                        "frequency_per_hour": 10,
                        "tables_accessed": ["orders"],
                    }
                ],
                "query_log_source": "v_dollar_sql",
            }
        )
        aws_patterns = [
            {
                "query_id": "abc",
                "query_text": "SELECT full text here...",
                "db_load_contribution_percent": 25.5,
                "tables_accessed": ["orders"],
            }
        ]
        merged = _merge_queries(live, aws_patterns)
        assert merged.query_patterns[0].query_text == "SELECT full text here..."
        assert merged.query_patterns[0].db_load_contribution_percent == 25.5

    def test_pi_appends_new(self):
        live = Queries.model_validate({"query_patterns": [], "query_log_source": "v_dollar_sql"})
        aws_patterns = [
            {
                "query_id": "new1",
                "query_text": "INSERT INTO t",
                "frequency_per_hour": 5,
                "tables_accessed": ["t"],
            }
        ]
        merged = _merge_queries(live, aws_patterns)
        assert len(merged.query_patterns) == 1
        assert merged.query_patterns[0].query_id == "new1"


class TestHandlerDispatch:
    @patch("src.agents.collector.oracle_collector.collect")
    def test_oracle_dispatch(self, mock_collect):
        """Handler routes engine='oracle' to oracle_collector."""
        from src.agents.collector.handler import _dispatch_collect

        mock_collect.return_value = MagicMock()
        inp = MagicMock(spec=CollectorInput)
        _dispatch_collect("oracle", inp)
        mock_collect.assert_called_once_with(inp)


class TestModeDispatch:
    @patch("src.agents.collector.oracle_collector._collect_live")
    @patch("src.agents.collector.oracle_collector._init_checkpoint_store")
    def test_live_mode(self, mock_ckpt, mock_live):
        mock_ckpt_inst = MagicMock()
        mock_ckpt_inst.exists.return_value = False
        mock_ckpt.return_value = mock_ckpt_inst
        mock_live.return_value = MagicMock()
        mock_live.return_value.model_dump.return_value = {}

        inp = MagicMock(spec=CollectorInput)
        inp.mode = CollectionMode.live
        inp.job_id = "test-001"
        collect(inp)
        mock_live.assert_called_once()

    @patch("src.agents.collector.oracle_collector._collect_ddl")
    @patch("src.agents.collector.oracle_collector._init_checkpoint_store")
    def test_ddl_mode(self, mock_ckpt, mock_ddl):
        mock_ckpt_inst = MagicMock()
        mock_ckpt_inst.exists.return_value = False
        mock_ckpt.return_value = mock_ckpt_inst
        mock_ddl.return_value = MagicMock()
        mock_ddl.return_value.model_dump.return_value = {}

        inp = MagicMock(spec=CollectorInput)
        inp.mode = CollectionMode.ddl
        inp.job_id = "test-002"
        collect(inp)
        mock_ddl.assert_called_once()

    @patch("src.agents.collector.oracle_collector._init_checkpoint_store")
    def test_cached_output_skips_collection(self, mock_ckpt):
        mock_ckpt_inst = MagicMock()
        mock_ckpt_inst.exists.return_value = True
        mock_ckpt_inst.load.return_value = {
            "job_id": "cached",
            "metadata": {
                "collection_timestamp": "2026-01-01T00:00:00Z",
                "collector_version": "0.1.0",
                "source_database": {
                    "engine": "oracle",
                    "version": "19.0.0.0.0",
                    "hostname": "h",
                    "database_name": "db",
                },
            },
            "database_schema": {
                "tables": [
                    {
                        "table_id": "app.t1",
                        "table_name": "t1",
                        "row_count": 1,
                        "columns": [
                            {"column_name": "id", "data_type": "number", "nullable": False}
                        ],
                    }
                ]
            },
            "queries": {"query_patterns": []},
            "metrics": {"performance_metrics": {}},
        }
        mock_ckpt.return_value = mock_ckpt_inst

        inp = MagicMock(spec=CollectorInput)
        inp.mode = CollectionMode.live
        inp.job_id = "cached"
        result = collect(inp)
        assert result.job_id == "cached"
