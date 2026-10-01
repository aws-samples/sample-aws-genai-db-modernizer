"""Synthesis reads every engine's schema output format.

ElastiCache emits ``key_designs`` and Aurora emits ``table_definitions`` with no
``source_tables`` (tables carry over 1:1). Synthesis previously only understood
DynamoDB-shaped ``table_definitions`` plus OpenSearch/DocumentDB fallbacks, so
ElastiCache ranked as ``schema_design_available: false`` and Aurora produced no
table mappings and a DynamoDB-shaped summary.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from src.agents.referee.synthesis_handler import _build_schema_summaries
from src.agents.referee.synthesis_report import schema_table_defs

_ELASTICACHE = {
    "key_designs": [
        {"key_pattern": "wp_posts:{id}", "data_type": "hash", "source_tables": ["wp.wp_posts"]},
        {"key_pattern": "wp_terms:{id}", "data_type": "hash", "source_tables": ["wp.wp_terms"]},
    ],
    "access_patterns": [{"pattern_id": "EC-AP-1"}],
    "validation_passed": True,
}


def _aurora(engine: str) -> dict:
    return {
        "source_database": "wp",
        "target_engine": engine,
        "migration_strategy": "carry_over",
        "table_definitions": [
            {
                "table_name": "wp_posts",
                "columns": [{"name": "ID"}, {"name": "post_title"}],
                "primary_key": ["ID"],
                "indexes": ["CREATE INDEX a ON wp_posts (post_type)", "CREATE INDEX b ON x (y)"],
                "foreign_keys": [],
            },
            {
                "table_name": "wp_postmeta",
                "columns": [{"name": "meta_id"}],
                "primary_key": ["meta_id"],
                "indexes": [],
                "foreign_keys": ["ALTER TABLE wp_postmeta ADD FOREIGN KEY ..."],
            },
        ],
        "generated_ddl": "CREATE TABLE wp_posts (...);",
        "app_layer_notes": [{"feature": "sql_mode", "source_object": "wp_posts"}],
        "optimizations": [{"category": "index", "target": "wp_posts"}],
        "trade_offs": [{"description": "t"}],
        "validation_passed": True,
    }


class TestSchemaTableDefs:
    def test_elasticache_key_designs(self) -> None:
        defs = schema_table_defs("elasticache", _ELASTICACHE)
        assert [d["table_name"] for d in defs] == ["wp_posts:{id}", "wp_terms:{id}"]
        assert defs[0]["source_tables"] == ["wp.wp_posts"]
        assert defs[0]["aggregate_pattern"] == "hash"

    @pytest.mark.parametrize("engine", ["aurora_mysql", "aurora_postgresql"])
    def test_aurora_tables_map_back_to_source(self, engine: str) -> None:
        defs = schema_table_defs(engine, _aurora(engine))
        assert [d["source_tables"] for d in defs] == [["wp.wp_posts"], ["wp.wp_postmeta"]]
        assert defs[0]["aggregate_pattern"] == "relational_table"

    def test_dynamodb_passes_through(self) -> None:
        schema = {"table_definitions": [{"table_name": "t", "source_tables": ["wp.a"]}]}
        assert schema_table_defs("dynamodb", schema) == schema["table_definitions"]

    def test_does_not_mutate_schema(self) -> None:
        schema = {"table_definitions": [], "index_designs": [{"index_name": "i"}]}
        schema_table_defs("opensearch", schema)
        assert schema["table_definitions"] == []


class TestAuroraSchemaSummary:
    @pytest.mark.parametrize("engine", ["aurora_mysql", "aurora_postgresql"])
    def test_summary_reports_relational_shape(self, engine: str) -> None:
        data = MagicMock()
        data.engines = {engine: MagicMock(schema_design=_aurora(engine))}
        summary = _build_schema_summaries(data)[engine]

        assert summary["status"] == "completed"
        assert summary["migration_strategy"] == "carry_over"
        assert summary["index_count"] == 2
        assert summary["foreign_key_count"] == 1
        assert summary["tables"][0]["column_count"] == 2
        assert summary["tables"][0]["index_count"] == 2
        assert summary["tables"][0]["source_tables"] == ["wp.wp_posts"]
        assert len(summary["optimizations"]) == 1
        assert len(summary["app_layer_notes"]) == 1
        assert summary["ddl_available"] is True

    def test_missing_aurora_schema_is_not_available(self) -> None:
        data = MagicMock()
        data.engines = {"aurora_mysql": MagicMock(schema_design=None)}
        assert _build_schema_summaries(data)["aurora_mysql"] == {"status": "not_available"}
