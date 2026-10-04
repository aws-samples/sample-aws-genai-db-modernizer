"""Counts that appear in more than one deliverable say what they count.

The rubric judge on the wordpress validation runs read several pairs of numbers as
contradictions because the deck, the Decision Report and the Engineering Report
counted different things under the same words. Each class below covers one pair.
"""

from __future__ import annotations

from typing import Any

from src.report import pptx_report, renderers


def _ap(pid: str, engine: str, in_scope: bool = True) -> dict[str, Any]:
    return {"pattern_id": pid, "engine": engine, "in_scope": in_scope}


def _design_report() -> dict[str, Any]:
    """DynamoDB design with 51 access patterns, 4 of them out of scope (run 5)."""
    ddb = [_ap(f"DDB-AP-{i}", "dynamodb") for i in range(1, 48)]
    ddb += [_ap(f"DDB-AP-{i}", "dynamodb", in_scope=False) for i in range(48, 52)]
    ec = [_ap(f"EC-AP-{i}", "elasticache") for i in range(1, 14)]
    return {
        "database_name": "wordpress",
        "schema_designs": {
            "dynamodb": {
                "status": "completed",
                "tables": [{"table_name": "wp_options", "source_tables": ["wp.wp_options"]}],
                "access_pattern_count": 51,
            },
            "elasticache": {
                "status": "completed",
                "tables": [{"table_name": "opt:{name}", "source_tables": ["wp.wp_options"]}],
                "access_pattern_count": 13,
            },
        },
        # The same pattern can sit in two groups; it is counted once.
        "query_groups": [
            {"group_name": "Option reads", "engines": ["dynamodb"], "access_patterns": ddb[:30]},
            {"group_name": "Post reads", "engines": ["dynamodb"], "access_patterns": ddb[25:]},
            {"group_name": "Cache", "engines": ["elasticache"], "access_patterns": ec},
        ],
    }


RUN5_SUMMARY = (
    "Analyzed 50 source tables and 107 query patterns across 3 target database(s). "
    "Schema design produced 36 target objects and 60 in-scope access patterns across "
    "29 query groups (dynamodb: 15 target tables, 47 access patterns; elasticache: 12 key "
    "designs, 13 access patterns; aurora_mysql: 9 target tables)."
)


class TestAccessPatternScope:
    """#255: deck "DynamoDB: 47 access patterns" (in scope) vs Engineering Report
    "51 access patterns" (all)."""

    def test_counts_in_and_out_of_scope_patterns_per_engine(self) -> None:
        assert renderers.access_pattern_scope(_design_report()) == {
            "dynamodb": (47, 4),
            "elasticache": (13, 0),
        }

    def test_engineering_heading_splits_out_of_scope_patterns(self) -> None:
        md = renderers.render_engineering_report_md(_design_report())
        assert (
            "### dynamodb (1 target object, 51 access patterns: 47 in scope, 4 out of scope)" in md
        )

    def test_engineering_heading_unchanged_when_all_patterns_are_in_scope(self) -> None:
        md = renderers.render_engineering_report_md(_design_report())
        assert "### elasticache (1 target object, 13 access patterns)" in md

    def test_summary_labels_per_engine_counts_as_in_scope(self) -> None:
        out = renderers.label_in_scope_access_patterns(RUN5_SUMMARY)
        assert "dynamodb: 15 target tables, 47 in-scope access patterns;" in out
        assert "elasticache: 12 key designs, 13 in-scope access patterns;" in out
        # The aggregate was already labelled and is left alone.
        assert "60 in-scope access patterns across" in out
        assert "in-scope in-scope" not in out

    def test_summary_labelling_is_idempotent(self) -> None:
        once = renderers.label_in_scope_access_patterns(RUN5_SUMMARY)
        assert renderers.label_in_scope_access_patterns(once) == once

    def test_deck_summary_says_in_scope(self) -> None:
        rep = {**_design_report(), "summary_deterministic": RUN5_SUMMARY}
        f = pptx_report.derive(rep, {})
        assert "DynamoDB: 15 target tables, 47 in-scope access patterns" in f["summary"]

    def test_decision_report_fallback_summary_says_in_scope(self) -> None:
        rep = {**_design_report(), "summary_deterministic": RUN5_SUMMARY}
        html = renderers.render_decision_report_html(rep)
        assert "dynamodb: 15 target tables, 47 in-scope access patterns" in html


def _leaderboard_export() -> dict[str, Any]:
    """15 leaderboard queries, 14 assigned to ElastiCache and 1 to Aurora MySQL (run 5)."""
    ids = [f"lb-{i}" for i in range(15)]
    items = [
        {"query_id": q, "assignment": {"assigned_engine": "elasticache" if i < 14 else "x"}}
        for i, q in enumerate(ids)
    ]
    patterns = [{"query_type": "SELECT", "tables_accessed": ["t"]} for _ in range(107)]
    return {
        "collector": {"queries": {"query_patterns": patterns}},
        "results": {
            "triage_summary": {
                "signals": [
                    {
                        "signal": "leaderboard_pattern",
                        "query_count": 15,
                        "targets": ["elasticache"],
                        "query_ids": ids,
                    }
                ]
            }
        },
        "queryJourneys": {"total": 15, "items": items},
    }


def _cache_report() -> dict[str, Any]:
    return {
        "ranking": [
            {"target": "elasticache", "confidence_score": 48, "workload_percent": 31.8},
            {"target": "dynamodb", "confidence_score": 50, "workload_percent": 68.2},
        ],
        "recommended_architecture": {"databases": [{"service": "dynamodb", "table_count": 19}]},
        "schema_designs": {"elasticache": {"tables": [{}]}},
    }


def _shape_texts(slide) -> list[str]:
    out = []
    for sh in slide.shapes:
        if sh.has_text_frame:
            out.append(sh.text_frame.text)
        if sh.has_table:
            out += [c.text for row in sh.table.rows for c in row.cells]
    return out


class TestLeaderboardCounts:
    """#256: page 4 lists 15 leaderboard queries (whole workload), page 5 says
    "14 leaderboard / top-n queries routed to ElastiCache"."""

    def test_evidence_names_the_routed_share_of_the_signal(self) -> None:
        f = pptx_report.derive(_cache_report(), _leaderboard_export())
        against = next(d for d in f["decisions"] if d["question"].startswith("Confirm"))["against"]
        assert "14 of 15 leaderboard / top-n queries routed to ElastiCache" in against

    def test_evidence_keeps_the_plain_count_when_every_query_was_routed(self) -> None:
        text = pptx_report._evidence_text(
            {"name": "leaderboard_pattern", "count": 15, "served": 15}, "elasticache"
        )
        assert text == "15 leaderboard / top-n queries routed to ElastiCache"

    def test_workload_table_says_it_counts_the_whole_workload(self) -> None:
        f = pptx_report.derive(_cache_report(), _leaderboard_export())
        slide = pptx_report.slide_workload(pptx_report.open_deck(keep=1), f)
        texts = _shape_texts(slide)
        assert any("WHOLE WORKLOAD" in t for t in texts)
        assert "Leaderboard / top-N (ORDER BY + LIMIT)" in texts
