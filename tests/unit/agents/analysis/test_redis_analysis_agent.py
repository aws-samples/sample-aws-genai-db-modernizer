"""
Unit tests for Redis Analysis Agent
"""

import time

import pytest

from src.agents.analysis.redis_analysis_agent import analyze_for_redis
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from tests.fixtures.gaming_collector_output import get_gaming_collector_output
from tests.fixtures.generate_synthetic_collector_output import generate_collector_output
from tests.fixtures.redis_pattern_fixtures import (
    get_anti_pattern_fixture,
    get_caching_fixture,
    get_geospatial_fixture,
    get_leaderboard_fixture,
    get_session_store_fixture,
    get_timeseries_fixture,
)


def test_redis_analysis_basic():
    """Test basic Redis analysis with minimal collector output."""
    # Minimal collector output
    collector_output = {
        "job_id": "test-job-123",
        "contract_version": "3.0",
        "metadata": {
            "collector_version": "1.0.0",
            "source_database": {
                "engine": "mysql",
                "database_name": "test_db",
                "database_size_gb": 10.0,
            },
            "collection_timestamp": "2026-02-16T14:00:00Z",
        },
        "database_schema": {
            "tables": [
                {
                    "table_id": "users",
                    "table_name": "users",
                    "row_count": 1000,
                }
            ]
        },
        "queries": {
            "query_patterns": [
                {
                    "query_id": "q1",
                    "query_text": "SELECT * FROM users WHERE user_id = ?",
                    "query_type": "SELECT",
                    "calls_per_second": 5.0,
                    "execution_time_ms_avg": 2.5,
                    "tables_accessed": ["users"],
                }
            ]
        },
        "metrics": {"performance_metrics": {}},
    }

    # Create analysis input
    analysis_input = AnalysisInput(
        job_id="test-job-123",
        collector_output=collector_output,
        target_database=TargetDatabase.elasticache,
    )

    # Run analysis
    result = analyze_for_redis(analysis_input)

    # Assertions
    assert result.contract_version == "2.1"
    assert result.agent_metadata.agent_name == "redis-analysis-agent"
    assert result.agent_metadata.target_database.value == "elasticache"
    assert len(result.table_recommendations) > 0
    assert result.workload_analysis is not None
    assert result.cost_estimate.monthly_cost_usd > 0


def test_redis_caching_pattern_detection():
    """Test detection of caching patterns."""
    collector_output = {
        "job_id": "test-job-123",
        "contract_version": "3.0",
        "metadata": {
            "collector_version": "1.0.0",
            "source_database": {"engine": "mysql", "database_size_gb": 10.0},
            "collection_timestamp": "2026-02-16T14:00:00Z",
        },
        "database_schema": {"tables": []},
        "queries": {
            "query_patterns": [
                {
                    "query_id": "q1",
                    "query_text": "SELECT * FROM products WHERE id = ?",
                    "query_type": "SELECT",
                    "calls_per_second": 10.0,  # High frequency
                    "tables_accessed": ["products"],
                },
                {
                    "query_id": "q2",
                    "query_text": "SELECT * FROM products WHERE category = ?",
                    "query_type": "SELECT",
                    "calls_per_second": 5.0,
                    "tables_accessed": ["products"],
                },
            ]
        },
        "metrics": {"performance_metrics": {}},
    }

    analysis_input = AnalysisInput(
        job_id="test-job-123",
        collector_output=collector_output,
        target_database=TargetDatabase.elasticache,
    )

    result = analyze_for_redis(analysis_input)

    # Should detect caching pattern
    patterns = result.workload_analysis.patterns_detected
    assert len(patterns) > 0
    assert any(p.pattern_type == "caching" for p in patterns)


def test_redis_session_store_detection():
    """Test detection of session store patterns."""
    collector_output = {
        "job_id": "test-job-123",
        "contract_version": "3.0",
        "metadata": {
            "collector_version": "1.0.0",
            "source_database": {"engine": "postgresql", "database_size_gb": 5.0},
            "collection_timestamp": "2026-02-16T14:00:00Z",
        },
        "database_schema": {"tables": []},
        "queries": {
            "query_patterns": [
                {
                    "query_id": "q1",
                    "query_text": "SELECT * FROM sessions WHERE user_id = ?",
                    "query_type": "SELECT",
                    "calls_per_second": 2.0,
                    "tables_accessed": ["sessions"],
                }
            ]
        },
        "metrics": {"performance_metrics": {}},
    }

    analysis_input = AnalysisInput(
        job_id="test-job-123",
        collector_output=collector_output,
        target_database=TargetDatabase.elasticache,
    )

    result = analyze_for_redis(analysis_input)

    # Should detect session store pattern
    patterns = result.workload_analysis.patterns_detected
    assert any(p.pattern_type == "session-store" for p in patterns)


