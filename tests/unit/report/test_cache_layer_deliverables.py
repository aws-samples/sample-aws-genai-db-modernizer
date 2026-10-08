"""The cache layer in the deliverables (#296).

ElastiCache owns no query: every deliverable shows it as "Cache layer" with the
hot reads it fronts and their share of calls, never as an owner share of the
workload. The deck keeps it as the no-migration Wave 1. Reports written before
the overlay (ElastiCache owning queries) render as before.
"""

from __future__ import annotations

from typing import Any

from ci.llm import judge_facts
from src.agents.referee.cache_overlay import safety_net_note
from src.report import pptx_report
from src.report.renderers import (
    _architecture_engines,
    architecture_svg,
    render_decision_report_html,
    render_engineering_report_md,
)


def _report() -> dict[str, Any]:
    return {
        "database_name": "wordpress",
        "job_id": "job-1",
        "timestamp": "2026-10-04T00:00:00Z",
        "ranking": [
            {
                "target": "dynamodb",
                "confidence_score": 50,
                "workload_percent": 80.4,
                "assigned_queries": 86,
            },
            {
                "target": "opensearch",
                "confidence_score": 60,
                "workload_percent": 19.6,
                "assigned_queries": 21,
            },
            {
                "target": "elasticache",
                "confidence_score": 48,
                "workload_percent": 0.0,
                "assigned_queries": 0,
                "role": "cache_layer",
                "cache_overlay_queries": 20,
                "cache_call_share_percent": 83.4,
                "cache_overlay_owners": {"dynamodb": 20},
            },
        ],
        "recommended_architecture": {
            "databases": [
                {"service": "dynamodb", "table_count": 19},
                {"service": "opensearch", "table_count": 2},
            ]
        },
        "schema_designs": {},
        "cache_overlay": {
            "engine": "elasticache",
            "query_count": 20,
            "calls_per_second": 185.07,
            "call_share_percent": 83.4,
            "owners": {"dynamodb": 20},
            "patterns": {"point_lookup": 19, "reference_read": 1},
            "min_calls_per_second": 1.0,
            "max_rows_avg": 100.0,
            "dropped_query_ids": ["q9"],
            # #459 review: the full audit trail (engineering report only) also
            # carries customer-edit/legacy-migration notes -- here it happens to
            # be only the safety-net note too, same text in both lists, as a real
            # run would record it in both.
            "notes": ["1 cached query had no in-scope access pattern ... owner unchanged)."],
            "safety_net_notes": [
                "1 cached query had no in-scope access pattern ... owner unchanged)."
            ],
        },
    }


def _cache_row(report: dict[str, Any]) -> dict[str, Any]:
    return next(e for e in _architecture_engines(report) if e["engine"] == "elasticache")


class TestDecisionReport:
    def test_cache_row_has_no_workload_share(self):
        row = _cache_row(_report())
        assert row["role"] == "Cache layer"
        assert row["workload"] is None
        assert (row["cached_queries"], row["cached_call_share"]) == (20, 83.4)
        assert row["scope"] == "cache-aside"

    def test_html_shows_cached_reads_and_call_share(self):
        html = render_decision_report_html(_report())
        assert "20 cached reads · 83.4% of calls" in html
        assert "fronts 20 hot reads (83.4% of calls) cache-aside" in html
        assert "<td>100%</td>" in html  # owner shares still add up

    def test_html_explains_a_safety_net_drop(self):
        # #424: the gate's cache_overlay and the report's disagreed with no
        # explanation. The decision report must carry the safety net's own
        # note (safety_net_notes), the same one the engineering report shows
        # (via the full notes list), so the two deliverables agree.
        html = render_decision_report_html(_report())
        assert "owner unchanged" in html

    def test_html_never_shows_the_full_notes_field(self):
        # #459 review: `notes` also carries customer-edit notes (full query
        # hashes) and the legacy-migration note -- neither customer-facing.
        rep = _report()
        rep["cache_overlay"]["safety_net_notes"] = []
        rep["cache_overlay"]["notes"] = ["query q_8f21c carried over, full hash attached"]
        html = render_decision_report_html(rep)
        assert "q_8f21c" not in html

    def test_html_escapes_a_hostile_safety_net_note(self):
        # #459: the note is server-generated, deterministic text -- but
        # escaping must not assume that. A `<script>`/`<b>&` payload must
        # render as literal text, never as markup, in the decision report.
        rep = _report()
        rep["cache_overlay"]["safety_net_notes"] = [
            "<script>alert(1)</script><b>bold</b> & escaped"
        ]
        html = render_decision_report_html(rep)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
        assert "&lt;b&gt;bold&lt;/b&gt; &amp; escaped" in html

    def test_svg(self):
        svg = architecture_svg(_report())
        assert "Cache layer  ·  20 reads · 83.4% of calls" in svg

    def test_legacy_report_keeps_the_workload_share(self):
        rep = _report()
        rep.pop("cache_overlay")
        legacy = {"target": "elasticache", "confidence_score": 48, "workload_percent": 31.8}
        rep["ranking"][2] = legacy
        row = _cache_row(rep)
        assert row["workload"] == 31.8 and row["cached_queries"] is None


