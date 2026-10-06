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
        out = renderers.label_in_scope_access_patterns(RUN5_SUMMARY, _design_report())
        assert "dynamodb: 15 target tables, 47 in-scope access patterns;" in out
        assert "elasticache: 12 key designs, 13 in-scope access patterns;" in out
        # The aggregate was already labelled and is left alone.
        assert "60 in-scope access patterns across" in out
        assert "in-scope in-scope" not in out

    def test_summary_labelling_is_idempotent(self) -> None:
        once = renderers.label_in_scope_access_patterns(RUN5_SUMMARY, _design_report())
        assert renderers.label_in_scope_access_patterns(once, _design_report()) == once

    def test_count_that_is_not_the_in_scope_total_is_left_alone(self) -> None:
        """An older or fallback summary may count all patterns (51); labelling that
        "in-scope" would be false (#267 review)."""
        text = RUN5_SUMMARY.replace("47 access patterns", "51 access patterns")
        out = renderers.label_in_scope_access_patterns(text, _design_report())
        assert "dynamodb: 15 target tables, 51 access patterns;" in out
        assert "13 in-scope access patterns" in out

    def test_without_query_groups_nothing_is_labelled(self) -> None:
        assert renderers.label_in_scope_access_patterns(RUN5_SUMMARY, {}) == RUN5_SUMMARY

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
        assert "<p>Tables migrate</p><h3>21</h3>" in html
        assert "1921" not in html

    def test_deck_totals_still_sum_mapped_tables(self) -> None:
        f = pptx_report.derive(_shared_tables_report(), {})
        # The schema-design-based figure (#257/#258) is preferred when it has
        # one; #225 only needs the fallback below it for a
        # report where that figure is 0 but a wave clearly moves real tables.
        assert f["migrated"] == 21


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
        and any(w in sh.text_frame.text.lower() for w in ("mitigation", "remain", "recognised"))
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
        assert rows[1][4] == "6"
        # Each row shows its own mitigation, not mitigation_strategies[0].
        assert rows[1][3] == "Keep a counter per key via UpdateItem."
        assert caption.startswith("No HIGH risks remain; the table shows the top MEDIUM risks.")
        assert "below" not in caption and "above" not in caption
        assert "Each HIGH risk" not in caption

    def test_only_low_risks_lists_the_low_risks(self) -> None:
        rows, caption = _risk_slide([_risk("RISK-001", "LOW", 2)])
        assert [r[0] for r in rows[1:]] == ["RISK-001"]
        assert caption.startswith(
            "No HIGH or MEDIUM risks remain; the table shows the top LOW risk."
        )

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
            "21 of the 22 mapped source tables (21 migrate, 1 to the cache layer) move to a "
            "purpose-built engine" in t
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


class TestNumberFormatting:
    """#259: the Engineering Report printed Design RPS as ``17.460499999999996``."""

    def test_fmt_num_rounds_and_groups(self) -> None:
        assert renderers.fmt_num(17.460499999999996) == "17.46"
        assert renderers.fmt_num(17.020000000000003) == "17.02"
        assert renderers.fmt_num(3.0) == "3"
        assert renderers.fmt_num(12537) == "12,537"
        assert renderers.fmt_num(1234567.891) == "1,234,567.89"
        assert renderers.fmt_num(58.900000001, 1) == "58.9"
        assert renderers.fmt_num(True) == "True"
        assert renderers.fmt_num("-") == "-"
        assert renderers.fmt_num(None) == "None"

    def test_engineering_report_rounds_design_rps(self) -> None:
        rep = _design_report()
        rep["query_groups"][0]["total_design_rps"] = 17.460499999999996
        rep["query_groups"][1]["total_design_rps"] = 1234.5
        md = renderers.render_engineering_report_md(rep)
        assert "| 17.46 |" in md
        assert "| 1,234.5 |" in md
        assert "17.4604" not in md

    def test_decision_report_rounds_workload_share(self) -> None:
        rep = _shared_tables_report()
        rep["ranking"][0]["workload_percent"] = 58.900000000000006
        html = renderers.render_decision_report_html(rep)
        assert "<td>58.9%</td>" in html
        assert "58.900000" not in html

    def test_deck_throughput_uses_thousands_separators(self) -> None:
        exp = _leaderboard_export()
        for p in exp["collector"]["queries"]["query_patterns"]:
            p["calls_per_second"] = 12.5
        f = pptx_report.derive(_cache_report(), exp)
        slide = pptx_report.slide_workload(pptx_report.open_deck(keep=1), f)
        assert any("1,337.5 queries/sec" in t for t in _shape_texts(slide))


