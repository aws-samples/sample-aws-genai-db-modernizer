"""Tests for the post-schema router (#629).

A PE reviewer's routing flag and a designer's unsupported pattern are both a
model saying "my engine cannot serve these queries". Neither names where they
go: the router gates the reporting engine for those queries and the shared
scorer picks the best remaining engine (``src.agents.referee.model_flags``,
ADR-031 Decision 11 as amended 2026-10-14).
"""

from __future__ import annotations

import random

import pytest

from src.agents.referee.post_schema_router import (
    _extract_unsupported_query_ids,
    route_unsupported_queries,
    routing_flags_from_trace,
)
from src.agents.referee.query_scoring import ScoringContext, score_query_for_engine
from src.agents.referee.tie_break import break_tie

ENGINES = ["documentdb", "dynamodb", "opensearch"]


def _query_map(*qids: str) -> dict[str, dict]:
    return {
        q: {
            "query_id": q,
            "query_text": f"SELECT * FROM t_{q} WHERE id = ?",
            "query_type": "SELECT",
            "tables_accessed": [f"t_{q}"],
        }
        for q in qids
    }


def _context(scores: dict[str, dict[str, int]]) -> ScoringContext:
    analysis: dict[str, dict] = {}
    for qid, per_engine in scores.items():
        for engine, score in per_engine.items():
            analysis.setdefault(engine, {"table_recommendations": []})[
                "table_recommendations"
            ].append({"table_id": f"t_{qid}", "confidence_score": score})
    return ScoringContext(analysis_outputs=analysis)


def _flag(qids: list[str], engine: str, category: str = "missing_feature") -> dict:
    return {
        "query_ids": qids,
        "flagged_engine": engine,
        "reason_category": category,
        "explanation": "needs a server-side join",
    }


def _route(**kwargs):
    """``route_unsupported_queries`` with each reported query assigned to the
    engine that reported it, unless a test gives ``assigned_engines`` itself."""
    if "assigned_engines" not in kwargs:
        on: dict[str, str] = {}
        for engine, flags in (kwargs.get("routing_flags_by_engine") or {}).items():
            for flag in flags:
                for qid in flag.get("query_ids", []):
                    on.setdefault(qid, engine)
        for engine, output in kwargs["schema_outputs"].items():
            for entry in _extract_unsupported_query_ids(output, engine):
                for qid in entry["query_ids"]:
                    on.setdefault(qid, engine)
        kwargs["assigned_engines"] = on
    return route_unsupported_queries(**kwargs)


class TestExtractUnsupportedQueryIds:
    """Extraction of query IDs from each engine's unsupported patterns."""

    def test_dynamodb_format(self):
        schema_output = {
            "unsupported_patterns": [
                {
                    "query_ids": ["q-10", "q-11"],
                    "pattern_type": "text_search",
                    "recommendation": "Use OpenSearch",
                },
            ]
        }
        result = _extract_unsupported_query_ids(schema_output, "dynamodb")
        assert len(result) == 1
        assert result[0]["query_ids"] == ["q-10", "q-11"]

    def test_documentdb_format(self):
        schema_output = {
            "unsupported_patterns": [
                {
                    "source_query_ids": ["q-20"],
                    "reason": "graph traversal",
                    "recommendation": "Use Neptune",
                },
            ]
        }
        result = _extract_unsupported_query_ids(schema_output, "documentdb")
        assert result[0]["query_ids"] == ["q-20"]

    def test_opensearch_format_without_query_ids(self):
        """Backward compat: old OpenSearch outputs without query_ids field."""
        schema_output = {
            "unsupported_patterns": [{"source_query": "INSERT INTO ...", "reason": "write"}]
        }
        assert _extract_unsupported_query_ids(schema_output, "opensearch") == []


