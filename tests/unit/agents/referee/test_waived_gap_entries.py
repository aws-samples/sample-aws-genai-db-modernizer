"""A designer's unsupported entry that names a waived gap does not move a query (#1650).

On the saved Bedrock runs the wordpress comment ``COUNT`` (and a discourse visit count)
moved off DynamoDB in one of two runs of every model variant: routing had placed the
key-scoped count on DynamoDB past the catalog waiver (``ddb-key-scoped-count``: a
``Query`` with ``Select = COUNT``), and the designer still listed it as an
``aggregation`` unsupported pattern. ADR-031 Amendment 18: an entry whose own category
is the waived capability names a gap decided before the gate, so the router records it
as waived and does not move the query; synthesis states it as served through the
waiver. Any other entry, and every PE reviewer flag, still counts.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.agents.referee.post_schema_router import route_unsupported_queries
from src.agents.referee.query_scoring import ScoringContext, score_query_for_engine

ENGINES = ("aurora_postgresql", "dynamodb")


def _table(name: str, rows: int | None, columns: list[str], **extra: Any) -> dict[str, Any]:
    return {
        "table_id": f"forum.{name}",
        "table_name": name,
        "schema_name": "forum",
        "row_count": rows,
        "size_mb": 0.02,
        "primary_key": ["id"],
        "columns": [{"column_name": c, "data_type": "integer"} for c in columns],
        **extra,
    }


POSTS = _table(
    "posts",
    5_000,
    ["id", "topic_id", "post_type"],
    foreign_keys=[{"columns": ["topic_id"], "referenced_table": "topics"}],
)


def _q(qid: str, text: str, table: str) -> dict[str, Any]:
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": text.split()[0].upper(),
        "tables_accessed": [f"forum.{table}"],
        "calls_per_second": 1.0,
    }


INS = _q("ins", "INSERT INTO posts (id, topic_id, post_type) VALUES ($1, $2, $3)", "posts")
CNT = _q("cnt", "SELECT COUNT(*) FROM posts WHERE topic_id = $1 AND post_type = $2", "posts")
BY_ID = _q("byid", "SELECT post_type FROM posts WHERE id = $1", "posts")
QUERIES = [INS, CNT, BY_ID]


def _rec(table_id: str, engine: str) -> dict[str, Any]:
    """The same table scores on every engine: only the query's class tells them apart."""
    from src.contracts.analysis_output import ScoreBreakdown
    from src.tools.analysis.scoring import compute_confidence

    breakdown = {
        "pattern_match_score": 70,
        "complexity_score": 80,
        "performance_score": 80,
        "cost_score": 80,
    }
    weights = {"pattern_match": 0.4, "complexity": 0.3, "performance": 0.2, "cost": 0.1}
    return {
        "table_id": table_id,
        "confidence_score": compute_confidence(ScoreBreakdown(**breakdown), weights),
        "score_breakdown": breakdown,
    }


def _ctx() -> ScoringContext:
    collector = {
        "job_id": "t",
        "metadata": {"source_database": {"engine": "postgresql"}},
        "database_schema": {"tables": [POSTS]},
        "queries": {"query_patterns": QUERIES},
    }
    return ScoringContext.from_pipeline(
        {e: {"table_recommendations": [_rec(t["table_id"], e) for t in (POSTS,)]} for e in ENGINES},
        {"query_capabilities": {"cnt": ["aggregation"]}},
        collector,
    ).with_sources_of_truth({"forum.posts": "dynamodb"})


def _route(flag: dict | None = None, unsupported: list[dict] | None = None):
    output = {"unsupported_patterns": unsupported or []}
    return route_unsupported_queries(
        schema_outputs={"dynamodb": output},
        active_engines=list(ENGINES),
        routing_flags_by_engine={"dynamodb": [flag] if flag else []},
        scoring_context=_ctx(),
        query_map={q["query_id"]: q for q in QUERIES},
        assigned_engines={q["query_id"]: "dynamodb" for q in QUERIES},
    )


def _entry(qid: str, pattern_type: str | None) -> dict:
    entry = {
        "query_ids": [qid],
        "recommendation": "DynamoDB has no server-side COUNT; served by Select=COUNT.",
    }
    if pattern_type is not None:
        entry["pattern_type"] = pattern_type
    return entry


def _flag(qid: str, category: str) -> dict:
    return {
        "query_ids": [qid],
        "flagged_engine": "dynamodb",
        "reason_category": category,
        "explanation": "DynamoDB has no server-side COUNT.",
    }


def _moved(routed) -> list[tuple[str, str | None]]:
    return [(r.query_id, r.to_engine) for r in routed.routings]


