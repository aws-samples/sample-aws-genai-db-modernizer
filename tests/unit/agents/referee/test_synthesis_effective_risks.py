"""Risks and mitigations follow the effective (post-reality-check) architecture (#202).

The reality check can eliminate an engine and fold its workload into another one
(here OpenSearch is absorbed into Aurora MySQL). Every target recommendation in the
synthesis report must describe the architecture that will actually be built, so no
risk, mitigation, mitigation strategy or recommended architectural pattern may present
the eliminated engine as a target. Consolidation history ("moved from OpenSearch to
Aurora MySQL") is fine: it explains the elimination rather than recommending it.
"""

from __future__ import annotations

import json

import pytest

from src.agents.referee.synthesis_grounding import eliminated_engines
from src.agents.referee.synthesis_handler import run_synthesis_deterministic
from src.storage.local_store import LocalArtifactStore

DB = "shop"
JOB = "job-rc-eliminated"


def _seed(store: LocalArtifactStore, *, eliminated: bool) -> None:
    selected = ["dynamodb", "aurora_mysql", "opensearch"]
    store.write_json(
        f"{DB}/{JOB}/referee-triage/triage.json",
        {"selected_agents": [{"agent_type": e} for e in selected]},
    )
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "database_schema": {"tables": [{"table_id": "shop.orders"}]},
            "queries": {"query_patterns": []},
        },
    )
    store.write_json(
        f"{DB}/{JOB}/analysis-dynamodb/analysis.json",
        {"workload_analysis": {"anti_patterns_detected": []}},
    )
    store.write_json(
        f"{DB}/{JOB}/analysis-aurora_mysql/analysis.json",
        {
            "workload_analysis": {
                "anti_patterns_detected": [
                    {
                        "anti_pattern_type": "like-wildcard-search",
                        "description": "Leading-wildcard LIKE search on product names",
                        "query_ids": ["q-search"],
                        "table_ids": ["shop.products"],
                        "severity_weight": 0.9,
                        "recommendation": "Move product search to OpenSearch Service.",
                    }
                ]
            }
        },
    )
    store.write_json(
        f"{DB}/{JOB}/analysis-opensearch/analysis.json",
        {"workload_analysis": {"anti_patterns_detected": []}},
    )
    store.write_json(
        f"{DB}/{JOB}/schema-dynamodb/v2/schema_output.json",
        {
            "table_definitions": [
                {"table_name": "Orders", "source_tables": ["shop.orders"], "gsis": []}
            ],
            "access_patterns": [],
            "unsupported_patterns": [
                {
                    "pattern_type": "text_search",
                    "recommendation": (
                        "Stream orders to OpenSearch via OpenSearch Ingestion for keyword "
                        "search. Keep exact order-id lookups on the base table."
                    ),
                },
                {
                    "pattern_type": "aggregation",
                    "recommendation": "Index order totals in OpenSearch for dashboards.",
                },
            ],
            "migration_notes": [],
        },
    )
    store.write_json(
        f"{DB}/{JOB}/schema-aurora_mysql/v2/schema_output.json",
        {
            "source_database": DB,
            "table_definitions": [{"table_name": "products", "columns": []}],
            "access_patterns": [],
        },
    )
    queries = [
        {"query_id": "q-orders", "assigned_engine": "dynamodb", "in_scope": True},
        {"query_id": "q-search", "assigned_engine": "aurora_mysql", "in_scope": True},
    ]
    if not eliminated:
        queries.append({"query_id": "q-fts", "assigned_engine": "opensearch", "in_scope": True})
    store.write_json(
        f"{DB}/{JOB}/assignment/v2/assignment.json",
        {"version": 2, "status": "reality_checked", "query_assignments": queries},
    )
    if eliminated:
        store.write_json(
            f"{DB}/{JOB}/reality-check/output.json",
            {
                "consolidations": [
                    {
                        "from_engine": "opensearch",
                        "to_engine": "aurora_mysql",
                        "query_count": 3,
                        "reason": "Aurora absorption: aurora_mysql serves the text search",
                        "action": "full",
                    }
                ],
                "architectural_patterns": [
                    {
                        "name": "Command Query Responsibility Segregation (CQRS)",
                        "description": "Separate writes from reads.",
                        "applies_to": {
                            "write_engine": "dynamodb",
                            "read_engines": ["opensearch"],
                        },
                    },
                    {
                        "name": "Materialized View Pattern",
                        "description": "Keep a read-only projection.",
                        "applies_to": {"source_engine": "dynamodb", "view_engine": "opensearch"},
                    },
                    {
                        "name": "Polyglot Persistence",
                        "description": "Different databases per bounded context.",
                        "applies_to": {"engines": ["dynamodb", "aurora_mysql", "opensearch"]},
                    },
                ],
                "recommendations": [
                    "Consolidated 3 queries from opensearch → aurora_mysql: Aurora absorption.",
                    "Recommended pattern: Command Query Responsibility Segregation (CQRS). "
                    "Use dynamodb for all write operations and opensearch for specialized reads. "
                    "Separate writes from reads.",
                    "Recommended pattern: Materialized View Pattern. dynamodb is the source of "
                    "truth; opensearch maintains a search-optimized projection. Keep a "
                    "read-only projection.",
                    "Recommended pattern: Polyglot Persistence. Different databases per "
                    "bounded context.",
                ],
                "before_distribution": {"dynamodb": 1, "aurora_mysql": 1, "opensearch": 3},
                "after_distribution": {"dynamodb": 1, "aurora_mysql": 4},
            },
        )


