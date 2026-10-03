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

from src.agents.referee.synthesis_grounding import eliminated_engines, ground_risks
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
                    "query_ids": ["q-orders"],
                    "recommendation": "Index order totals in OpenSearch for dashboards.",
                },
                {
                    "pattern_type": "full_text",
                    "query_ids": ["q-search"],
                    "recommendation": "Use OpenSearch for the product keyword search.",
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
    """Any mention at all (only used where no mention is allowed: strategies, patterns)."""
    return "opensearch" in json.dumps(obj).lower()


class TestEliminatedEngineNeverATarget:
    @pytest.fixture
    def result(self, store):
        _seed(store, eliminated=True)
        return run_synthesis_deterministic(JOB, DB, store, assignment_version=2)

    def _risk(self, result, needle: str) -> dict:
        return next(r for r in result["risk_assessment"]["risks"] if needle in r["description"])

    def test_no_risk_is_dropped_and_no_severity_changes(self, result, store) -> None:
        risks = result["risk_assessment"]["risks"]
        assert len(risks) == 4  # 3 unsupported patterns + 1 anti-pattern
        assert [r["severity"] for r in risks].count("HIGH") == 1

    def test_mitigation_strategies_name_the_absorbing_engine(self, result) -> None:
        strategies = result["risk_assessment"]["mitigation_strategies"]
        assert not _mentions_opensearch(strategies)
        complementary = [s for s in strategies if "unsupported query pattern" in s]
        assert complementary and "Aurora MySQL" in complementary[0]

    def test_description_kept_whole_mitigation_filtered(self, result) -> None:
        risk = self._risk(result, "text search")
        assert risk["description"] == (
            "[dynamodb] text search: Stream orders to OpenSearch via OpenSearch Ingestion for "
            "keyword search. Keep exact order-id lookups on the base table. (OpenSearch "
            "is not part of the target architecture; its queries run on Aurora MySQL.)"
        )
        assert risk["mitigation"] == "Keep exact order-id lookups on the base table."
        assert "grounding_note" in risk

    def test_description_with_only_a_recommendation_is_kept_with_a_note(self, result) -> None:
        risk = self._risk(result, "aggregation")
        assert risk["description"].startswith(
            "[dynamodb] aggregation: Index order totals in OpenSearch for dashboards."
        )
        assert risk["description"].endswith(
            "(OpenSearch is not part of the target architecture; its queries run on "
            "Aurora MySQL.)"
        )

    def test_absorber_not_claimed_for_queries_assigned_elsewhere(self, result) -> None:
        # q-orders stays on DynamoDB, so "handle this on Aurora MySQL" would be false.
        risk = self._risk(result, "aggregation")
        assert risk["mitigation"] == (
            "Re-plan this on DynamoDB or Aurora MySQL; the original recommendation named an "
            "engine that was removed."
        )

    def test_absorber_named_when_the_queries_moved_there(self, result) -> None:
        risk = self._risk(result, "full text")
        assert risk["mitigation"] == (
            "Handle this on Aurora MySQL, where the assignment now routes these queries."
        )

    def test_no_op_instruction_for_a_risk_already_on_the_absorber(self, result) -> None:
        risk = self._risk(result, "LIKE search")
        assert "Handle this on" not in risk["mitigation"]
        assert risk["mitigation"].startswith("Re-plan this on Aurora MySQL;")

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
        r for r in result["risk_assessment"]["risks"] if "text search" in r["description"]
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


class TestGroundRisks:
    """Sentence-level rules of ``ground_risks`` (#202 review)."""

    ELIM = {"opensearch": "aurora_mysql"}

    def _risk(self, description: str, mitigation: str | None = None, **extra) -> dict:
        return {
            "risk_id": "RISK-001",
            "risk_type": "MIGRATION_COMPLEXITY",
            "severity": "HIGH",
            "description": description,
            "mitigation": mitigation,
            **extra,
        }

    def test_trade_off_sentence_survives(self) -> None:
        risk = self._risk(
            "[aurora_mysql] Text search was consolidated from OpenSearch into Aurora MySQL, "
            "so relevance ranking is simpler.",
            "Accept simpler ranking instead of OpenSearch scoring.",
        )
        assert ground_risks([risk], self.ELIM) == [risk]

    def test_history_from_engine_survives(self) -> None:
        risk = self._risk("[dynamodb] Queries moved from OpenSearch need a GSI.")
        assert ground_risks([risk], self.ELIM) == [risk]

    def test_high_count_unchanged_when_nothing_absorbed(self) -> None:
        risks = [
            self._risk("[dynamodb] Stream items to OpenSearch for search.", "Use OpenSearch."),
            self._risk("[dynamodb] Route analytics to OpenSearch.", None),
            self._risk("[dynamodb] Hot partition on status."),
        ]
        out = ground_risks(risks, {"opensearch": None})
        assert len(out) == 3
        assert [r["severity"] for r in out] == ["HIGH", "HIGH", "HIGH"]
        assert out[0]["description"].endswith(
            "(OpenSearch has no in-scope queries in the target architecture.)"
        )
        assert out[0]["mitigation"] == (
            "Re-plan this on DynamoDB; the original recommendation named an engine that was "
            "removed."
        )

    def test_abbreviations_do_not_split_sentences(self) -> None:
        risk = self._risk(
            "[dynamodb] Sparse GSI needed.",
            "Add a sparse GSI, e.g. by status vs. date, for 2.5x fewer reads. "
            "Stream the rest to OpenSearch.",
        )
        out = ground_risks([risk], self.ELIM)[0]
        assert out["mitigation"] == (
            "Add a sparse GSI, e.g. by status vs. date, for 2.5x fewer reads."
        )

    def test_engine_as_proposed_doer_is_a_recommendation(self) -> None:
        risk = self._risk(
            "[dynamodb] Facets.", "Keep keys short. OpenSearch can handle the facets."
        )
        assert ground_risks([risk], self.ELIM)[0]["mitigation"] == "Keep keys short."

    def test_description_never_loses_a_sentence(self) -> None:
        desc = "[dynamodb] Keep keys short. Stream the rest to OpenSearch."
        out = ground_risks([self._risk(desc)], self.ELIM)[0]
        assert out["description"].startswith(desc + " (OpenSearch is not part")

    @pytest.mark.parametrize(
        "sentence",
        [
            "Replace LIKE scans with OpenSearch full-text queries.",
            "Use OpenSearch to remove full-table scans on wp_posts.",
            "Stream posts to OpenSearch instead of running LIKE on Aurora.",
            "Offload search to OpenSearch, which eliminates the table scans.",
        ],
    )
    def test_recommendation_with_a_trade_off_word_elsewhere_is_dropped(self, sentence) -> None:
        risk = self._risk("[aurora_mysql] Wildcard search.", f"Add an index. {sentence}")
        assert ground_risks([risk], self.ELIM)[0]["mitigation"] == "Add an index."

    @pytest.mark.parametrize(
        "sentence",
        [
            "Consolidating OpenSearch Service into Aurora MySQL loses BM25 relevance ranking.",
            "Without OpenSearch, ranking uses MySQL FULLTEXT natural-language mode.",
            "Accept simpler ranking instead of OpenSearch scoring.",
        ],
    )
    def test_trade_off_mention_is_kept(self, sentence) -> None:
        risk = self._risk("[aurora_mysql] Ranking.", sentence)
        assert ground_risks([risk], self.ELIM) == [risk]

    def test_risk_without_eliminated_engine_is_untouched(self) -> None:
        risk = self._risk("[dynamodb] Use DynamoDB Streams to keep counters.")
        assert ground_risks([risk], self.ELIM) == [risk]
        assert "grounding_note" not in ground_risks([risk], self.ELIM)[0]