class TestEngineeringReport:
    def test_cache_layer_section(self):
        md = render_engineering_report_md(_report())
        # #459 round 3: display_engine, not the raw engine id.
        assert "## Cache layer (ElastiCache)" in md
        assert "ElastiCache fronts 20 hot reads (83.4% of calls, 185.1 calls/s) cache-aside" in md
        assert "- Owner engines: dynamodb 20" in md
        assert "≥ 1 calls/s" in md and "≤ 100 rows" in md
        assert "owner unchanged" in md

    def test_md_escapes_a_hostile_note(self):
        # #459: markdown-escaped (escaping.md_text), not HTML-escaped -- the
        # engineering report is Markdown. A literal `<script>` and markdown
        # emphasis/link syntax must not survive into the rendered document.
        rep = _report()
        rep["cache_overlay"]["notes"] = ["<script>alert(1)</script> *bold* [x](y)"]
        md = render_engineering_report_md(rep)
        assert "<script>alert(1)</script>" not in md
        assert "<script>" not in md

    def test_no_section_without_overlay(self):
        rep = _report()
        rep.pop("cache_overlay")
        assert "## Cache layer" not in render_engineering_report_md(rep)

    def test_after_a_customer_edit_only_this_versions_note_is_current(self):
        """#459 round 3: ``notes`` is cumulative across versions (customer
        edits carry it forward, #459 round 1); ``safety_net_notes`` is reset
        on every new version (#459 round 2). A v3 rendered right after a
        customer edit must show only the v3 note as current -- the v2 note
        goes under "Earlier assignment versions", never presented as if it
        described v3's own numbers.
        """
        v2_note = (
            "20 hot reads (83.4% of calls) were assigned at the assignment gate. "
            "The ElastiCache schema design covers 10 of them; the other 10 are no "
            "longer cached and stay served by their owner engine. 10 hot reads "
            "(72.3% of calls) remain."
        )
        v3_note = (
            "11 hot reads (73.7% of calls) were assigned at the assignment gate. "
            "The ElastiCache schema design covers 9 of them; the other 2 are no "
            "longer cached and stay served by their owner engine. 9 hot reads "
            "(68.1% of calls) remain."
        )
        rep = _report()
        rep["cache_overlay"] = {
            **rep["cache_overlay"],
            "notes": [v2_note, v3_note],
            "safety_net_notes": [v3_note],
        }
        md = render_engineering_report_md(rep)
        current_section, _, rest = md.partition("### Earlier assignment versions")
        # Markdown-escaped parentheses (escaping.md_text): compare on each
        # note's own distinctive, non-overlapping number (not "20 hot reads"
        # or "83.4%", which the base fixture's own stat paragraph also uses).
        assert "73.7% of calls" in current_section and "68.1% of calls" in current_section
        assert "72.3% of calls" not in current_section
        assert "### Earlier assignment versions" in md
        assert "72.3% of calls" in rest
        assert "73.7% of calls" not in rest and "68.1% of calls" not in rest


