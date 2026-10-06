"""The cache layer's cost figure must be justified from the overlay's own
facts (the hot-read floor, #304/#296, and the combined traffic that cleared
it), not stated on its own with no explanation (review of #375)."""

from __future__ import annotations

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import build_ranking


def _q(qid: str, cps: float) -> dict:
    return {
        "query_id": qid,
        "query_type": "SELECT",
        "tables_accessed": ["t"],
        "calls_per_second": cps,
    }


def _data() -> SynthesisData:
    queries = [_q("c1", 3.0), _q("c2", 3.0), _q("c3", 3.8), _q("other", 20.0)]
    return SynthesisData(
        job_id="j",
        database_name="db",
        collector={"queries": {"query_patterns": queries}},
        engines={
            "aurora_postgresql": EngineArtifacts(
                "aurora_postgresql",
                analysis={"cost_estimate": {"monthly_cost_usd": 121.06}},
                schema_design={},
            ),
            "elasticache": EngineArtifacts(
                "elasticache",
                analysis={"cost_estimate": {"monthly_cost_usd": 165.10}},
                schema_design={},
            ),
        },
        assignment={
            "query_assignments": [
                {
                    "query_id": qid,
                    "assigned_engine": "aurora_postgresql",
                    "cache_engine": "elasticache",
                    "in_scope": True,
                }
                for qid in ("c1", "c2", "c3")
            ]
            + [{"query_id": "other", "assigned_engine": "aurora_postgresql", "in_scope": True}]
        },
    )


class TestCacheCostJustification:
    def test_rationale_states_the_hot_read_floor_the_cache_clears(self) -> None:
        ranking = build_ranking(_data())
        cache = next(r for r in ranking if r.get("role") == "cache_layer")
        assert "estimated $165.10/month" in cache["rationale"]
        assert "1 calls/s hot-read floor" in cache["rationale"]
        # 3 + 3 + 3.8 = 9.8 calls/s combined across the three cached reads.
        assert "9.8 calls/s combined" in cache["rationale"]