def test_redis_leaderboard_detection():
    """Test detection of leaderboard patterns."""
    collector_output = {
        "job_id": "test-job-123",
        "contract_version": "3.0",
        "metadata": {
            "collector_version": "1.0.0",
            "source_database": {"engine": "mysql", "database_size_gb": 10.0},
            "collection_timestamp": "2026-02-16T14:00:00Z",
        },
        "database_schema": {"tables": []},
        "queries": {
            "query_patterns": [
                {
                    "query_id": "q1",
                    "query_text": "SELECT * FROM scores ORDER BY score DESC LIMIT 10",
                    "query_type": "SELECT",
                    "calls_per_second": 1.0,
                    "tables_accessed": ["scores"],
                }
            ]
        },
        "metrics": {"performance_metrics": {}},
    }

    analysis_input = AnalysisInput(
        job_id="test-job-123",
        collector_output=collector_output,
        target_database=TargetDatabase.elasticache,
    )

    result = analyze_for_redis(analysis_input)

    # Should detect leaderboard pattern
    patterns = result.workload_analysis.patterns_detected
    assert any(p.pattern_type == "leaderboard" for p in patterns)


def test_redis_ecommerce_differentiated_scoring():
    """Test that e-commerce fixture produces differentiated scores across tables."""
    from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output

    collector_output = get_ecommerce_collector_output()
    analysis_input = AnalysisInput(
        job_id="test-ecommerce",
        collector_output=collector_output,
        target_database=TargetDatabase.elasticache,
    )

    result = analyze_for_redis(analysis_input)
    scores = {r.table_id: r.confidence_score for r in result.table_recommendations}

    # Scores should not all be the same
    assert len(set(scores.values())) > 1, f"All scores identical: {scores}"

    # Sessions table should score higher than daily_analytics (sessions has caching + session patterns)
    assert scores["ecommerce.sessions"] > scores["ecommerce.daily_analytics"]

    # Tables involved in patterns should have supporting_patterns populated
    recs_by_id = {r.table_id: r for r in result.table_recommendations}
    sessions_rec = recs_by_id["ecommerce.sessions"]
    assert sessions_rec.supporting_patterns, "sessions should have supporting_patterns"

    # Products table is in the large-result-set anti-pattern (catalog export query)
    products_rec = recs_by_id["ecommerce.products"]
    assert products_rec.concerns, "products should have concerns from anti-pattern"

    # Verify anti-pattern tables have MEDIUM migration complexity
    assert products_rec.migration_complexity == "MEDIUM"


# ---------------------------------------------------------------------------
# Per-pattern tests using focused fixtures
# ---------------------------------------------------------------------------


class TestRedisPatternFixtures:
    """Each focused fixture triggers exactly one pattern and no others."""

    def _run(self, fixture: dict) -> tuple[set[str], set[str]]:
        """Run analysis and return (pattern_types, anti_pattern_types)."""
        analysis_input = AnalysisInput(
            job_id=fixture["job_id"],
            collector_output=fixture,
            target_database=TargetDatabase.elasticache,
        )
        result = analyze_for_redis(analysis_input)
        pattern_types = {p.pattern_type for p in result.workload_analysis.patterns_detected}
        ap_types = {
            ap.anti_pattern_type for ap in (result.workload_analysis.anti_patterns_detected or [])
        }
        return pattern_types, ap_types

    def test_caching_only(self):
        """Caching fixture triggers caching pattern and no others."""
        patterns, anti_patterns = self._run(get_caching_fixture())
        assert "caching" in patterns
        assert patterns == {"caching"}
        assert not anti_patterns

    def test_session_store_only(self):
        """Session fixture triggers session-store pattern and no others."""
        patterns, anti_patterns = self._run(get_session_store_fixture())
        assert "session-store" in patterns
        assert patterns == {"session-store"}
        assert not anti_patterns

    def test_leaderboard_only(self):
        """Leaderboard fixture triggers leaderboard pattern and no others."""
        patterns, anti_patterns = self._run(get_leaderboard_fixture())
        assert "leaderboard" in patterns
        assert patterns == {"leaderboard"}
        assert not anti_patterns

    def test_geospatial_only(self):
        """Geospatial fixture triggers geospatial pattern and no others."""
        patterns, anti_patterns = self._run(get_geospatial_fixture())
        assert "geospatial" in patterns
        assert patterns == {"geospatial"}
        assert not anti_patterns

    def test_timeseries_only(self):
        """Time-series fixture triggers time-series pattern and no others."""
        patterns, anti_patterns = self._run(get_timeseries_fixture())
        assert "time-series" in patterns
        assert patterns == {"time-series"}
        assert not anti_patterns

    def test_anti_pattern_only(self):
        """Anti-pattern fixture triggers large-result-sets and no patterns."""
        patterns, anti_patterns = self._run(get_anti_pattern_fixture())
        assert not patterns
        assert "large-result-sets" in anti_patterns


