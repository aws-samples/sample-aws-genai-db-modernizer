# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for the graph-backed query-journey read-model (graph.queries).

These prove the graph serves the SAME per-query journey shape the WebApp report
and API previously read from the ~1,654 per-query JSON artifacts — sourced from
the Query node (+ READS_FROM / MIGRATES_TO / PART_OF), with the nested
performance/characteristics blocks round-tripped through their JSON columns.
"""

from __future__ import annotations

import pytest

from src.graph.populators import (
    populate_from_assignment,
    populate_from_collector,
    populate_from_schema_design,
)
from src.graph.queries import query_count, query_journey, query_journeys, query_journeys_page


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


def _design(engine: str, prefix: str) -> dict:
    return {
        "trade_offs": [],
        "access_patterns": [
            {
                "pattern_id": f"{prefix}-AP-1",
                "pattern_group": "g",
                "query_ids": ["q1"],
                "description": "d",
                "operation": "op",
                "table_name": "t",
                "design_rps": 1.0,
                "in_scope": True,
            }
        ],
    }


def _assign(engine: str) -> dict:
    return {
        "query_assignments": [
            {
                "query_id": "q1",
                "assigned_engine": engine,
                "confidence": 0.9,
                "source_tables": ["orders"],
                "assignment_reason": "r",
                "in_scope": True,
            }
        ],
        "table_assignments": [],
        "co_dependency_groups": [],
    }


class TestDesignEngineFollowsAssignment:
    """#669: a query designed on several engines reports the engine that owns it."""

    def _build(self, store, assigned: str) -> dict:
        populate_from_collector(_rich_collector(), store)
        populate_from_assignment(_assign(assigned), store)
        # dynamodb is inserted first on purpose (the old [1] picked it every time).
        populate_from_schema_design(_design("dynamodb", "DDB"), "dynamodb", 1, store)
        populate_from_schema_design(_design("opensearch", "OS"), "opensearch", 1, store)
        return query_journey(store, "q1")

    def test_assigned_engine_wins_over_insertion_order(self, graph_store):
        assert self._build(graph_store, "opensearch")["design"]["engine"] == "opensearch"

    def test_assigned_dynamodb_still_reported(self, graph_store):
        assert self._build(graph_store, "dynamodb")["design"]["engine"] == "dynamodb"

    def test_unassigned_design_falls_back_to_name_order(self, graph_store):
        populate_from_collector(_rich_collector(), graph_store)
        populate_from_schema_design(_design("opensearch", "OS"), "opensearch", 1, graph_store)
        populate_from_schema_design(_design("dynamodb", "DDB"), "dynamodb", 1, graph_store)
        assert query_journey(graph_store, "q1")["design"]["engine"] == "dynamodb"


def _many_queries(n: int) -> dict:
    """``n`` collector patterns whose ids sort in a known order (q000, q001, ...)."""
    return {
        "queries": {
            "query_patterns": [
                {
                    "query_id": f"q{i:03d}",
                    "query_text": f"SELECT * FROM t{i % 3} WHERE id = ?",
                    "query_type": "SELECT",
                    "tables_accessed": [f"t{i % 3}", "shared"] if i % 2 else [f"t{i % 3}"],
                    "calls_per_second": float(i),
                }
                for i in reversed(range(n))  # inserted out of order on purpose
            ]
        }
    }


class TestPagedJourneys:
    """#998: a journey page is read with SKIP/LIMIT in the graph query itself."""

    def test_pages_concatenate_to_the_full_list(self, graph_store):
        populate_from_collector(_many_queries(23), graph_store)
        full = query_journeys(graph_store)
        pages = [query_journeys_page(graph_store, skip, 5) for skip in range(0, 25, 5)]
        assert [len(p) for p in pages] == [5, 5, 5, 5, 3]
        assert [j for page in pages for j in page] == full
        assert [j["query_id"] for j in full] == [f"q{i:03d}" for i in range(23)]

    def test_page_keeps_every_table_of_a_query(self, graph_store):
        # The page limit applies to queries, not to the READS_FROM rows they expand to.
        populate_from_collector(_many_queries(4), graph_store)
        page = query_journeys_page(graph_store, 0, 2)
        assert [j["query_id"] for j in page] == ["q000", "q001"]
        assert page[1]["source"]["tables_accessed"] == ["shared", "t1"]

    def test_page_past_the_end_is_empty(self, graph_store):
        populate_from_collector(_many_queries(3), graph_store)
        assert query_journeys_page(graph_store, 3, 10) == []
        assert query_journeys_page(graph_store, 100, 10) == []

    def test_count_matches_the_queries(self, graph_store):
        assert query_count(graph_store) == 0
        assert query_journeys(graph_store) == []
        populate_from_collector(_many_queries(7), graph_store)
        assert query_count(graph_store) == 7

    def test_full_list_reads_in_bounded_batches(self, graph_store, monkeypatch):
        # query_journeys streams the graph page by page instead of in one statement.
        from src.graph import queries as graph_queries

        populate_from_collector(_many_queries(11), graph_store)
        seen: list[tuple[int, int]] = []
        real = graph_queries.query_journeys_page

        def _spy(store, skip, limit):
            seen.append((skip, limit))
            return real(store, skip, limit)

        monkeypatch.setattr(graph_queries, "query_journeys_page", _spy)
        monkeypatch.setattr(graph_queries, "JOURNEY_BATCH_SIZE", 4)
        assert len(graph_queries.query_journeys(graph_store)) == 11
        assert seen == [(0, 4), (4, 4), (8, 4)]

    def test_pages_differ_on_one_reused_connection(self, graph_store):
        # graph_store is a read-write handle: every statement runs on one
        # connection. The engine binds a SKIP/LIMIT parameter once per
        # connection, so the page must not depend on parameters.
        populate_from_collector(_many_queries(6), graph_store)
        first = [j["query_id"] for j in query_journeys_page(graph_store, 0, 2)]
        second = [j["query_id"] for j in query_journeys_page(graph_store, 2, 2)]
        assert first == ["q000", "q001"]
        assert second == ["q002", "q003"]

    @pytest.mark.parametrize(("skip", "limit"), [(-1, 5), (0, 0), (0, -3)])
    def test_invalid_page_is_refused(self, graph_store, skip, limit):
        with pytest.raises(ValueError):
            query_journeys_page(graph_store, skip, limit)