def _shrink_report() -> dict[str, Any]:
    """Reproduces #424's own WordPress numbers: 20 hot reads (83.4% of
    calls) were assigned at the assignment gate; the post-schema-design
    safety net dropped 10, leaving 10 (61.3% of calls). The note is the real
    generator's output, not a hand-written stand-in, so these tests fail if
    the renderers stop reading it.

    Set on both ``notes`` (the full audit trail, engineering report only) and
    ``safety_net_notes`` (the customer-facing subset every other deliverable
    reads, #459) with the same text, as a real synthesis run would record it
    in both.
    """
    note = safety_net_note(
        "elasticache",
        [f"q{i}" for i in range(10)],
        before={"query_count": 20, "call_share_percent": 83.4},
        after={"query_count": 10, "call_share_percent": 61.3},
    )
    rep = _report()
    rep["ranking"][2]["cache_overlay_queries"] = 10
    rep["ranking"][2]["cache_call_share_percent"] = 61.3
    rep["cache_overlay"] = {
        **rep["cache_overlay"],
        "query_count": 10,
        "call_share_percent": 61.3,
        "dropped_query_ids": [f"q{i}" for i in range(10)],
        "notes": [note],
        "safety_net_notes": [note],
    }
    return rep


class TestCacheOverlayShrinkExplained:
    """#424: the cache layer shrinks between the gate and the report with no
    explanation. The fix is to carry the before/after numbers in the note the
    safety net already records, and to show that note in every deliverable
    that states the cache layer's final scope -- not only the engineering
    report, which already did.
    """

    def test_decision_report_states_before_and_after_numbers(self):
        html = render_decision_report_html(_shrink_report())
        assert "10 cached reads · 61.3% of calls" in html
        assert "20 hot reads (83.4% of calls) were assigned at the assignment gate" in html
        assert "ElastiCache schema design covers 10 of them" in html
        assert "10 hot reads (61.3% of calls) remain." in html

    def test_engineering_report_states_before_and_after_numbers(self):
        md = render_engineering_report_md(_shrink_report())
        assert "fronts 10 hot reads (61.3% of calls" in md
        # Markdown-escaped parentheses (escaping.md_text): still the same note.
        assert "20 hot reads \\(83.4% of calls\\) were assigned at the assignment gate" in md
        assert "10 hot reads \\(61.3% of calls\\) remain." in md


def _full_drop_report() -> dict[str, Any]:
    """#459 round 2: every cached read is dropped. ElastiCache then owns no
    workload and fronts nothing (``cache_overlay`` carries no ``engine`` key
    at all -- :func:`src.agents.referee.synthesis_report.overlay_summary`
    returns ``None`` with nothing cached), so it is absent from ``ranking``
    entirely (#296: the cache never owns a query, so a cache with nothing
    cached has no workload and no overlay to be listed by). The note is still
    the real generator's own output.
    """
    note = safety_net_note(
        "elasticache",
        ["q9"],
        before={"query_count": 1, "call_share_percent": 10.0},
        after={"query_count": 0, "call_share_percent": 0.0},
    )
    rep = _report()
    rep["ranking"] = [r for r in rep["ranking"] if r["target"] != "elasticache"]
    rep["cache_overlay"] = {
        "dropped_query_ids": ["q9"],
        "notes": [note],
        "safety_net_notes": [note],
    }
    return rep


class TestCacheOverlayFullDropExplained:
    """#459 round 2: when the safety net drops *every* cached read, the cache
    engine leaves the engine list entirely (it owns no workload, #296) --
    exactly #424's own repro. The note must still say so, in plain words
    ("none remain"), and the decision report must still show it even though
    there is no "Cache layer" row left to attach it to.
    """

    def test_note_says_none_remain(self):
        note = safety_net_note(
            "elasticache",
            ["q9"],
            before={"query_count": 1, "call_share_percent": 10.0},
            after={"query_count": 0, "call_share_percent": 0.0},
        )
        assert note == (
            "1 hot read (10.0% of calls) was assigned at the assignment gate. "
            "The ElastiCache schema design covers none of them; it is no "
            "longer cached and stays served by its owner engine. None remain."
        )

    def test_decision_report_still_shows_the_note(self):
        rep = _full_drop_report()
        assert not any(r["target"] == "elasticache" for r in rep["ranking"])
        html = render_decision_report_html(rep)
        assert "was assigned at the assignment gate" in html
        assert "None remain." in html

    def test_engineering_report_still_shows_the_note_with_no_engine_in_the_heading(self):
        # #459 round 3: a full drop clears `engine`, so the heading cannot
        # name it -- it used to hide the whole section (round 2's own fix
        # only applied to the decision/analysis reports and the Results
        # page); now it renders a plain "## Cache layer" heading plus the
        # note.
        md = render_engineering_report_md(_full_drop_report())
        assert "## Cache layer\n" in md
        assert "## Cache layer (" not in md
        assert "was assigned at the assignment gate" in md
        assert "None remain." in md