def test_gaming_vertical_all_patterns():
    """Gaming fixture produces all 5 pattern types + anti-pattern."""
    collector_output = get_gaming_collector_output()
    analysis_input = AnalysisInput(
        job_id="test-gaming",
        collector_output=collector_output,
        target_database=TargetDatabase.elasticache,
    )
    result = analyze_for_redis(analysis_input)

    pattern_types = {p.pattern_type for p in result.workload_analysis.patterns_detected}
    assert "caching" in pattern_types
    assert "session-store" in pattern_types
    assert "leaderboard" in pattern_types
    assert "time-series" in pattern_types
    assert "geospatial" in pattern_types

    ap_types = {ap.anti_pattern_type for ap in result.workload_analysis.anti_patterns_detected}
    assert "large-result-sets" in ap_types

    # Should have recommendations for all 6 tables
    assert len(result.table_recommendations) == 6

    # Scores should be differentiated
    scores = {r.table_id: r.confidence_score for r in result.table_recommendations}
    assert len(set(scores.values())) > 1, f"All scores identical: {scores}"


# ---------------------------------------------------------------------------
# Benchmark tests (marked slow — run with: pytest -m slow)
# ---------------------------------------------------------------------------


def _run_analysis(num_tables: int, num_queries: int, seed: int = 42):
    """Helper: generate synthetic data and run full Redis analysis pipeline."""
    collector_output = generate_collector_output(
        num_tables=num_tables, num_queries=num_queries, seed=seed
    )
    analysis_input = AnalysisInput(
        job_id=f"bench-{num_tables}-{num_queries}",
        collector_output=collector_output,
        target_database=TargetDatabase.elasticache,
    )
    start = time.perf_counter()
    result = analyze_for_redis(analysis_input)
    elapsed = time.perf_counter() - start
    return result, elapsed


@pytest.mark.slow
class TestRedisAnalysisBenchmark:
    """Performance benchmarks at increasing scale."""

    def test_100_queries_20_tables(self):
        """100 queries / 20 tables completes in <1s."""
        result, elapsed = _run_analysis(num_tables=20, num_queries=100)
        assert elapsed < 1.0, f"Took {elapsed:.2f}s (limit 1s)"
        assert len(result.table_recommendations) == 20

    def test_1k_queries_200_tables(self):
        """1k queries / 200 tables completes in <2s."""
        result, elapsed = _run_analysis(num_tables=200, num_queries=1000)
        assert elapsed < 2.0, f"Took {elapsed:.2f}s (limit 2s)"
        assert len(result.table_recommendations) == 200

    def test_5k_queries_1k_tables(self):
        """5k queries / 1k tables completes in <5s."""
        result, elapsed = _run_analysis(num_tables=1000, num_queries=5000)
        assert elapsed < 5.0, f"Took {elapsed:.2f}s (limit 5s)"
        assert len(result.table_recommendations) == 1000

    def test_20k_queries_5k_tables(self):
        """20k queries / 5k tables completes in <15s."""
        result, elapsed = _run_analysis(num_tables=5000, num_queries=20000)
        assert elapsed < 15.0, f"Took {elapsed:.2f}s (limit 15s)"
        assert len(result.table_recommendations) == 5000

    def test_correctness_at_scale(self):
        """All pattern types detected, scores differentiated, fields populated at 5k/1k scale."""
        result, _ = _run_analysis(num_tables=1000, num_queries=5000, seed=99)

        # All 4 pattern types should be detected
        pattern_types = {p.pattern_type for p in result.workload_analysis.patterns_detected}
        assert "caching" in pattern_types
        assert "session-store" in pattern_types
        assert "leaderboard" in pattern_types
        assert "time-series" in pattern_types

        # Anti-patterns should be detected
        assert result.workload_analysis.anti_patterns_detected
        ap_types = {ap.anti_pattern_type for ap in result.workload_analysis.anti_patterns_detected}
        assert "large-result-sets" in ap_types

        # Scores should not all be identical
        scores = {r.confidence_score for r in result.table_recommendations}
        assert len(scores) > 1, "All scores are identical — no differentiation"

        # At least some recommendations should have supporting_patterns
        with_patterns = [r for r in result.table_recommendations if r.supporting_patterns]
        assert len(with_patterns) > 0, "No recommendations have supporting_patterns"

        # At least some recommendations should have concerns
        with_concerns = [r for r in result.table_recommendations if r.concerns]
        assert len(with_concerns) > 0, "No recommendations have concerns"

    def test_reproducibility(self):
        """Same seed produces identical results."""
        result1, _ = _run_analysis(num_tables=200, num_queries=1000, seed=123)
        result2, _ = _run_analysis(num_tables=200, num_queries=1000, seed=123)

        # Compare pattern counts
        assert len(result1.workload_analysis.patterns_detected) == len(
            result2.workload_analysis.patterns_detected
        )

        # Compare recommendation scores
        scores1 = [r.confidence_score for r in result1.table_recommendations]
        scores2 = [r.confidence_score for r in result2.table_recommendations]
        assert scores1 == scores2
