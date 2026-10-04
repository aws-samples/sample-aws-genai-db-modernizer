"""Reality Check fixes shipped with the cache overlay (#296).

- A mandatory (signal-override) engine protects only its mandatory queries.
- The capability map: aggregations need ``aggregation``; ElastiCache serves top-N.
- ElastiCache is never a consolidation target; non-system-of-record engines never
  absorb writes.
- The cache overlay survives its owner's consolidation and is re-evaluated.
"""

from __future__ import annotations

from src.agents.referee.assignment_overrides import refresh_consolidated_assignment
from src.agents.referee.reality_check import (
    BASIC_CRUD_SCORE,
    ENGINE_CAPABILITIES,
    SIGNAL_MATCH_BONUS,
    SIGNAL_TO_CAPABILITY,
    _engine_fit_score,
    _find_best_absorber_for_query,
    run_reality_check,
)


def _collector(qids: list[str], **fields) -> dict:
    return {
        "queries": {
            "query_patterns": [
                {
                    "query_id": q,
                    "query_text": "SELECT * FROM users WHERE id = ?",
                    "tables_accessed": ["db.users"],
                    "query_type": "SELECT",
                    "calls_per_second": 0.01,
                    "rows_returned_avg": 1,
                    **fields,
                }
                for q in qids
            ]
        }
    }


def _triage(signals: list[dict] | None = None) -> dict:
    return {
        "selected_agents": [{"agent_type": "dynamodb"}, {"agent_type": "opensearch"}],
        "signals": signals or [],
    }


class TestMandatoryProtectsOnlyItsQueries:
    def _run(self):
        others = [f"o{i}" for i in range(12)]
        assignment = {
            "version": 1,
            "query_assignments": [
                {"query_id": "d1", "assigned_engine": "dynamodb", "assignment_reason": "t"},
                {"query_id": "d2", "assigned_engine": "dynamodb", "assignment_reason": "t"},
                {
                    "query_id": "search",
                    "assigned_engine": "opensearch",
                    "assignment_reason": "signal override: text_search → opensearch",
                    "signal_override": "text_search",
                },
                *(
                    {"query_id": q, "assigned_engine": "opensearch", "assignment_reason": "t"}
                    for q in others
                ),
            ],
        }
        triage = _triage(
            [{"signal": "text_search", "targets": ["opensearch"], "query_ids": ["search"]}]
        )
        result = run_reality_check(
            assignment, triage, {}, _collector(["d1", "d2", "search", *others])
        )
        return result, others

    def test_non_mandatory_queries_are_consolidated(self):
        result, others = self._run()
        engine = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine["search"] == "opensearch"
        assert {engine[q] for q in others} == {"dynamodb"}

    def test_engine_stays_with_a_partial_record(self):
        result, others = self._run()
        record = next(c for c in result["consolidations"] if c["from_engine"] == "opensearch")
        assert record["action"] == "partial"
        assert record["query_count"] == len(others)
        assert record["queries_retained"] == ["search"]
        assert "Mandatory signal queries" in record["retention_reason"]
        assert record["saved_cost_estimate"] == 0
        assert result["unique_value_assessment"]["opensearch"]["is_mandatory"] is True

    def test_engine_with_only_mandatory_queries_is_untouched(self):
        assignment = {
            "version": 1,
            "query_assignments": [
                {"query_id": "d1", "assigned_engine": "dynamodb", "assignment_reason": "t"},
                {
                    "query_id": "search",
                    "assigned_engine": "opensearch",
                    "assignment_reason": "signal override: text_search",
                    "signal_override": "text_search",
                },
            ],
        }
        result = run_reality_check(assignment, _triage(), {}, _collector(["d1", "search"]))
        engine = {qa["query_id"]: qa["assigned_engine"] for qa in result["revised_assignments"]}
        assert engine == {"d1": "dynamodb", "search": "opensearch"}


class TestCapabilityMap:
    def test_aggregations_need_the_aggregation_capability(self):
        assert SIGNAL_TO_CAPABILITY["aggregations"] == "aggregation"
        signals = {"q": ["aggregations"]}
        query_map = {"q": {"tables_accessed": []}}
        qa = {"query_id": "q"}
        assert _engine_fit_score("dynamodb", qa, signals, query_map, {}) == (
            BASIC_CRUD_SCORE - SIGNAL_MATCH_BONUS
        )
        assert _engine_fit_score("aurora_mysql", qa, signals, query_map, {}) == (
            BASIC_CRUD_SCORE + SIGNAL_MATCH_BONUS
        )
        assert _engine_fit_score("elasticache", qa, signals, query_map, {}) == 0

    def test_elasticache_serves_top_n(self):
        assert SIGNAL_TO_CAPABILITY["leaderboard_pattern"] in ENGINE_CAPABILITIES["elasticache"]


class TestNoCacheAbsorber:
    def _absorber(self, qa, query_map):
        analysis = {
            "elasticache": {"table_recommendations": [{"table_id": "t", "confidence_score": 99}]},
            "dynamodb": {"table_recommendations": [{"table_id": "t", "confidence_score": 40}]},
        }
        return _find_best_absorber_for_query(
            qa,
            {"elasticache", "dynamodb"},
            "documentdb",
            {},
            query_map,
            analysis,
            {},
            set(),
            "dynamodb",
        )

    def test_elasticache_never_absorbs_a_read(self):
        query_map = {"q": {"tables_accessed": ["t"], "query_type": "SELECT"}}
        assert self._absorber({"query_id": "q"}, query_map)["target_engine"] == "dynamodb"

    def test_elasticache_never_absorbs_a_write(self):
        query_map = {"w": {"tables_accessed": ["t"], "query_type": "UPDATE"}}
        assert self._absorber({"query_id": "w"}, query_map)["target_engine"] == "dynamodb"


class TestOverlaySurvivesConsolidation:
    def test_refresh_reevaluates_the_overlay_against_the_new_owner(self):
        collector = {
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "hot",
                        "query_text": "SELECT * FROM users WHERE id = ?",
                        "query_type": "SELECT",
                        "tables_accessed": ["users"],
                        "calls_per_second": 5.0,
                        "rows_returned_avg": 1,
                    },
                    {
                        "query_id": "cold",
                        "query_text": "SELECT * FROM users WHERE id = ?",
                        "query_type": "SELECT",
                        "tables_accessed": ["users"],
                        "calls_per_second": 0.1,
                        "rows_returned_avg": 1,
                    },
                ]
            },
            "database_schema": {"tables": [{"table_id": "users"}]},
        }
        raw = {
            "job_id": "t",
            "version": 2,
            "query_assignments": [
                # consolidated from documentdb to dynamodb; the overlay must survive
                {
                    "query_id": "hot",
                    "assigned_engine": "dynamodb",
                    "confidence": 50,
                    "source_tables": ["users"],
                    "assignment_reason": "reality check: consolidated from documentdb",
                    "cache_engine": "elasticache",
                },
                {
                    "query_id": "cold",
                    "assigned_engine": "dynamodb",
                    "confidence": 50,
                    "source_tables": ["users"],
                    "assignment_reason": "t",
                    "cache_engine": "elasticache",  # stale: no longer qualifies
                },
            ],
        }
        analysis = {"dynamodb": {}, "elasticache": {}}
        out = refresh_consolidated_assignment(raw, collector, analysis, dead_engines={"documentdb"})
        by_id = {qa["query_id"]: qa for qa in out["query_assignments"]}
        assert by_id["hot"]["cache_engine"] == "elasticache"
        assert by_id["cold"]["cache_engine"] is None
        assert out["cache_overlay"]["owners"] == {"dynamodb": 1}
