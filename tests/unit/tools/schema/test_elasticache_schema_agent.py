"""Unit tests for ElastiCache schema agent contract alignment.

Validates that the ElastiCache output contract uses structured TradeOff objects,
source_query_ids traceability, and the synthesis handler can summarize it.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pydantic
import pytest

from src.contracts.elasticache_model_output import ElastiCacheModelOutputContract
from src.contracts.schema_design_output import TradeOff

# ---------------------------------------------------------------------------
# Contract validation tests
# ---------------------------------------------------------------------------


class TestElastiCacheContract:
    """Verify ElastiCache contract matches other engines' format."""

    def _make_minimal_output(self) -> dict:
        """Build a minimal valid ElastiCache schema output."""
        return {
            "contract_version": "1.0",
            "job_id": "test-job",
            "source_database": "test_db",
            "target_engine": "elasticache",
            "key_designs": [
                {
                    "key_pattern": "cache:user:{user_id}",
                    "data_type": "hash",
                    "use_case": "caching",
                    "source_tables": ["users"],
                    "ttl_seconds": 3600,
                    "example_key": "cache:user:42",
                    "example_value": '{"name": "Alice"}',
                    "rationale": "Fast user lookups by ID",
                    "estimated_key_count": 1000,
                    "estimated_avg_value_bytes": 512,
                }
            ],
            "access_patterns": [
                {
                    "pattern_id": "AP-1",
                    "description": "Get user profile",
                    "operation": "HGETALL",
                    "key_pattern": "cache:user:{user_id}",
                    "command_example": "HGETALL cache:user:42",
                    "source_query_ids": ["abc123def456"],
                }
            ],
            "cache_invalidation": [
                {
                    "key_pattern": "cache:user:{user_id}",
                    "strategy": "write_through",
                    "description": "Update on every write",
                    "source_write_query_ids": ["write_query_1"],
                }
            ],
            "unsupported_patterns": [],
            "migration_notes": [],
            "trade_offs": [
                {
                    "description": "Denormalized user data for fast reads",
                    "impact": "Higher memory usage, stale data if TTL not managed",
                    "source_tables": ["users"],
                    "target_tables": ["cache:user:{user_id}"],
                    "query_ids": ["abc123def456"],
                    "engine": "elasticache",
                }
            ],
            "validation_passed": True,
            "validation_failures": [],
        }

    def test_trade_offs_accept_structured_tradeoff(self):
        """trade_offs field accepts structured TradeOff objects."""
        data = self._make_minimal_output()
        output = ElastiCacheModelOutputContract.model_validate(data)

        assert len(output.trade_offs) == 1
        assert isinstance(output.trade_offs[0], TradeOff)
        assert output.trade_offs[0].description == "Denormalized user data for fast reads"
        assert output.trade_offs[0].engine == "elasticache"
        assert output.trade_offs[0].query_ids == ["abc123def456"]

    def test_trade_offs_reject_plain_strings(self):
        """trade_offs field no longer accepts plain strings."""
        data = self._make_minimal_output()
        data["trade_offs"] = ["This is a plain string trade-off"]

        with pytest.raises(pydantic.ValidationError):
            ElastiCacheModelOutputContract.model_validate(data)

    def test_access_pattern_has_source_query_ids(self):
        """Access patterns must include source_query_ids."""
        data = self._make_minimal_output()
        output = ElastiCacheModelOutputContract.model_validate(data)

        assert output.access_patterns[0].source_query_ids == ["abc123def456"]

    def test_access_pattern_requires_source_query_ids(self):
        """Access patterns require at least one source_query_id."""
        data = self._make_minimal_output()
        data["access_patterns"][0]["source_query_ids"] = []

        with pytest.raises(pydantic.ValidationError):
            ElastiCacheModelOutputContract.model_validate(data)

    def test_cache_invalidation_has_source_write_query_ids(self):
        """Cache invalidation strategies include write query IDs."""
        data = self._make_minimal_output()
        output = ElastiCacheModelOutputContract.model_validate(data)

        assert output.cache_invalidation[0].source_write_query_ids == ["write_query_1"]

    def test_unsupported_pattern_has_source_query_ids(self):
        """Unsupported patterns include source_query_ids for traceability."""
        data = self._make_minimal_output()
        data["unsupported_patterns"] = [
            {
                "source_query_ids": ["complex_join_query_1"],
                "reason": "Multi-table JOIN not supported",
                "workaround": "Use DocumentDB instead",
            }
        ]
        output = ElastiCacheModelOutputContract.model_validate(data)

        assert output.unsupported_patterns[0].source_query_ids == ["complex_join_query_1"]

    def test_full_contract_serialization_roundtrip(self):
        """Contract can serialize and deserialize without data loss."""
        data = self._make_minimal_output()
        output = ElastiCacheModelOutputContract.model_validate(data)
        serialized = json.loads(output.model_dump_json())
        output2 = ElastiCacheModelOutputContract.model_validate(serialized)

        assert output2.trade_offs[0].description == output.trade_offs[0].description
        assert output2.access_patterns[0].source_query_ids == ["abc123def456"]