def _deck_text(rep: dict[str, Any]) -> str:
    f = pptx_report.derive(rep, {})
    prs = pptx_report.open_deck(keep=1)
    for build in pptx_report.SLIDES:
        build(prs, f)
    out = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                out.append(shape.text_frame.text)
            if shape.has_table:
                out.extend(c.text for r in shape.table.rows for c in r.cells)
    return " ".join(" ".join(out).split())


class TestDeck:
    def test_cache_layer_is_the_no_migration_wave_without_a_workload_share(self):
        f = pptx_report.derive(_report(), {})
        wave1 = f["waves"][0]
        assert wave1["engines"] == ["elasticache"]
        assert wave1["workload"] == 0
        assert (wave1["cached_queries"], wave1["cached_share"]) == (20, 83.4)
        assert wave1["no_migration"] is True
        start = next(d for d in f["decisions"] if d["question"].startswith("Add"))
        assert start["badge"] == "83% of calls cached"

    def test_deck_text(self):
        text = _deck_text(_report())
        assert (
            "Wave 1 puts the cache layer for 20 hot reads (83.4% of calls) in front of the "
            "current source database, with no data migration"
        ) in text
        # This fixture has no relational engine in ranking, so the source engine
        # can't be inferred and the sentence falls back to the generic phrasing
        # rather than naming a database.
        assert "cache-aside in front of the current source database" in text
        assert "Cache hit rate and invalidation verified against the source database" in text
        assert "ElastiCache caches 20 hot reads (83.4% of calls) and owns none of the workload" in (
            text
        )
        assert "ElastiCache cache layer · 20 cached reads · 83.4% of calls" in text
        assert "ElastiCache keeps" not in text
        assert "Wave 1 covers 0.0%" not in text
        assert "83.4% calls" in text


class TestJudgeFacts:
    def test_cache_layer_is_not_a_workload_share(self):
        assignment = {
            "query_assignments": [
                {
                    "query_id": "q1",
                    "assigned_engine": "dynamodb",
                    "source_tables": ["wp_options"],
                    "cache_engine": "elasticache",
                }
            ]
        }
        facts = judge_facts.build_facts(_report(), None, assignment)
        cache = next(e for e in facts["engines"] if e["engine"] == "elasticache")
        assert cache["role"] == "cache_layer"
        assert cache["workload_percent"] is None
        assert cache["cached_queries"] == 20
        assert cache["cached_call_share_percent"] == 83.4
        assert cache["tables_served"] == ["wp_options"]
        owner = next(e for e in facts["engines"] if e["engine"] == "dynamodb")
        assert "role" not in owner and owner["workload_percent"] == 80.4
        assert facts["cache_overlay"]["cached_queries"] == 20
        assert facts["cache_overlay"]["owners"] == {"dynamodb": 20}

    def test_no_overlay(self):
        rep = _report()
        rep.pop("cache_overlay")
        assert "cache_overlay" not in judge_facts.build_facts(rep)