class TestAnEntryOnAWaivedCount:
    @pytest.mark.passes_on_main  # the #377 waiver, unchanged
    def test_the_waiver_lets_dynamodb_serve_it(self) -> None:
        assert score_query_for_engine(CNT, "dynamodb", _ctx()).score is not None

    def test_an_entry_naming_the_waived_capability_moves_nothing_and_is_recorded(self) -> None:
        routed = _route(unsupported=[_entry("cnt", "aggregation")])
        assert routed.routings == [] and routed.terminal_queries == []
        assert [w.model_dump() for w in routed.waived] == [
            {
                "query_id": "cnt",
                "flagged_engine": "dynamodb",
                "capability": "aggregation",
                "rule_id": "ddb-key-scoped-count",
            }
        ]

    @pytest.mark.passes_on_main  # another gap still moves it, as before
    @pytest.mark.parametrize("pattern_type", ["text_search", "multi_table_join", None])
    def test_an_entry_naming_another_gap_still_moves_it(self, pattern_type) -> None:
        routed = _route(unsupported=[_entry("cnt", pattern_type)])
        assert _moved(routed) == [("cnt", "aurora_postgresql")]
        assert routed.waived == []

    @pytest.mark.passes_on_main  # a reviewer's flag names no capability, so it counts
    @pytest.mark.parametrize("category", ["missing_feature", "full_scan", "other"])
    def test_a_reviewers_flag_still_moves_it(self, category: str) -> None:
        assert _moved(_route(_flag("cnt", category))) == [("cnt", "aurora_postgresql")]

    @pytest.mark.passes_on_main  # no waiver, no change
    def test_an_aggregation_entry_on_a_query_no_waiver_covers_still_moves_it(self) -> None:
        routed = _route(unsupported=[_entry("byid", "aggregation")])
        assert [r.query_id for r in routed.routings] == ["byid"]
        assert routed.waived == []

    def test_one_entry_naming_both_moves_only_the_unwaived_query(self) -> None:
        entry = {**_entry("cnt", "aggregation"), "query_ids": ["cnt", "byid"]}
        routed = _route(unsupported=[entry])
        assert [r.query_id for r in routed.routings] == ["byid"]
        assert [w.query_id for w in routed.waived] == ["cnt"]


class TestSynthesisStatesAWaivedEntryAsServed:
    def _risks(self, waived):
        from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
        from src.agents.referee.synthesis_report import build_risk_assessment

        data = SynthesisData(
            job_id="j",
            database_name="forum",
            collector={"queries": {"query_patterns": QUERIES}},
            engines={
                "dynamodb": EngineArtifacts(
                    "dynamodb",
                    analysis={},
                    schema_design={
                        "table_definitions": [],
                        "access_patterns": [{"pattern_id": "DDB-AP-1", "query_ids": ["cnt"]}],
                        "unsupported_patterns": [_entry("cnt", "aggregation")],
                    },
                )
            },
            assignment={
                "query_assignments": [
                    {"query_id": q["query_id"], "assigned_engine": "dynamodb", "in_scope": True}
                    for q in QUERIES
                ]
            },
        )
        return build_risk_assessment(data, waived=waived)

    def test_a_waived_entry_is_a_resolved_record_not_an_open_risk(self) -> None:
        result = self._risks({"dynamodb": {"cnt"}})
        assert not [r for r in result["risks"] if "cnt" in (r.get("query_ids") or [])]
        [record] = [r for r in result["resolved_risks"] if "cnt" in (r.get("query_ids") or [])]
        assert record["reason"].startswith("served as routed")
        assert "waiver" in record["reason"]

    @pytest.mark.passes_on_main  # with no waived record it stays the MEDIUM risk it was
    def test_without_the_record_it_stays_an_open_medium_risk(self) -> None:
        result = self._risks(None)
        [risk] = [r for r in result["risks"] if "cnt" in (r.get("query_ids") or [])]
        assert risk["severity"] == "MEDIUM"


class TestTheDeliverablesListAWaivedEntryAsServed:
    def _summaries(self, waived):
        from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
        from src.agents.referee.synthesis_handler import _build_schema_summaries

        schema = {
            "table_definitions": [{"table_name": "Posts", "source_tables": ["forum.posts"]}],
            "access_patterns": [{"pattern_id": "DDB-AP-1", "query_ids": ["cnt"]}],
            "unsupported_patterns": [_entry("cnt", "aggregation"), _entry("byid", "aggregation")],
        }
        data = SynthesisData(
            job_id="j",
            database_name="forum",
            collector={"queries": {"query_patterns": QUERIES}},
            engines={"dynamodb": EngineArtifacts("dynamodb", analysis={}, schema_design=schema)},
            assignment={
                "query_assignments": [
                    {"query_id": q["query_id"], "assigned_engine": "dynamodb", "in_scope": True}
                    for q in QUERIES
                ]
            },
        )
        return _build_schema_summaries(data, waived)["dynamodb"]

    def test_report_json_moves_it_out_of_the_unsupported_patterns(self) -> None:
        summary = self._summaries({"dynamodb": {"cnt"}})
        assert [e["query_ids"] for e in summary["unsupported_patterns"]] == [["byid"]]
        assert [e["query_ids"] for e in summary["served_by_waiver"]] == [["cnt"]]

    @pytest.mark.passes_on_main  # no waived record: the entry stays unsupported
    def test_without_the_record_it_stays_unsupported(self) -> None:
        summary = self._summaries(None)
        assert len(summary["unsupported_patterns"]) == 2
        assert "served_by_waiver" not in summary

    def test_the_engineering_report_lists_it_as_served_not_unsupported(self) -> None:
        from src.report.engineering_report import render_engineering_report_md

        summary = self._summaries({"dynamodb": {"cnt"}})
        md = render_engineering_report_md(
            {"database_name": "forum", "schema_designs": {"dynamodb": summary}}
        )
        section = md[md.index("## Target schemas by engine") :]
        assert "**Unsupported patterns (1):**" in section
        assert "**Served as routed through a catalog waiver (1):**" in section
