"""Synthesis ranking follows the routed workload (#152).

``analysis_confidence`` (the all-analyzed-tables average) stays for audit; every
entry also carries ``routed_confidence``, the ranking is ordered by workload share
with the cache layer after the owners, and the rationale names the routed workload.
"""

from __future__ import annotations

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import (
    _engine_rationale,
    build_architecture_recommendation,
    build_ranking,
)


def _analysis(patterns: int, **scores: int) -> dict:
    return {
        "table_recommendations": [
            {"table_id": t, "confidence_score": s} for t, s in scores.items()
        ],
        "workload_analysis": {
            "patterns_detected": [{"p": i} for i in range(patterns)],
            "anti_patterns_detected": [],
        },
        "cost_estimate": {"monthly_cost_usd": 10},
    }


def _q(qid: str, table: str) -> dict:
    return {
        "query_id": qid,
        "query_type": "SELECT",
        "tables_accessed": [table],
        "calls_per_second": 2.0,
    }


QUERIES = [_q("s1", "posts"), *(_q(f"k{i}", "users") for i in range(6))]


def _data(assignment: dict | None) -> SynthesisData:
    # OpenSearch suits 1 of 4 tables (all-tables average 25%) but fits the search
    # query routed to it; ElastiCache's analysis weight is the highest of all.
    analyses = {
        "dynamodb": _analysis(1, posts=30, users=90, a=40, b=40),
        "opensearch": _analysis(0, posts=90, users=5, a=3, b=2),
        "elasticache": _analysis(5, posts=60, users=95, a=95, b=95),
    }
    return SynthesisData(
        job_id="j",
        database_name="db",
        triage={
            "selected_agents": [{"agent_type": e} for e in analyses],
            "signals": [
                {"signal": "text_search", "query_ids": ["s1"]},
                {"signal": "key_value_lookups", "query_ids": [f"k{i}" for i in range(6)]},
            ],
        },
        collector={
            "database_schema": {"tables": [{"table_id": t} for t in ("posts", "users", "a", "b")]},
            "queries": {"query_patterns": QUERIES},
        },
        engines={e: EngineArtifacts(engine=e, analysis=a) for e, a in analyses.items()},
        assignment=assignment,
    )


ASSIGNMENT = {
    "version": 2,
    "query_assignments": [
        {"query_id": "s1", "assigned_engine": "opensearch", "assignment_reason": "signal override"},
        *(
            {
                "query_id": f"k{i}",
                "assigned_engine": "dynamodb",
                "assignment_reason": "highest confidence",
                **(
                    {"cache_engine": "elasticache", "cache_pattern": "point_lookup"}
                    if i < 2
                    else {}
                ),
            }
            for i in range(6)
        ),
    ],
}


def _by_target(ranking: list[dict]) -> dict[str, dict]:
    return {r["target"]: r for r in ranking}


class TestRankingOrder:
    def test_ordered_by_workload_share_with_the_cache_last(self):
        ranking = build_ranking(_data(ASSIGNMENT))
        assert [r["target"] for r in ranking] == ["dynamodb", "opensearch", "elasticache"]
        # the old weight would have put opensearch's 25% average far below; it stays an audit field
        assert all("weight" in r for r in ranking)

    def test_without_assignment_the_weight_order_stands(self):
        ranking = build_ranking(_data(None))
        weights = [r["weight"] for r in ranking if r.get("role") != "cache_layer"]
        assert weights == sorted(weights, reverse=True)
        assert all(r["routed_confidence"] is None for r in ranking)


