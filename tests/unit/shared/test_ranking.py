"""Reading a synthesis ranking: routed confidence and the main engine (#152)."""

from __future__ import annotations

from src.shared.ranking import confidence_text, engine_confidence, is_signal_only, main_engine


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


class TestEvidenceNote:
    """#152 decision: a signal-only score is never presented as solid."""

    def test_signal_only_is_always_labelled(self):
        e = {"routed_confidence": 60, "routed_confidence_evidence": "signal_only"}
        assert confidence_text(e) == "60% (signal only — no table-level evidence)"
        assert confidence_text(e, short=True) == "60% (signal only)"
        assert is_signal_only(e)

    def test_partial_is_labelled_from_a_quarter_of_the_queries(self):
        e = {
            "routed_confidence": 90,
            "routed_confidence_evidence": "partial",
            "routed_queries": 8,
            "routed_queries_without_table_evidence": 2,
        }
        assert confidence_text(e) == "90% (partly signal-based)"
        e["routed_queries_without_table_evidence"] = 1
        assert confidence_text(e) == "90%"

    def test_table_backed_and_legacy_entries_are_plain(self):
        assert (
            confidence_text({"routed_confidence": 93, "routed_confidence_evidence": "table"})
            == "93%"
        )
        assert confidence_text({"confidence_score": 2}) == "2%"
        assert not is_signal_only(
            {"confidence_score": 2, "routed_confidence_evidence": "signal_only"}
        )