class TestReviewFollowUps:
    """#267 review: singular resolved risk, unrated severities, colours, footnote."""

    def test_one_resolved_risk_is_singular(self) -> None:
        text = "3 risk(s) identified (overall: LOW; 1 resolved by the assignment)."
        assert renderers.label_resolved_risks(text) == (
            "3 open risk(s) (overall: LOW); 1 more was resolved by the assignment."
        )

    def test_risks_without_a_recognised_severity_are_shown_as_unrated(self) -> None:
        risks = [_risk("RISK-001", "SEVERE", 2), _risk("RISK-002", "", 5)]
        rows, caption = _risk_slide(risks)
        assert [r[0] for r in rows[1:]] == ["RISK-002", "RISK-001"]
        assert "No open risks remain" not in caption
        assert caption.startswith("No risk has a recognised severity; the table shows the top")
        f = pptx_report.derive({**_cache_report(), "risk_assessment": {"risks": risks}}, {})
        assert f["sev"] == {"UNRATED": 2}

    def test_severity_bar_uses_the_table_colour(self) -> None:
        f = pptx_report.derive(
            {**_cache_report(), "risk_assessment": {"risks": [_risk("RISK-001", "LOW", 1)]}}, {}
        )
        slide = pptx_report.slide_risk(pptx_report.open_deck(keep=1), f)
        fills = {
            str(sh.fill.fore_color.rgb)
            for sh in slide.shapes
            if sh.shape_type == 1 and sh.fill.type == 1 and sh.height < 300000
        }
        assert str(pptx_report._risk_accent("LOW")) in fills
        assert str(pptx_report.BLUE) not in fills

    def test_shared_tables_footnote_in_decision_report_and_deck(self) -> None:
        html = renderers.render_decision_report_html(_shared_tables_report())
        assert "including tables also served by another engine" in html
        f = pptx_report.derive(_shared_tables_report(), {})
        slide = pptx_report.slide_summary(pptx_report.open_deck(keep=1), f)
        assert any(
            "including tables also served by another engine" in t for t in _shape_texts(slide)
        )

    def test_no_footnote_without_shared_tables(self) -> None:
        rep = _shared_tables_report()
        rep["schema_designs"]["dynamodb"]["tables"] = []
        assert "also served by another engine" not in renderers.render_decision_report_html(rep)


class TestFmtNumEdges:
    def test_never_negative_zero(self) -> None:
        assert renderers.fmt_num(-0.0) == "0"
        assert renderers.fmt_num(-0.004) == ">-0.01"
        assert renderers.fmt_num(0.0) == "0"

    def test_small_positive_is_not_zero(self) -> None:
        assert renderers.fmt_num(0.004) == "<0.01"
        assert renderers.fmt_num(0.005) == "0.01"
        assert renderers.fmt_num(0.04, 1) == "<0.1"

    def test_non_finite(self) -> None:
        assert renderers.fmt_num(float("nan")) == "\u2013"
        assert renderers.fmt_num(float("inf")) == "\u2013"
        assert renderers.fmt_num(float("-inf")) == "\u2013"


# Real run-5 records: the description is "<type>: <mitigation>".
RUN5_RISK_001 = {
    "risk_id": "RISK-001",
    "severity": "MEDIUM",
    "risk_type": "MIGRATION_COMPLEXITY",
    "description": (
        "[dynamodb] aggregation: COUNT(*) on postmeta by post and meta_key: Query the "
        "META#<meta_key># prefix and count items in the application (or keep a counter). "
        "SUM over child posts' postmeta: compute in the application after Querying each "
        "child post, or maintain a materialized total via UpdateItem."
    ),
    "mitigation": (
        "COUNT(*) on postmeta by post and meta_key: Query the META#<meta_key># prefix and "
        "count items in the application (or keep a counter). SUM over child posts' postmeta: "
        "compute in the application after Querying each child post, or maintain a "
        "materialized total via UpdateItem."
    ),
    "query_ids": ["q1", "q2"],
}
RUN5_RISK_007 = {
    "risk_id": "RISK-007",
    "severity": "MEDIUM",
    "risk_type": "MIGRATION_COMPLEXITY",
    "description": (
        "[elasticache] unsupported pattern: Database metadata and session administration "
        "statements with no data to cache. None needed; remain with the relational database."
    ),
    "mitigation": "None needed; remain with the relational database.",
    "query_ids": ["q3"],
}
# The #268 shape: the description states the problem, the mitigation is separate.
SEPARATE_RISK = {
    "risk_id": "RISK-002",
    "severity": "MEDIUM",
    "risk_type": "MIGRATION_COMPLEXITY",
    "description": (
        "[dynamodb] COUNT of comments per post and approval status has no server-side "
        "equivalent in DynamoDB."
    ),
    "mitigation": "Maintain a counter per (post, status) via UpdateItem on write.",
    "query_ids": ["q4"],
}