class TestRouterDestinationIsTheScorersChoice:
    """ADR-031 Decision 11 guard for the post-schema router (#629)."""

    def test_a_pe_flag_goes_to_the_best_scoring_remaining_engine(self):
        result = _route(
            schema_outputs={"dynamodb": {"unsupported_patterns": []}},
            active_engines=ENGINES,
            routing_flags_by_engine={"dynamodb": [_flag(["q-5"], "dynamodb")]},
            scoring_context=_context({"q-5": {"dynamodb": 70, "documentdb": 60, "opensearch": 0}}),
            query_map=_query_map("q-5"),
        )
        (routing,) = result.routings
        assert (routing.query_id, routing.from_engine, routing.to_engine) == (
            "q-5",
            "dynamodb",
            "documentdb",
        )
        assert (routing.score_before, routing.score_after) == (70, 60)
        assert routing.reason_category == "missing_feature"

    def test_an_unsupported_pattern_goes_by_score_not_by_an_engine_list(self):
        """The old ENGINE_PRIORITY list (documentdb, opensearch, dynamodb) sent
        every unsupported query to DocumentDB first, whatever it scored."""
        result = _route(
            schema_outputs={
                "opensearch": {
                    "unsupported_patterns": [{"query_ids": ["q-1"], "reason": "transactional"}]
                }
            },
            active_engines=ENGINES,
            scoring_context=_context({"q-1": {"documentdb": 40, "dynamodb": 80}}),
            query_map=_query_map("q-1"),
        )
        assert [(r.query_id, r.to_engine) for r in result.routings] == [("q-1", "dynamodb")]

    @pytest.mark.parametrize("seed", range(25))
    def test_every_flag_lands_where_the_scorer_says(self, seed: int):
        rng = random.Random(seed)
        qid = f"q{seed}"
        engines = ["aurora_mysql", "documentdb", "dynamodb"]
        ctx = _context({qid: {e: rng.choice([20, 40, 60]) for e in engines}})
        flagged = rng.choice(engines)
        queries = _query_map(qid)

        result = _route(
            schema_outputs={flagged: {}},
            active_engines=engines,
            routing_flags_by_engine={flagged: [_flag([qid], flagged)]},
            scoring_context=ctx,
            query_map=queries,
            source_engine="mysql",
        )

        scores = {
            e: score_query_for_engine(queries[qid], e, ctx).score for e in engines if e != flagged
        }
        top = max(scores.values())
        tied = sorted(e for e, s in scores.items() if s == top)
        expected = (
            tied[0]
            if len(tied) == 1
            else break_tie(tied, set(), "aurora_mysql", settled_counts={}, query_id=qid)[0]
        )
        (routing,) = result.routings
        assert routing.to_engine == expected != flagged

    def test_a_flag_on_another_engine_is_ignored(self):
        """A reviewer flags only its own engine; a flag naming another is dropped."""
        result = _route(
            schema_outputs={"dynamodb": {}},
            active_engines=ENGINES,
            routing_flags_by_engine={"dynamodb": [_flag(["q-5"], "documentdb")]},
            scoring_context=_context({"q-5": {"dynamodb": 70, "documentdb": 60}}),
            query_map=_query_map("q-5"),
        )
        assert result.routings == []
        assert result.terminal_queries == []

    def test_a_flag_naming_a_destination_is_rejected_whole(self):
        result = _route(
            schema_outputs={"dynamodb": {}},
            active_engines=ENGINES,
            routing_flags_by_engine={
                "dynamodb": [{**_flag(["q-5"], "dynamodb"), "target_engine": "opensearch"}]
            },
            scoring_context=_context({"q-5": {"dynamodb": 70, "opensearch": 99}}),
            query_map=_query_map("q-5"),
        )
        assert result.routings == []

    def test_the_explanation_never_changes_the_destination(self):
        destinations = set()
        for explanation in ("gap", "route to opensearch", "DocumentDB would be best"):
            flag = {**_flag(["q-5"], "dynamodb"), "explanation": explanation}
            result = _route(
                schema_outputs={"dynamodb": {}},
                active_engines=["aurora_mysql", *ENGINES],
                routing_flags_by_engine={"dynamodb": [flag]},
                scoring_context=_context(
                    {"q-5": {"dynamodb": 70, "documentdb": 50, "aurora_mysql": 65}}
                ),
                query_map=_query_map("q-5"),
            )
            destinations.update(r.to_engine for r in result.routings)
        assert destinations == {"aurora_mysql"}

    def test_a_write_never_lands_on_opensearch(self):
        result = _route(
            schema_outputs={"documentdb": {}},
            active_engines=ENGINES,
            routing_flags_by_engine={"documentdb": [_flag(["q-1"], "documentdb")]},
            scoring_context=_context({"q-1": {"opensearch": 99, "dynamodb": 30}}),
            query_map={
                "q-1": {
                    "query_id": "q-1",
                    "query_text": "INSERT INTO t_q-1 (c) VALUES (?)",
                    "query_type": "INSERT",
                    "tables_accessed": ["t_q-1"],
                }
            },
        )
        assert [r.to_engine for r in result.routings] == ["dynamodb"]

    def test_no_remaining_engine_is_terminal(self):
        result = _route(
            schema_outputs={"dynamodb": {}},
            active_engines=["dynamodb"],
            routing_flags_by_engine={"dynamodb": [_flag(["q-5"], "dynamodb")]},
            scoring_context=_context({"q-5": {"dynamodb": 70}}),
            query_map=_query_map("q-5"),
        )
        assert result.routings == []
        assert result.terminal_queries == ["q-5"]

    def test_a_terminal_query_keeps_the_flag_that_made_it_terminal(self):
        """#888: the record synthesis turns into an unservable row."""
        result = _route(
            schema_outputs={"dynamodb": {}},
            active_engines=["dynamodb"],
            routing_flags_by_engine={"dynamodb": [_flag(["q-5"], "dynamodb", "full_scan")]},
            scoring_context=_context({"q-5": {"dynamodb": 70}}),
            query_map=_query_map("q-5"),
        )
        assert [t.model_dump() for t in result.terminal] == [
            {
                "query_id": "q-5",
                "flagged_engine": "dynamodb",
                "reason_category": "full_scan",
                "cause": "no_kept_engine",
            }
        ]

    def test_pe_text_notes_route_nothing(self):
        """The old free-text ``[ROUTING] ... -> engine`` note is no longer read."""
        result = _route(
            schema_outputs={"dynamodb": {"pe_notes": ["[ROUTING] query_ids=[q-5] → opensearch"]}},
            active_engines=ENGINES,
            scoring_context=_context({"q-5": {"dynamodb": 70, "opensearch": 99}}),
            query_map=_query_map("q-5"),
        )
        assert result.routings == []