# ---------------------------------------------------------------------------
# Synthesis handler tests
# ---------------------------------------------------------------------------


class TestElastiCacheSynthesisSummary:
    """Verify synthesis handler can summarize ElastiCache schemas."""

    def test_build_elasticache_schema_summary(self):
        """_build_elasticache_schema_summary produces correct structure."""
        from src.agents.referee.synthesis_handler import _build_elasticache_schema_summary

        schema = {
            "key_designs": [
                {
                    "key_pattern": "cache:user:{user_id}",
                    "data_type": "hash",
                    "source_tables": ["users"],
                    "ttl_seconds": 3600,
                },
                {
                    "key_pattern": "leaderboard:{board_id}",
                    "data_type": "sorted_set",
                    "source_tables": ["scores"],
                    "ttl_seconds": None,
                },
            ],
            "access_patterns": [
                {"pattern_id": "AP-1"},
                {"pattern_id": "AP-2"},
                {"pattern_id": "AP-3"},
            ],
            "validation_passed": True,
            "trade_offs": [
                {
                    "description": "Memory vs latency",
                    "impact": "Higher memory",
                    "source_tables": ["users"],
                    "target_tables": ["cache:user:{user_id}"],
                    "query_ids": ["q1"],
                    "engine": "elasticache",
                }
            ],
            "unsupported_patterns": [{"source_query_ids": ["q9"], "reason": "Too complex"}],
            "migration_notes": [],
        }

        summary = _build_elasticache_schema_summary(schema)

        assert summary["status"] == "completed"
        assert summary["validation_passed"] is True
        assert len(summary["tables"]) == 2
        assert summary["tables"][0]["table_name"] == "cache:user:{user_id}"
        assert summary["tables"][0]["aggregate_pattern"] == "hash"
        assert summary["tables"][0]["source_tables"] == ["users"]
        assert summary["tables"][0]["ttl_seconds"] == 3600
        assert summary["tables"][1]["aggregate_pattern"] == "sorted_set"
        assert summary["access_pattern_count"] == 3
        assert len(summary["trade_offs"]) == 1
        assert len(summary["unsupported_patterns"]) == 1

    def test_schema_summaries_dispatches_elasticache(self):
        """_build_schema_summaries routes elasticache to dedicated handler."""
        from src.agents.referee.synthesis_handler import _build_schema_summaries

        mock_data = MagicMock()
        mock_data.engines = {
            "elasticache": MagicMock(
                schema_design={
                    "key_designs": [
                        {"key_pattern": "k:1", "data_type": "string", "source_tables": []}
                    ],
                    "access_patterns": [{"pattern_id": "AP-1"}],
                    "validation_passed": True,
                    "trade_offs": [],
                    "unsupported_patterns": [],
                    "migration_notes": [],
                }
            )
        }

        summaries = _build_schema_summaries(mock_data)

        assert "elasticache" in summaries
        assert summaries["elasticache"]["status"] == "completed"
        assert summaries["elasticache"]["access_pattern_count"] == 1


# ---------------------------------------------------------------------------
# Agent parameter passing tests
# ---------------------------------------------------------------------------


class TestElastiCacheAgentParams:
    """Verify the agent accepts path parameters instead of env vars."""

    def test_run_elasticache_schema_agent_accepts_paths(self):
        """run_elasticache_schema_agent accepts collector_path and analysis_path."""
        import inspect

        from src.tools.schema.elasticache_schema_agent import run_elasticache_schema_agent

        sig = inspect.signature(run_elasticache_schema_agent)
        params = list(sig.parameters.keys())

        assert "collector_path" in params
        assert "analysis_path" in params
        assert "revision_context_path" in params
