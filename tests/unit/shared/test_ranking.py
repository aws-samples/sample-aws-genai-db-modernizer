"""Reading a synthesis ranking: routed confidence and the main engine (#152)."""

from __future__ import annotations

from src.shared.ranking import engine_confidence, main_engine


class TestEngineConfidence:
    def test_routed_confidence_wins(self):
        assert engine_confidence({"confidence_score": 2, "routed_confidence": 60}) == 60

    def test_legacy_report_falls_back_to_the_analysis_average(self):
        assert engine_confidence({"confidence_score": 48}) == 48

    def test_no_routed_query_falls_back(self):
        assert engine_confidence({"confidence_score": 40, "routed_confidence": None}) == 40


class TestMainEngine:
    def test_largest_owner_share_not_the_first_entry(self):
        """A legacy report ordered by weight put the cache layer or a small engine first."""
        ranking = [
            {"target": "opensearch", "workload_percent": 3.7},
            {"target": "dynamodb", "workload_percent": 91.6},
        ]
        assert main_engine(ranking)["target"] == "dynamodb"

    def test_cache_layer_is_never_the_main_engine(self):
        ranking = [
            {"target": "elasticache", "role": "cache_layer", "workload_percent": 0.0},
            {"target": "aurora_mysql", "workload_percent": 4.7},
        ]
        assert main_engine(ranking)["target"] == "aurora_mysql"

    def test_without_shares_the_first_owner_stands(self):
        assert main_engine([{"target": "a"}, {"target": "b"}])["target"] == "a"

    def test_empty(self):
        assert main_engine([]) is None
        assert main_engine(None) is None
