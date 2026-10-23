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
from src.report.architecture import _architecture_engines
from src.report.renderers import render_engineering_report_md


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
            "candidate_query_count": 21,
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


class TestCacheRow:
    def test_cache_row_has_no_workload_share(self):
        row = _cache_row(_report())
        assert row["role"] == "Cache layer"
        assert row["workload"] is None
        assert (row["cached_queries"], row["cached_call_share"]) == (20, 83.4)
        assert row["scope"] == "cache-aside"

    def test_engine_table_shows_cached_reads_and_call_share(self):
        md = render_engineering_report_md(_report())
        assert (
            "| ElastiCache | Cache layer | 20 designed cached reads · 83.4% of calls | cache-aside |"
            in md
        )
        total = next(x for x in md.splitlines() if x.startswith("| Total |"))
        assert "| 100% |" in total  # owner shares still add up

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
        assert (
            "ElastiCache fronts 20 designed cached reads (83.4% of calls, 185.1 calls/s) cache-aside"
            in md
        )
        # #572: display_engine here too, same as the heading and fronts line above.
        assert "- Owner engines: DynamoDB 20" in md
        assert "≥ 1 calls/s" in md and "≤ 100 rows" in md
        assert "21 cache candidates, 20 designed; it stays on its owner engine." in md

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
            "query_count": 9,
            "candidate_query_count": 11,
            "notes": [v2_note, v3_note],
            "safety_net_notes": [v3_note],
        }
        md = render_engineering_report_md(rep)
        current_section, _, rest = md.partition("### Earlier assignment versions")
        # #1252: the current explanation is the facts' sentence for this version's own
        # numbers; the v2 note stays an earlier version's, and the v3 note's old wording is
        # not repeated beside it.
        assert "11 cache candidates, 9 designed; 2 stay on their owner engine." in current_section
        assert "72.3% of calls" not in current_section
        assert "### Earlier assignment versions" in md
        assert "72.3% of calls" in rest
        assert "73.7% of calls" not in md


def _shrink_report() -> dict[str, Any]:
    """Reproduces #424's and #1252's WordPress numbers: 20 cache candidates (83.4% of
    calls) at the assignment gate; the post-schema-design safety net dropped 6, leaving
    14 designed cached reads (76.1% of calls). The note is the real
    generator's output, not a hand-written stand-in, so these tests fail if
    the renderers stop reading it.

    Set on both ``notes`` (the full audit trail, engineering report only) and
    ``safety_net_notes`` (the customer-facing subset every other deliverable
    reads, #459) with the same text, as a real synthesis run would record it
    in both.
    """
    note = safety_net_note(
        "elasticache",
        [f"q{i}" for i in range(6)],
        before={"query_count": 20, "call_share_percent": 83.4},
        after={"query_count": 14, "call_share_percent": 76.1},
    )
    rep = _report()
    rep["ranking"][2]["cache_overlay_queries"] = 14
    rep["ranking"][2]["cache_call_share_percent"] = 76.1
    rep["cache_overlay"] = {
        **rep["cache_overlay"],
        "query_count": 14,
        "call_share_percent": 76.1,
        "candidate_query_count": 20,
        "candidate_call_share_percent": 83.4,
        "dropped_query_ids": [f"q{i}" for i in range(6)],
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

    def test_engineering_report_states_before_and_after_numbers(self):
        md = render_engineering_report_md(_shrink_report())
        assert "| 14 designed cached reads · 76.1% of calls |" in md
        assert "fronts 14 designed cached reads (76.1% of calls" in md
        # #1252: the change is one short sentence with both labelled numbers, once.
        sentence = "20 cache candidates, 14 designed; 6 stay on their owner engine."
        assert md.count(sentence) == 1


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
    ("none remain"), and the engineering report must still show it even though
    there is no "Cache layer" row left to attach it to.
    """

    def test_note_says_none_remain(self):
        note = safety_net_note(
            "elasticache",
            ["q9"],
            before={"query_count": 1, "call_share_percent": 10.0},
            after={"query_count": 0, "call_share_percent": 0.0},
        )
        assert note == "1 cache candidate, none designed; it stays on its owner engine."

    def test_engineering_report_still_shows_the_note_with_no_engine_in_the_heading(self):
        # #459 round 3: a full drop clears `engine`, so the heading cannot
        # name it -- it used to hide the whole section; now it renders a plain
        # "## Cache layer" heading plus the note.
        rep = _full_drop_report()
        assert not any(r["target"] == "elasticache" for r in rep["ranking"])
        md = render_engineering_report_md(rep)
        assert "## Cache layer\n" in md
        assert "## Cache layer (" not in md
        assert "1 cache candidate, none designed; it stays on its owner engine." in md


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

    def test_engineering_report_never_calls_it_the_relational_core(self):
        md = render_engineering_report_md(_discourse_shape()).replace("**", "")
        assert "OpenSearch is retained as the relational core" not in md
        assert "OpenSearch is a search read model" in md
        assert "| OpenSearch | Search read model |" in md

    def test_retained_is_only_the_source_compatible_relational_engine(self):
        rep = _discourse_shape()
        rep["recommended_architecture"]["databases"] = [{"service": "dynamodb", "table_count": 115}]
        rep["schema_designs"]["aurora_postgresql"] = {"status": "skipped"}
        rows = {e["engine"]: e["role"] for e in _architecture_engines(rep)}
        assert rows["aurora_postgresql"] == "Retained"
        assert rows["opensearch"] == "Search read model"

    def test_engineering_report_cache_note_fronts_aurora_once_retained_exists(self):
        # #321: the cache note names the retained Aurora engine it fronts, not
        # the legacy source.
        md = render_engineering_report_md(_discourse_shape())
        assert "It fronts Aurora PostgreSQL with no data migration of its own" in md
        assert "invalidation follows the engine that owns each cached table" in md


def test_engineering_report_read_shapes_use_the_pattern_labels() -> None:
    """#679 review: the Read shapes line used ``top_n`` -> "top n"; it uses the
    overlay's own labels ("top-N lookup", "session/token lookup")."""
    rep = _report()
    rep["cache_overlay"]["patterns"] = {
        "point_lookup": 13,
        "reference_read": 2,
        "session_lookup": 1,
        "top_n": 1,
    }
    md = render_engineering_report_md(rep)
    line = next(x for x in md.splitlines() if x.startswith("- Read shapes:"))
    assert line == (
        "- Read shapes: point lookup 13, small reference read 2, session/token lookup 1, "
        "top-N lookup 1"
    )