class TestRiskRowSplitsDescriptionAndMitigation:
    """#249 review: descriptions shaped "<type>: <mitigation>" made the Mitigation
    column repeat the "What it is" text."""

    def _rows(self, *risks: dict[str, Any]) -> dict[str, list[str]]:
        rows, _ = _risk_slide(list(risks))
        return {r[0]: r for r in rows[1:]}

    def test_type_prefixed_description_shows_type_and_mitigation_apart(self) -> None:
        row = self._rows(RUN5_RISK_001)["RISK-001"]
        assert row[1] == "DynamoDB"
        assert row[2] == "Aggregation"
        assert row[3].startswith("COUNT(*) on postmeta by post and meta_key")
        assert "COUNT(*)" not in row[2]

    def test_mitigation_suffix_is_removed_from_what_it_is(self) -> None:
        row = self._rows(RUN5_RISK_007)["RISK-007"]
        assert row[2].startswith("Unsupported pattern: Database metadata and session")
        assert "None needed" not in row[2]
        assert row[3] == "None needed; remain with the relational database."

    def test_separate_mitigation_renders_both_columns(self) -> None:
        row = self._rows(SEPARATE_RISK)["RISK-002"]
        assert row[2].startswith("COUNT of comments per post and approval status")
        assert row[3] == "Maintain a counter per (post, status) via UpdateItem on write."

    def test_description_equal_to_mitigation_is_shown_once(self) -> None:
        risk = {
            **SEPARATE_RISK,
            "mitigation": "COUNT of comments per post and approval "
            "status has no server-side equivalent in DynamoDB.",
        }
        row = self._rows(risk)["RISK-002"]
        assert row[2].startswith("COUNT of comments")
        assert row[3] == "—"


def _new_summary_report() -> dict[str, Any]:
    """A report whose summary synthesis wrote with the labels already in (#255, #258)."""
    from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
    from src.agents.referee.synthesis_report import build_summary

    rep = _design_report()
    by_engine: dict[str, list[dict[str, Any]]] = {}
    for g in rep["query_groups"]:
        for ap in g["access_patterns"]:
            by_engine.setdefault(ap["engine"], [])
            if ap not in by_engine[ap["engine"]]:
                by_engine[ap["engine"]].append(ap)
    data = SynthesisData(
        job_id="j",
        database_name="wordpress",
        collector={
            "database_schema": {"tables": [{"table_id": f"wp.t{i}"} for i in range(50)]},
            "queries": {"query_patterns": [{"query_id": f"q{i}"} for i in range(107)]},
        },
    )
    data.engines = {
        e: EngineArtifacts(e, schema_design={"access_patterns": aps})
        for e, aps in by_engine.items()
    }
    ranking = [
        {"target": "dynamodb", "assigned_queries": 63, "workload_percent": 58.9,
         "schema_design_available": True, "target_tables": 15, "access_patterns": 51},
        {"target": "elasticache", "assigned_queries": 34, "workload_percent": 31.8,
         "schema_design_available": True, "target_tables": 12, "access_patterns": 13},
    ]  # fmt: skip
    risks = {"overall_risk_level": "LOW", "risks": [{}] * 8, "resolved_risks": [{}] * 4}
    rep["ranking"] = ranking
    rep["risk_assessment"] = {
        "overall_risk_level": "LOW",
        "risks": [_risk(f"RISK-00{i}", "MEDIUM", 1) for i in range(1, 9)],
        "resolved_risks": [{"engine": "dynamodb", "severity": "HIGH", "description": "x"}] * 4,
    }
    rep["summary_deterministic"] = build_summary(
        data, ranking, [], {"projected_monthly_cost": 0}, risks, rep["query_groups"]
    )
    return rep


class TestLabelsWrittenAtSource:
    """Synthesis writes the labels itself; the render-time relabelling of an older
    ``report.json`` must not label them a second time."""

    LABELS = (
        "dynamodb: 15 target tables, 47 in-scope access patterns",
        "elasticache: 12 key designs, 13 in-scope access patterns",
        "60 in-scope access patterns (plus 4 out of scope)",
        "8 open migration risks (overall: LOW); 4 more were resolved by the assignment.",
    )

    def test_synthesis_writes_the_labels(self) -> None:
        text = _new_summary_report()["summary_deterministic"]
        for label in self.LABELS:
            assert text.count(label) == 1, (label, text)

    def test_each_label_appears_once_in_every_deliverable(self) -> None:
        rep = _new_summary_report()
        html = renderers.render_decision_report_html(rep)
        f = pptx_report.derive(rep, {})
        slide = pptx_report.slide_summary(pptx_report.open_deck(keep=1), f)
        deck = " ".join(_shape_texts(slide))
        for label in self.LABELS:
            assert html.count(label) == 1, (label, "decision report")
            pretty = pptx_report.prettify_engines(label)
            assert deck.count(pretty) == 1, (pretty, deck)
        for text in (html, deck):
            assert "in-scope in-scope" not in text
            assert "open open" not in text
            assert "risk(s)" not in text
            assert "more more" not in text