class TestConfidences:
    def test_analysis_and_routed_confidence_are_both_kept(self):
        r = _by_target(build_ranking(_data(ASSIGNMENT)))
        assert r["opensearch"]["analysis_confidence"] == 25
        assert r["opensearch"]["confidence_score"] == 25  # backward compatible
        assert r["opensearch"]["routed_confidence"] == 100  # 90 + text-search bonus, capped
        assert r["opensearch"]["routed_confidence_basis"] == "owned_queries"
        assert r["opensearch"]["routed_queries"] == 1
        assert r["opensearch"]["routed_tables"] == 1

    def test_cache_layer_gets_a_cache_specific_confidence(self):
        cache = _by_target(build_ranking(_data(ASSIGNMENT)))["elasticache"]
        assert cache["role"] == "cache_layer"
        assert cache["routed_confidence_basis"] == "cached_reads"
        assert cache["routed_queries"] == 2
        # users=95, no penalty for the owner's key-value signal
        assert cache["routed_confidence"] == 95


class TestRationale:
    def test_owner_rationale_names_the_routed_workload(self):
        ranking = build_ranking(_data(ASSIGNMENT))
        dbs = {
            d["service"]: d
            for d in build_architecture_recommendation(_data(ASSIGNMENT), ranking, [])["databases"]
        }
        assert dbs["opensearch"]["rationale"].startswith(
            "100% mean fit across 1 query (1 table), led by full-text search (1 of 1)."
        )
        assert dbs["dynamodb"]["rationale"].startswith(
            "100% mean fit across 6 queries (1 table), led by key-value lookups (6 of 6)."
        )
        assert "average confidence across" not in dbs["opensearch"]["rationale"]
        assert dbs["opensearch"]["routed_confidence"] == 100

    def test_cache_rationale_states_its_cache_fit(self):
        ranking = build_ranking(_data(ASSIGNMENT))
        dbs = {
            d["service"]: d
            for d in build_architecture_recommendation(_data(ASSIGNMENT), ranking, [])["databases"]
        }
        text = dbs["elasticache"]["rationale"]
        assert text.startswith("Cache layer for 2 hot reads")
        assert "mostly point lookups" in text
        assert "95% mean cache fit across 2 cached reads (1 table)" in text

    def test_without_assignment_the_rationale_keeps_the_analysis_average(self):
        data = _data(None)
        dynamodb = _by_target(build_ranking(data))["dynamodb"]
        assert _engine_rationale(data, dynamodb).startswith(
            "50% average confidence across 4 tables"
        )


class TestNoTableEvidence:
    def test_rationale_says_when_no_table_backs_the_fit(self):
        """discourse OpenSearch: its queries read tables_accessed ["unknown"]."""
        data = _data(ASSIGNMENT)
        data.collector["queries"]["query_patterns"] = [
            {**q, "tables_accessed": ["unknown"]} if q["query_id"] == "s1" else q for q in QUERIES
        ]
        opensearch = _by_target(build_ranking(data))["opensearch"]
        assert opensearch["routed_confidence_evidence"] == "signal_only"
        assert opensearch["routed_tables"] == 0
        assert _engine_rationale(data, opensearch).startswith(
            "60% mean fit across 1 query (signal only — no table-level evidence), led by full-text search"
        )

    def test_partial_evidence_is_counted(self):
        data = _data(ASSIGNMENT)
        data.collector["queries"]["query_patterns"] = [
            {**q, "tables_accessed": ["unknown"]} if q["query_id"] == "k0" else q for q in QUERIES
        ]
        dynamodb = _by_target(build_ranking(data))["dynamodb"]
        assert dynamodb["routed_confidence_evidence"] == "partial"
        # 1 of 6 queries (17%) is under the 25% label threshold
        assert "(1 table; 1 query without table-level evidence)" in _engine_rationale(
            data, dynamodb
        )

    def test_partial_evidence_from_a_quarter_is_partly_signal_based(self):
        data = _data(ASSIGNMENT)
        data.collector["queries"]["query_patterns"] = [
            {**q, "tables_accessed": ["unknown"]} if q["query_id"] in ("k0", "k1") else q
            for q in QUERIES
        ]
        dynamodb = _by_target(build_ranking(data))["dynamodb"]
        assert (
            "(1 table; 2 queries without table-level evidence, partly signal-based)"
            in _engine_rationale(data, dynamodb)
        )