class TestAnalysisReport:
    def test_cache_layer_stat_card(self):
        from src.report.analysis_report import _ASSIGNMENT_FIELDS, _cache_layer_stat

        card = _cache_layer_stat({"cache_overlay": _report()["cache_overlay"]})
        assert "Cache layer · 20 cached reads · 83.4% of calls" in card
        assert _cache_layer_stat({}) == ""
        assert {"cache_engine", "cache_reason"} <= set(_ASSIGNMENT_FIELDS)

    def test_engine_badges_names_the_cache_layer(self):
        # #358: render_analysis_report_html's "Target Engines" badges
        # (built from after_distribution, which never carries the cache
        # engine -- it owns no workload share, #296) must still name it,
        # the same way the WebApp's Results page and "Export to HTML"
        # button do.
        from src.report.analysis_report import _engine_badges

        after = {"dynamodb": 82.0, "aurora_mysql": 21.0, "opensearch": 4.0}
        badges = _engine_badges(after, {"engine": "elasticache"})
        assert 'data-engine="dynamodb">DynamoDB</span>' in badges
        assert 'data-engine="aurora_mysql">Aurora MySQL</span>' in badges
        assert 'data-engine="opensearch">OpenSearch</span>' in badges
        assert 'data-engine="elasticache">ElastiCache (cache layer)</span>' in badges

    def test_engine_badges_no_overlay_no_suffix(self):
        from src.report.analysis_report import _engine_badges

        badges = _engine_badges({"elasticache": 29.4, "dynamodb": 24.1}, None)
        assert 'data-engine="elasticache">ElastiCache</span>' in badges
        assert "cache layer" not in badges

    def test_rendered_report_names_the_cache_layer_and_sums_the_kept_rows(self):
        # Shaped like the real wordpress job e6a0127b's report.json (#358): three
        # owners (dynamodb, aurora_mysql, opensearch) plus elasticache fronting
        # them as a cache layer, no projected_monthly_cost field, so the total
        # falls back to summing the kept cost_breakdown rows.
        from src.report.analysis_report import render_analysis_report_html

        html = render_analysis_report_html(_export_data(_wordpress_shape()))
        assert 'data-engine="dynamodb">DynamoDB</span>' in html
        assert 'data-engine="aurora_mysql">Aurora MySQL</span>' in html
        assert 'data-engine="opensearch">OpenSearch</span>' in html
        assert 'data-engine="elasticache">ElastiCache (cache layer)</span>' in html
        # 98.41 + 318.80 + 240.96 + 165.55 (dynamodb + aurora_mysql + opensearch + elasticache)
        assert "823.72" in html

    def test_rendered_report_uses_the_reports_own_total_even_when_cost_breakdown_disagrees(self):
        # The report's own tco_analysis.projected_monthly_cost is the one source of
        # truth (matches the decision report and chat) -- never a sum recomputed
        # from cost_breakdown, which here is deliberately set to NOT add up to it.
        from src.report.analysis_report import render_analysis_report_html

        html = render_analysis_report_html(
            _export_data(_wordpress_shape(projected_monthly_cost=900.0))
        )
        assert "900.00" in html
        assert "823.72" not in html


def _wordpress_shape(projected_monthly_cost: float | None = None) -> dict[str, Any]:
    """Shaped like the real wordpress job e6a0127b's report.json (#358)."""
    report: dict[str, Any] = {
        "database_name": "wordpress",
        "job_id": "e6a0127b",
        "summary": "wordpress splits across three owners plus a cache layer.",
        "reality_check": {
            "after_distribution": {"dynamodb": 82.0, "aurora_mysql": 21.0, "opensearch": 4.0}
        },
        "cache_overlay": {
            "engine": "elasticache",
            "query_count": 14,
            "calls_per_second": 158.85,
            "call_share_percent": 71.5,
        },
        "tco_analysis": {
            "cost_breakdown": [
                {"database": "dynamodb", "monthly_cost_usd": 98.41, "pricing_mode": "on-demand"},
                {
                    "database": "elasticache",
                    "monthly_cost_usd": 165.55,
                    "pricing_mode": "on-demand",
                },
                {
                    "database": "opensearch",
                    "monthly_cost_usd": 240.96,
                    "pricing_mode": "on-demand",
                },
                {
                    "database": "aurora_mysql",
                    "monthly_cost_usd": 318.80,
                    "pricing_mode": "on-demand",
                },
            ]
        },
        "schema_designs": {},
        "recommended_architecture": {"databases": []},
    }
    if projected_monthly_cost is not None:
        report["tco_analysis"]["projected_monthly_cost"] = projected_monthly_cost
    return report


def _export_data(report: dict[str, Any]) -> dict[str, Any]:
    """The ``DATA`` shape ``render_analysis_report_html`` expects (#358)."""
    return {
        "results": {"job_id": report["job_id"], "status": "COMPLETED", "synthesis": report},
        "schemaDesigns": [],
        "collector": {},
        "jobId": report["job_id"],
        "exportDate": "2026-10-06T00:00:00Z",
        "queryJourneys": {"items": []},
    }