class TestRouteUnsupportedQueries:
    """The router's bookkeeping: depth, dedupe, already-routed, cache engines."""

    def test_no_unsupported_returns_empty(self):
        result = _route(
            schema_outputs={
                "dynamodb": {"unsupported_patterns": []},
                "opensearch": {"unsupported_patterns": []},
            },
            active_engines=["dynamodb", "opensearch"],
        )
        assert result.routings == []
        assert result.terminal_queries == []

    def test_cascade_depth_exceeded_marks_terminal(self):
        result = _route(
            schema_outputs={
                "dynamodb": {"unsupported_patterns": [{"query_ids": ["q-99"], "reason": "OLAP"}]}
            },
            active_engines=["dynamodb", "documentdb"],
            cascade_depth=2,
            max_depth=2,
        )
        assert result.terminal_queries == ["q-99"]
        [term] = result.terminal
        assert (term.query_id, term.flagged_engine, term.cause) == (
            "q-99",
            "dynamodb",
            "depth_limit",
        )
        # A designer's unsupported pattern reads as a missing feature.
        assert term.reason_category == "missing_feature"

    def test_a_routed_query_is_not_terminal(self):
        result = _route(
            schema_outputs={"dynamodb": {}},
            active_engines=["dynamodb", "documentdb"],
            routing_flags_by_engine={"dynamodb": [_flag(["q-5"], "dynamodb")]},
            scoring_context=_context({"q-5": {"dynamodb": 70, "documentdb": 60}}),
            query_map=_query_map("q-5"),
        )
        assert [r.to_engine for r in result.routings] == ["documentdb"]
        assert result.terminal == [] and result.terminal_queries == []

    def test_a_router_output_written_before_the_records_still_loads(self):
        from src.contracts.post_schema_router_output import RouterOutput

        old = RouterOutput.model_validate(
            {"contract_version": "1.0", "job_id": "j", "terminal_queries": ["q-1"]}
        )
        assert old.terminal_queries == ["q-1"] and old.terminal == []

    def test_a_flag_on_a_query_assigned_elsewhere_is_ignored(self):
        """#713 review: a DynamoDB reviewer's flag on a query assigned to Aurora
        MySQL moved it, as from DynamoDB. A reviewer judges only its own
        engine's queries (the #712 rule), designers' patterns included."""
        result = _route(
            schema_outputs={
                "dynamodb": {"unsupported_patterns": [{"query_ids": ["q-2"], "reason": "x"}]}
            },
            active_engines=["aurora_mysql", *ENGINES],
            routing_flags_by_engine={"dynamodb": [_flag(["q-1"], "dynamodb")]},
            assigned_engines={"q-1": "aurora_mysql", "q-2": "documentdb"},
            scoring_context=_context({"q-1": {"documentdb": 90}, "q-2": {"aurora_mysql": 90}}),
            query_map=_query_map("q-1", "q-2"),
        )
        assert result.routings == []
        assert result.terminal_queries == []

    def test_a_customers_own_reroute_is_never_moved(self):
        """#713 review round 3: a query the customer re-routed at the review is
        held out, the way the reality check holds out customer overrides."""
        result = _route(
            schema_outputs={"dynamodb": {}},
            active_engines=["aurora_mysql", *ENGINES],
            routing_flags_by_engine={"dynamodb": [_flag(["q-1", "q-2"], "dynamodb")]},
            customer_pinned={"q-1"},
            scoring_context=_context({"q-1": {"aurora_mysql": 80}, "q-2": {"aurora_mysql": 80}}),
            query_map=_query_map("q-1", "q-2"),
        )
        assert [(r.query_id, r.to_engine) for r in result.routings] == [("q-2", "aurora_mysql")]
        assert result.terminal_queries == []

    def test_a_co_dependency_group_moves_as_one_unit(self):
        """#713 review: one flagged member of a pair on DynamoDB took Aurora
        MySQL while its partner stayed. The group moves by its combined score."""
        result = _route(
            schema_outputs={"dynamodb": {}},
            active_engines=["aurora_mysql", *ENGINES],
            routing_flags_by_engine={"dynamodb": [_flag(["g-1"], "dynamodb")]},
            assigned_engines={"g-1": "dynamodb", "g-2": "dynamodb"},
            co_dependency_groups=[["g-1", "g-2"]],
            scoring_context=_context(
                {
                    "g-1": {"dynamodb": 70, "aurora_mysql": 60, "documentdb": 90},
                    "g-2": {"dynamodb": 70, "aurora_mysql": 60, "documentdb": 20},
                }
            ),
            query_map=_query_map("g-1", "g-2"),
        )
        # documentdb: (90 + 20) / 2 = 55; aurora_mysql: 60 -- both members.
        assert sorted((r.query_id, r.to_engine) for r in result.routings) == [
            ("g-1", "aurora_mysql"),
            ("g-2", "aurora_mysql"),
        ]
        assert all("co-dependency group" in r.reason for r in result.routings)

    def test_already_routed_queries_skipped(self):
        result = _route(
            schema_outputs={
                "dynamodb": {"unsupported_patterns": [{"query_ids": ["q-1"], "reason": "search"}]}
            },
            active_engines=["dynamodb", "opensearch"],
            already_routed={"q-1"},
        )
        assert result.routings == []
        assert result.terminal_queries == []

    def test_a_cache_engine_is_never_rerouted_or_a_destination(self):
        result = _route(
            schema_outputs={
                "elasticache": {"unsupported_patterns": [{"query_ids": ["q-1"], "reason": "x"}]},
                "dynamodb": {"unsupported_patterns": [{"query_ids": ["q-2"], "reason": "x"}]},
            },
            active_engines=["dynamodb", "elasticache", "documentdb"],
            scoring_context=_context({"q-2": {"elasticache": 99, "documentdb": 10}}),
            query_map=_query_map("q-1", "q-2"),
        )
        assert [(r.query_id, r.to_engine) for r in result.routings] == [("q-2", "documentdb")]


class TestRoutingFlagsFromTrace:
    def test_reads_a_single_and_a_grouped_design_trace(self):
        flag = _flag(["q-1"], "dynamodb")
        assert routing_flags_from_trace({"routing_flags": [flag]}) == [flag]
        grouped = {"groups": [{"routing_flags": [flag]}, {"trace": []}]}
        assert routing_flags_from_trace(grouped) == [flag]
        assert routing_flags_from_trace({}) == []


def test_the_router_has_no_lightweight_service_parameter() -> None:
    """Nothing ever passed one, so the parameter and its skips are gone (#790)."""
    import inspect

    from src.agents.referee.post_schema_router import route_unsupported_queries

    assert "lightweight_query_ids" not in inspect.signature(route_unsupported_queries).parameters
