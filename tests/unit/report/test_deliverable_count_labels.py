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


def _shared_tables_report() -> dict[str, Any]:
    """DynamoDB is the recommended engine of 19 tables; its design reads 21 (run 5)."""
    ddb_mapped = [f"wp.t{i}" for i in range(19)]
    return {
        "database_name": "wordpress",
        "ranking": [
            {"target": "dynamodb", "workload_percent": 58.9},
            {"target": "elasticache", "workload_percent": 31.8},
            {"target": "aurora_mysql", "workload_percent": 9.3},
        ],
        "recommended_architecture": {
            "databases": [
                {"service": "dynamodb", "table_count": 19},
                {"service": "aurora_mysql", "table_count": 2},
            ]
        },
        "schema_designs": {
            "dynamodb": {
                "status": "completed",
                "tables": [
                    {"table_name": "a", "source_tables": ddb_mapped[:10] + ["wp.postmeta"]},
                    {"table_name": "b", "source_tables": ddb_mapped[10:] + ["wp.order_items"]},
                ],
            },
            "elasticache": {"status": "completed", "tables": [{}, {}]},
            "aurora_mysql": {
                "status": "completed",
                "tables": [{"source_tables": ["wp.postmeta", "wp.usermeta"]}],
            },
        },
    }


class TestSharedSourceTables:
    """#257: Scope "19 source tables" (tables whose recommended engine is DynamoDB)
    vs the 21 source tables the DynamoDB design serves."""

    def test_scope_names_the_tables_the_design_also_serves(self) -> None:
        rows = {e["engine"]: e for e in renderers._architecture_engines(_shared_tables_report())}
        assert rows["dynamodb"]["scope"] == "19 source tables (21 incl. shared)"
        assert rows["dynamodb"]["migrates"] == 19

    def test_scope_unchanged_when_the_design_serves_only_mapped_tables(self) -> None:
        rows = {e["engine"]: e for e in renderers._architecture_engines(_shared_tables_report())}
        assert rows["aurora_mysql"]["scope"] == "2 source tables"
        assert rows["elasticache"]["scope"] == "2 key designs"

    def test_decision_report_total_still_sums_mapped_tables(self) -> None:
        html = renderers.render_decision_report_html(_shared_tables_report())
        assert "19 source tables (21 incl. shared)" in html
        assert "21 tables migrate" in html
        assert "<h3>21</h3><p>Tables migrate</p>" in html
        assert "1921" not in html

    def test_deck_totals_still_sum_mapped_tables(self) -> None:
        f = pptx_report.derive(_shared_tables_report(), {})
        assert f["migrated"] == 21
        assert [w["note"].split(" ")[0] for w in f["waves"] if "source table" in w["note"]] == [
            "21"
        ]


def _risk(rid: str, sev: str, n_queries: int, engine: str = "dynamodb") -> dict[str, Any]:
    return {
        "risk_id": rid,
        "severity": sev,
        "risk_type": "MIGRATION_COMPLEXITY",
        "description": f"[{engine}] aggregation: count rows for {rid} in the application",
        "mitigation": "Keep a counter per key via UpdateItem.",
        "query_ids": [f"{rid}-q{i}" for i in range(n_queries)],
    }


def _risk_slide(risks: list[dict[str, Any]]) -> tuple[list[list[str]], str]:
    rep = {**_cache_report(), "risk_assessment": {"risks": risks}}
    f = pptx_report.derive(rep, {})
    slide = pptx_report.slide_risk(pptx_report.open_deck(keep=1), f)
    rows = [
        [c.text for c in row.cells] for sh in slide.shapes if sh.has_table for row in sh.table.rows
    ]
    caption = next(
        sh.text_frame.text
        for sh in slide.shapes
        if sh.has_text_frame
        and ("risk" in sh.text_frame.text.lower())
        and ("mitigation" in sh.text_frame.text.lower() or "remain" in sh.text_frame.text.lower())
    )
    return rows, caption


