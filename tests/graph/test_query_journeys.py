"""Tests for the graph-backed query-journey read-model (graph.queries).

These prove the graph serves the SAME per-query journey shape the WebApp report
and API previously read from the ~1,654 per-query JSON artifacts — sourced from
the Query node (+ READS_FROM / MIGRATES_TO / PART_OF), with the nested
performance/characteristics blocks round-tripped through their JSON columns.
"""

from __future__ import annotations

from src.graph.populators import (
    populate_from_assignment,
    populate_from_collector,
    populate_from_schema_design,
)
from src.graph.queries import query_journey, query_journeys


def _rich_collector() -> dict:
    """A collector pattern carrying the full source payload (perf/characteristics)."""
    return {
        "queries": {
            "query_patterns": [
                {
                    "query_id": "q1",
                    "query_text": "SELECT * FROM orders WHERE id = ?",
                    "query_type": "SELECT",
                    "tables_accessed": ["orders"],
                    "calls_per_second": 12.5,
                    "frequency_per_hour": 45000.0,
                    "performance": {
                        "execution_time_ms_avg": 3.2,
                        "rows_returned_avg": 1.0,
                        "full_table_scans": 0,
                    },
                    "characteristics": {
                        "has_joins": False,
                        "join_count": 0,
                        "filter_columns": ["id"],
                    },
                }
            ]
        }
    }


class TestQueryJourneysFromGraph:
    def test_full_source_round_trips(self, graph_store):
        populate_from_collector(_rich_collector(), graph_store)

        journeys = query_journeys(graph_store)
        assert len(journeys) == 1
        j = journeys[0]

        assert j["query_id"] == "q1"
        src = j["source"]
        assert src["query_text"] == "SELECT * FROM orders WHERE id = ?"
        assert src["query_type"] == "SELECT"
        assert src["tables_accessed"] == ["orders"]
        assert src["calls_per_second"] == 12.5
        assert src["frequency_per_hour"] == 45000.0
        # Nested blocks decoded from their JSON columns, intact.
        assert src["performance"]["execution_time_ms_avg"] == 3.2
        assert src["performance"]["full_table_scans"] == 0
        assert src["characteristics"]["has_joins"] is False
        assert src["characteristics"]["filter_columns"] == ["id"]
        # No assignment / design yet.
        assert j["assignment"] is None
        assert j["design"] is None

    def test_assignment_section_from_migrates_to(self, graph_store):
        populate_from_collector(_rich_collector(), graph_store)
        populate_from_assignment(
            {
                "query_assignments": [
                    {
                        "query_id": "q1",
                        "assigned_engine": "dynamodb",
                        "confidence": 0.92,
                        "source_tables": ["orders"],
                        "assignment_reason": "kv lookup",
                        "in_scope": True,
                    }
                ],
                "table_assignments": [],
                "co_dependency_groups": [],
            },
            graph_store,
        )

        j = query_journeys(graph_store)[0]
        assert j["assignment"] is not None
        assert j["assignment"]["assigned_engine"] == "dynamodb"
        assert j["assignment"]["confidence"] == 0.92
        assert j["assignment"]["in_scope"] is True

    def test_design_section_from_access_pattern(self, graph_store, sample_schema_design_output):
        populate_from_collector(_rich_collector(), graph_store)
        populate_from_schema_design(sample_schema_design_output, "dynamodb", 1, graph_store)

        j = query_journeys(graph_store)[0]
        # q1 is in the sample access pattern's query_ids -> design present.
        assert j["design"] is not None
        assert j["design"]["engine"] == "dynamodb"
        assert j["design"]["status"] == "completed"

    def test_empty_nested_blocks_degrade_to_empty_dicts(self, graph_store, sample_collector_output):
        # The minimal shared fixture has no performance/characteristics — they must
        # come back as {} (which the report renders as "No data available"), never
        # crash the projection.
        populate_from_collector(sample_collector_output, graph_store)
        journeys = {j["query_id"]: j for j in query_journeys(graph_store)}
        assert set(journeys) == {"q1", "q2", "q3"}
        assert journeys["q1"]["source"]["performance"] == {}
        assert journeys["q1"]["source"]["characteristics"] == {}

    def test_single_query_journey_lookup(self, graph_store):
        populate_from_collector(_rich_collector(), graph_store)
        j = query_journey(graph_store, "q1")
        assert j is not None
        assert j["query_id"] == "q1"

    def test_single_query_journey_unknown_returns_none(self, graph_store):
        populate_from_collector(_rich_collector(), graph_store)
        assert query_journey(graph_store, "does-not-exist") is None