@pytest.fixture
def store(tmp_path):
    return LocalArtifactStore(base_dir=str(tmp_path))


def _mentions_opensearch(obj: object) -> bool:
    return "opensearch" in json.dumps(obj).lower()


class TestEliminatedEngineNeverATarget:
    @pytest.fixture
    def result(self, store):
        _seed(store, eliminated=True)
        return run_synthesis_deterministic(JOB, DB, store, assignment_version=2)

    def test_risk_register_never_names_the_eliminated_engine(self, result) -> None:
        assert result["risk_assessment"]["risks"], "risks must still be reported"
        assert not _mentions_opensearch(result["risk_assessment"])

    def test_mitigation_strategies_name_the_absorbing_engine(self, result) -> None:
        strategies = result["risk_assessment"]["mitigation_strategies"]
        assert not _mentions_opensearch(strategies)
        complementary = [s for s in strategies if "unsupported patterns" in s]
        assert complementary and "Aurora MySQL" in complementary[0]

    def test_risk_rewritten_to_the_absorbing_engine(self, result) -> None:
        risks = result["risk_assessment"]["risks"]
        # The DynamoDB text-search sentence is dropped; the surviving sentence stays.
        text_search = next(r for r in risks if "text_search" in r["description"])
        assert "exact order-id lookups" in text_search["mitigation"]
        # A mitigation that only pointed at the eliminated engine is rewritten.
        like = next(r for r in risks if "LIKE search" in r["description"])
        assert "Aurora MySQL" in like["mitigation"]

    def test_patterns_and_trade_offs_reference_only_effective_engines(self, result) -> None:
        rc = result["reality_check_summary"]
        assert not _mentions_opensearch([p["applies_to"] for p in rc["architectural_patterns"]])
        names = {p["name"] for p in rc["architectural_patterns"]}
        assert "Materialized View Pattern" not in names  # its view engine was eliminated
        for text in rc["recommendations"] + [t["description"] for t in result["trade_offs"]]:
            if text.startswith("Recommended pattern"):
                assert "opensearch" not in text.lower(), text

    def test_consolidation_history_is_kept(self, result) -> None:
        rc = result["reality_check_summary"]
        assert rc["consolidations"][0]["from_engine"] == "opensearch"
        assert any(
            r.startswith("Consolidated 3 queries from opensearch") for r in rc["recommendations"]
        )


def test_surviving_opensearch_is_still_recommended(store) -> None:
    """No elimination: OpenSearch is part of the target, so it may be recommended."""
    _seed(store, eliminated=False)
    result = run_synthesis_deterministic(JOB, DB, store, assignment_version=2)
    strategies = result["risk_assessment"]["mitigation_strategies"]
    assert any("OpenSearch Service" in s for s in strategies)
    text_search = next(
        r for r in result["risk_assessment"]["risks"] if "text_search" in r["description"]
    )
    assert "OpenSearch Ingestion" in text_search["mitigation"]


class TestEliminatedEngines:
    """Only the reality check eliminates an engine (an unassigned triage pick does not)."""

    def test_no_reality_check_means_nothing_eliminated(self) -> None:
        assert eliminated_engines({"dynamodb"}, None) == {}

    def test_consolidated_engine_maps_to_its_absorber(self) -> None:
        rc = {
            "before_distribution": {"dynamodb": 5, "opensearch": 3, "documentdb": 2},
            "consolidations": [
                {"from_engine": "opensearch", "to_engine": "aurora_mysql", "query_count": 3},
                {"from_engine": "documentdb", "to_engine": "dynamodb", "query_count": 2},
            ],
        }
        assert eliminated_engines({"dynamodb", "aurora_mysql"}, rc) == {
            "documentdb": "dynamodb",
            "opensearch": "aurora_mysql",
        }

    def test_engine_without_pre_reality_check_queries_is_not_eliminated(self) -> None:
        rc = {"before_distribution": {"dynamodb": 5}, "consolidations": []}
        assert eliminated_engines({"dynamodb"}, rc) == {}

    def test_partially_consolidated_engine_that_survives_is_not_eliminated(self) -> None:
        rc = {
            "before_distribution": {"dynamodb": 5, "aurora_mysql": 4},
            "consolidations": [
                {"from_engine": "aurora_mysql", "to_engine": "dynamodb", "action": "partial"}
            ],
        }
        assert eliminated_engines({"dynamodb", "aurora_mysql"}, rc) == {}