def _discourse_shape() -> dict[str, Any]:
    """The PR #304 discourse run: OpenSearch owns 3 text-search queries, has no tables
    or target objects ($240.96), and its schema design was skipped."""
    return {
        "database_name": "discourse",
        "job_id": "cc263d38",
        "timestamp": "2026-10-04T16:35:17Z",
        "ranking": [
            {
                "target": "aurora_postgresql",
                "confidence_score": 67,
                "workload_percent": 76.7,
                "assigned_queries": 1268,
            },
            {
                "target": "dynamodb",
                "confidence_score": 60,
                "workload_percent": 23.1,
                "assigned_queries": 383,
            },
            {
                "target": "opensearch",
                "confidence_score": 2,
                "workload_percent": 0.2,
                "assigned_queries": 3,
            },
            {
                "target": "elasticache",
                "confidence_score": 56,
                "workload_percent": 0.0,
                "assigned_queries": 0,
                "role": "cache_layer",
                "cache_overlay_queries": 3,
                "cache_call_share_percent": 25.1,
            },
        ],
        "recommended_architecture": {
            "databases": [
                {"service": "aurora_postgresql", "table_count": 158},
                {"service": "dynamodb", "table_count": 115},
            ]
        },
        "schema_designs": {
            "aurora_postgresql": {"status": "completed", "tables": [{}] * 158},
            "dynamodb": {"status": "completed", "tables": [{}] * 117},
            "opensearch": {"status": "skipped"},
            "elasticache": {"status": "completed", "tables": [{}] * 3},
        },
        "tco_analysis": {
            "cost_breakdown": [
                {"database": "aurora_postgresql", "monthly_cost_usd": 121.06},
                {"database": "dynamodb", "monthly_cost_usd": 15.20},
                {"database": "opensearch", "monthly_cost_usd": 240.96},
                {"database": "elasticache", "monthly_cost_usd": 165.10},
            ]
        },
        "cache_overlay": {"engine": "elasticache", "query_count": 3, "call_share_percent": 25.1},
    }


class TestSearchReadModel:
    def test_opensearch_without_tables_is_a_read_model_not_retained(self):
        rows = {e["engine"]: e for e in _architecture_engines(_discourse_shape())}
        assert rows["opensearch"]["role"] == "Search read model"
        assert rows["opensearch"]["scope"] == "synced from owners"
        assert rows["opensearch"]["migrates"] == 0

    def test_decision_report_never_calls_it_the_relational_core(self):
        html = render_decision_report_html(_discourse_shape())
        assert "retained as the relational core" not in html
        assert "<b>OpenSearch</b> is a search read model" in html
        assert "source schema retained" not in html

    def test_wave_plan_puts_it_after_its_owners_never_in_the_no_migration_wave(self):
        f = pptx_report.derive(_discourse_shape(), {})
        waves = [w["engines"] for w in f["waves"]]
        # #321: the relational move (aurora_postgresql) comes first now; the
        # search read model still comes last among the rest, after the
        # engines that own its tables, never in the no-migration (cache) wave.
        assert waves[0] == ["aurora_postgresql"]
        assert waves[1] == ["elasticache"]
        assert waves[-1] == ["opensearch"]
        opensearch_wave = next(w for w in f["waves"] if w["engines"] == ["opensearch"])
        assert opensearch_wave is f["waves"][-1]
        assert all("opensearch" not in w for w in waves[:-1])
        # This fixture has no table_mappings, so the indexed tables cannot be
        # resolved -- #225's fallback: say so, and fall back to the
        # retained engine as the owner of record rather than showing no owner.
        assert "could not be resolved" in opensearch_wave["note"]
        assert "Aurora PostgreSQL" in opensearch_wave["note"]
        start = next(d for d in f["decisions"] if d["question"].startswith("Add"))
        assert "OpenSearch" not in start["against"]

    def test_retained_is_only_the_source_compatible_relational_engine(self):
        rep = _discourse_shape()
        rep["recommended_architecture"]["databases"] = [{"service": "dynamodb", "table_count": 115}]
        rep["schema_designs"]["aurora_postgresql"] = {"status": "skipped"}
        rows = {e["engine"]: e["role"] for e in _architecture_engines(rep)}
        assert rows["aurora_postgresql"] == "Retained"
        assert rows["opensearch"] == "Search read model"

    def test_decision_report_cache_note_fronts_aurora_once_retained_exists(self):
        # #321: the generic "Recommended architecture" cache note now names
        # the retained Aurora engine it fronts, not the legacy source.
        html = render_decision_report_html(_discourse_shape())
        assert "It fronts Aurora PostgreSQL with no data migration of its own" in html
        assert "invalidation follows the engine that owns each cached table" in html