class TestRiskProfileSeverityFallback:
    """#249: with no HIGH risks the Risk Profile table was empty and the caption
    still said "Each HIGH risk carries ..."."""

    def test_only_medium_risks_lists_the_top_medium_risks(self) -> None:
        risks = [_risk(f"RISK-00{i}", "MEDIUM", n) for i, n in enumerate([1, 6, 2, 1, 3], 1)]
        rows, caption = _risk_slide(risks)
        # The busiest four, by affected-query count; the count comes from query_ids.
        assert [r[0] for r in rows[1:]] == ["RISK-002", "RISK-005", "RISK-003", "RISK-001"]
        assert rows[1][3] == "6"
        assert caption.startswith("No HIGH risks remain; the top MEDIUM risks are below.")
        assert "Each HIGH risk" not in caption

    def test_only_low_risks_lists_the_low_risks(self) -> None:
        rows, caption = _risk_slide([_risk("RISK-001", "LOW", 2)])
        assert [r[0] for r in rows[1:]] == ["RISK-001"]
        assert caption.startswith("No HIGH or MEDIUM risks remain; the top LOW risk is below.")

    def test_high_risks_keep_the_high_caption_and_rows(self) -> None:
        rows, caption = _risk_slide([_risk("RISK-001", "MEDIUM", 9), _risk("RISK-002", "HIGH", 1)])
        assert [r[0] for r in rows[1:]] == ["RISK-002"]
        assert caption.startswith(
            "Each HIGH risk carries an affected-query count and a documented mitigation."
        )

    def test_no_risks_says_so(self) -> None:
        rows, caption = _risk_slide([])
        assert rows[1:] == []
        assert caption.startswith("No open risks remain.")


def _mapped_report() -> dict[str, Any]:
    """22 mapped tables: 19 DynamoDB + 2 Aurora MySQL migrate, 1 maps to ElastiCache."""
    rep = _shared_tables_report()
    rep["table_mappings"] = [
        {"source_table": f"wp.t{i}", "recommended_database": "dynamodb"} for i in range(19)
    ] + [
        {"source_table": "wp.postmeta", "recommended_database": "aurora_mysql"},
        {"source_table": "wp.usermeta", "recommended_database": "aurora_mysql"},
        {"source_table": "wp.order_items", "recommended_database": "elasticache"},
    ]
    rep["assignment_summary"] = {"query_count": 107, "co_dependency_groups": 1}
    rep["query_groups"] = [
        {"group_name": f"Group {i}", "engines": ["dynamodb"], "access_patterns": []}
        for i in range(29)
    ]
    rep["risk_assessment"] = {
        "overall_risk_level": "LOW",
        "risks": [_risk(f"RISK-00{i}", "MEDIUM", 1) for i in range(1, 9)],
        "resolved_risks": [
            {"engine": "dynamodb", "severity": "HIGH", "description": "[dynamodb] x"}
        ]
        * 4,
    }
    rep["summary_deterministic"] = (
        "22 source tables mapped to aurora_mysql, dynamodb, elasticache. "
        "8 risk(s) identified (overall: LOW; 4 resolved by the assignment)."
    )
    return rep


class TestUnlabelledCounts:
    """#258: query groups vs co-dependency groups, mapped vs migrating tables, and
    open vs resolved risks."""

    def test_engineering_report_calls_query_groups_query_groups(self) -> None:
        md = renderers.render_engineering_report_md(_mapped_report())
        assert "## Query groups (29)" in md
        assert "co-dependency groups (29)" not in md
        assert "assignment's 1 co-dependency group" in md

    def test_migration_map_heading_splits_migrating_and_cache_tables(self) -> None:
        md = renderers.render_engineering_report_md(_mapped_report())
        assert "## Migration map (22 tables: 21 migrate, 1 to the cache layer)" in md

    def test_migration_map_heading_plain_when_every_table_migrates(self) -> None:
        rep = _mapped_report()
        rep["table_mappings"] = rep["table_mappings"][:21]
        md = renderers.render_engineering_report_md(rep)
        assert "## Migration map (21 tables)" in md

    def test_deck_footer_reconciles_mapped_and_migrating_tables(self) -> None:
        f = pptx_report.derive(_mapped_report(), {})
        slide = pptx_report.slide_summary(pptx_report.open_deck(keep=1), f)
        assert any(
            "21 of the 22 mapped source tables move to a purpose-built engine" in t
            for t in _shape_texts(slide)
        )

    def test_decision_report_separates_open_and_resolved_risks(self) -> None:
        html = renderers.render_decision_report_html(_mapped_report(), trust_generated_summary=True)
        assert "8 open migration risks (0 high, 8 medium) across migration complexity" in html
        assert "4 more were resolved by the assignment." in html
        assert "4 resolved by the assignment)" not in html

    def test_summary_risk_sentence_separates_open_and_resolved(self) -> None:
        out = renderers.label_resolved_risks(_mapped_report()["summary_deterministic"])
        assert "8 open risk(s) (overall: LOW); 4 more were resolved by the assignment." in out
        assert renderers.label_resolved_risks(out) == out

    def test_deck_summary_separates_open_and_resolved(self) -> None:
        f = pptx_report.derive(_mapped_report(), {})
        assert "8 open risk(s) (overall: LOW); 4 more were resolved by the assignment." in (
            f["summary"]
        )
