"""Issue #530: the deck's "Basis of the analysis" bullet said "0 tables mapped
to a named target" on a run with no schema design, while slide 2's "Tables
migrate" tile (and the migration roadmap) already said 8 tables migrate --
both read from the same ``report.json``, on the same deck.

The bullet read ``len(report["table_mappings"])``, which only a completed
schema design ever populates. ``renderers._n_tables_mapped`` now prefers
``source_table_count`` (``SynthesisOutputContract``, #530 review round 3):
the collected schema's own tables-only total, as a structured field, when no
design has completed. A report synthesized before that field existed falls
back to parsing the same number out of the deck's "Analyzed N source
tables" line (``summary_deterministic``), and only then to the migration
roadmap's own per-wave table lists (``resolve_migration_waves``). It reports
whether the figure came from a design so the deck can avoid claiming a
per-table fit score that does not exist without one, and
``pptx_report.derive()`` builds the "Basis of the analysis" bullet (and
slide 2's footer) to match.

Round 2 (review of #532):
- The migration roadmap's own table lists mix in views -- wave 1 carries
  "every collected table and view" (#321) -- so summing/unioning them can
  overcount a view against the "Analyzed N source tables" line (discourse's
  ``badge_posts`` would have inflated 311 to 312). Preferring the parsed
  summary line avoids this; a small synthetic discourse-shaped report below
  covers it directly.
- Slide 2's footer ("N of M mapped source tables") also read ``n_tables``
  without a design and must not call it "mapped" either.
- The bullet now explains the wave relationship by name instead of a bare
  count, and the no-waves/no-mappings case says what is actually known
  instead of printing a fabricated "0 tables assigned".

Round 3 (review of #532): ``source_table_count`` promoted the parsed-summary
figure to a real contract field (``src.agents.referee.synthesis_report.
source_table_count``, ``tests/unit/agents/referee/
test_synthesis_source_table_count.py`` proves it matches ``build_summary``'s
own text); the regex is now a fallback for a report written before it
existed, same as the wordpress fixture below.

Fixtures:
- ``wordpress_no_schema_design_report.json`` (#528): the real synthesis
  report from ``uv run python scripts/run_assessment.py --all -y
  --llm-mode none`` on the bundled wordpress sample -- no engine has a
  completed schema design.
- ``e2e09_report.json``: the real, schema-designed discourse run already
  used by ``test_report_renderers.py`` and
  ``test_engine_table_counts_without_schema_design.py``, reused here for the
  completed-design guard.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.report import pptx_report
from src.report.pptx_report import _estimated_text_height_in
from src.report.renderers import _n_tables_mapped, clip

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_no_schema_design_report.json"
DESIGNED_FIXTURE = Path(__file__).parent / "fixtures" / "e2e09_report.json"

# The "Basis of the analysis" card's own text box (slide_evidence): 5.05in
# wide, 1.85in tall, holding a bold title line plus three bullets.
_BASIS_BOX_W = 5.05
_BASIS_BOX_H = 1.85
_BASIS_TITLE_H = _estimated_text_height_in("Basis of the analysis", 11.0, _BASIS_BOX_W)


@pytest.fixture(scope="module")
def report() -> dict[str, Any]:
    data: dict = json.loads(FIXTURE.read_text())
    return data


@pytest.fixture(scope="module")
def designed_report() -> dict[str, Any]:
    data: dict = json.loads(DESIGNED_FIXTURE.read_text())
    return data


def _discourse_like_report_with_a_view() -> dict[str, Any]:
    """A small synthetic report shaped like the real discourse run, but with
    no schema design: wave 1 (the whole collected schema, Aurora PostgreSQL)
    carries 311 real tables plus one view, ``discourse.badge_posts``; wave 2
    moves 8 of those tables on to DynamoDB. The executive summary -- the one
    place a tables-only total already exists -- says 311, not 312.
    """
    return {
        "database_name": "discourse",
        "job_id": "job-view",
        "timestamp": "2026-10-12T00:00:00Z",
        "ranking": [
            {
                "target": "aurora_postgresql",
                "confidence_score": 60,
                "workload_percent": 70.0,
                "assigned_queries": 1100,
            },
            {
                "target": "dynamodb",
                "confidence_score": 55,
                "workload_percent": 30.0,
                "assigned_queries": 400,
            },
        ],
        "recommended_architecture": {"databases": [{"service": "dynamodb", "table_count": 0}]},
        "schema_designs": {},
        "table_mappings": [],
        "summary_deterministic": (
            "Analyzed 311 source tables and 1500 query patterns across 2 target database(s)."
        ),
        "migration_waves": [
            {
                "wave": 1,
                "title": "Move to Aurora PostgreSQL",
                "engines": ["aurora_postgresql"],
                "moves_from": ["postgresql"],
                "homogeneity": "homogeneous",
                "tables": [f"discourse.t{i}" for i in range(311)] + ["discourse.badge_posts"],
                "table_count": 312,
                "query_count": 1100,
                "workload_share_percent": 70.0,
                "share_basis": "queries",
                "cutover_query_count": 1500,
            },
            {
                "wave": 2,
                "title": "Move key-value and point-lookup queries to DynamoDB",
                "engines": ["dynamodb"],
                "moves_from": ["aurora_postgresql"],
                "homogeneity": None,
                "tables": [f"discourse.t{i}" for i in range(8)],
                "table_count": 8,
                "query_count": 400,
                "workload_share_percent": 30.0,
                "share_basis": "queries",
            },
        ],
    }


def _nothing_known_report() -> dict[str, Any]:
    """No design, no migration roadmap, no parseable summary: truly nothing
    to report a table count from."""
    return {
        "database_name": "empty",
        "job_id": "job-empty",
        "timestamp": "2026-10-12T00:00:00Z",
        "ranking": [],
        "recommended_architecture": {"databases": []},
        "schema_designs": {},
        "table_mappings": [],
        "summary_deterministic": "",
        "migration_waves": [],
    }


def _all_text(prs) -> str:
    out = []
    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                out.append(shape.text_frame.text)
    return "\n".join(out)


def _deck_text(rep: dict[str, Any]) -> str:
    f = pptx_report.derive(rep, {})
    prs = pptx_report.open_deck(keep=1)
    for build in pptx_report.SLIDES:
        build(prs, f)
    return _all_text(prs)


def test_fixture_reproduces_the_defect_shape(report: dict[str, Any]) -> None:
    """Sanity check: no schema design ran, so ``table_mappings`` is empty while
    the migration roadmap already names 8 real tables moving to DynamoDB."""
    assert report.get("table_mappings") == []
    dynamodb_wave = next(w for w in report["migration_waves"] if w["engines"] == ["dynamodb"])
    assert dynamodb_wave["table_count"] == 8


class TestNTablesMapped:
    def test_prefers_source_table_count_when_present(self, report: dict[str, Any]) -> None:
        # Rigged to disagree with the summary's own "Analyzed 50 ..." line,
        # to prove source_table_count wins when both are present -- the
        # real-world case is simpler (synthesis fills both from the same
        # source_table_count() helper, so they always agree).
        rigged = {**report, "source_table_count": 999}
        assert _n_tables_mapped(rigged) == (999, False)

    def test_falls_back_to_the_analyzed_source_tables_line_without_the_field(
        self, report: dict[str, Any]
    ) -> None:
        # The wordpress fixture predates ``source_table_count`` (#530 round
        # 3), so this exercises the regex fallback: its own
        # ``summary_deterministic`` already says "Analyzed 50 source
        # tables ...".
        assert "source_table_count" not in report
        assert "Analyzed 50 source tables" in report["summary_deterministic"]
        n_tables, from_design = _n_tables_mapped(report)
        assert (n_tables, from_design) == (50, False)

    def test_prefers_a_completed_design(self, designed_report: dict[str, Any]) -> None:
        n_tables, from_design = _n_tables_mapped(designed_report)
        assert n_tables == len(designed_report["table_mappings"]) == 92
        assert from_design is True

    def test_does_not_count_a_view_the_summary_excludes(self) -> None:
        # #530 round 2: the migration roadmap's wave 1 carries the whole
        # collected schema, views included (311 tables + 1 view = 312); the
        # executive summary's own tables-only total is 311. Must read 311,
        # not the view-inflated 312 a naive union of wave table lists gives.
        rep = _discourse_like_report_with_a_view()
        wave_1_tables = rep["migration_waves"][0]["tables"]
        assert len(wave_1_tables) == 312
        assert "discourse.badge_posts" in wave_1_tables
        n_tables, from_design = _n_tables_mapped(rep)
        assert (n_tables, from_design) == (311, False)

    def test_falls_back_to_the_wave_union_without_a_parseable_summary(
        self, report: dict[str, Any]
    ) -> None:
        # Last resort only (no completed design, no source_table_count, no
        # "Analyzed N source tables" line to read): the union of every
        # wave's own table list. The wordpress fixture has no views, so the
        # union still lands on 50 -- not 58, which summing wave 1 (50, the
        # whole schema) and wave 3 (8, a subset of it) would double count to.
        rigged = {**report, "summary_deterministic": "", "summary": ""}
        n_tables, from_design = _n_tables_mapped(rigged)
        assert (n_tables, from_design) == (50, False)

    def test_nothing_known_is_zero(self) -> None:
        n_tables, from_design = _n_tables_mapped(_nothing_known_report())
        assert (n_tables, from_design) == (0, False)


class TestBasisBullet:
    def test_matches_the_coordinator_shaped_sentence(self, report: dict[str, Any]) -> None:
        f = pptx_report.derive(report, {})
        assert f["n_tables"] == 50
        assert f["schema_mapped"] is False
        assert f["migrated"] == 8
        # #530 round 3 review: the disclaimer leads, so a defensive clip can
        # never cut it off -- only the wave detail after it is at risk.
        assert f["basis_table_line"] == (
            "Schema mapping has not run, so there are no per-table fit scores "
            "yet; all 50 source tables are placed: the whole schema lands on Aurora "
            "MySQL in wave 1, then 8 move on to DynamoDB"
        )
        assert f["basis_table_line"].startswith("Schema mapping has not run")

    def test_text_appears_on_the_deck_and_fits_its_card(self, report: dict[str, Any]) -> None:
        f = pptx_report.derive(report, {})
        text = " ".join(_deck_text(report).split())
        assert f["basis_table_line"] in text
        # The card's title line plus its three bullets must fit the 1.85in
        # text box (same risk ``_wave_card_text`` guards for the Migration
        # Sequencing card): estimate each bullet's wrapped height at the
        # same width/size slide_evidence renders them at.
        bullets = [
            "107 of 107 query patterns in scope",
            f["basis_table_line"],
            "1 co-dependency group — table sets that must move together",
        ]
        total_h = _BASIS_TITLE_H + sum(
            _estimated_text_height_in(b, 11.5, _BASIS_BOX_W) for b in bullets
        )
        assert total_h < _BASIS_BOX_H

    def test_discourse_shaped_text_also_fits_its_card(self) -> None:
        rep = _discourse_like_report_with_a_view()
        f = pptx_report.derive(rep, {})
        assert f["n_tables"] == 311
        assert f["basis_table_line"] == (
            "Schema mapping has not run, so there are no per-table fit scores "
            "yet; all 311 source tables are placed: the whole schema lands on Aurora "
            "PostgreSQL in wave 1, then 8 move on to DynamoDB"
        )
        total_h = _BASIS_TITLE_H + _estimated_text_height_in(
            f["basis_table_line"], 11.5, _BASIS_BOX_W
        )
        assert total_h < _BASIS_BOX_H

    def test_with_a_completed_design_is_unchanged(self, designed_report: dict[str, Any]) -> None:
        f = pptx_report.derive(designed_report, {})
        assert f["basis_table_line"] == (
            "92 tables mapped to a named target, each with its own fit score"
        )
        text = " ".join(_deck_text(designed_report).split())
        assert f["basis_table_line"] in text
        assert "schema mapping has not run" not in text

    def test_nothing_known_says_so_without_a_fabricated_zero(self) -> None:
        f = pptx_report.derive(_nothing_known_report(), {})
        assert f["n_tables"] == 0
        assert f["basis_table_line"] == "No source tables are mapped or assigned to a target yet"
        assert "0 tables" not in f["basis_table_line"]
        assert "0 source" not in f["basis_table_line"]

    def test_does_not_name_the_roadmap_when_there_are_no_waves(self) -> None:
        # No ranking at all, so resolve_migration_waves derives nothing --
        # only source_table_count says anything is known.
        rep = {
            "database_name": "odd",
            "job_id": "job-no-waves",
            "timestamp": "2026-10-12T00:00:00Z",
            "ranking": [],
            "recommended_architecture": {"databases": []},
            "schema_designs": {},
            "table_mappings": [],
            "migration_waves": [],
            "source_table_count": 25,
        }
        f = pptx_report.derive(rep, {})
        assert f["n_tables"] == 25
        assert f["basis_table_line"] == (
            "Schema mapping has not run, so there are no per-table fit scores "
            "yet; 25 source tables are assigned to a target engine"
        )
        assert "migration roadmap" not in f["basis_table_line"]
        assert "wave 1" not in f["basis_table_line"]

    def test_names_the_roadmap_when_waves_exist_with_no_single_base_engine(self) -> None:
        # A key-value wave with no retained/homogeneous wave 1 ahead of it
        # (no source engine to move from): there is a roadmap, just no one
        # wave covering the whole schema to name.
        rep = {
            "database_name": "kv-only",
            "job_id": "job-kv-only",
            "timestamp": "2026-10-12T00:00:00Z",
            "ranking": [
                {
                    "target": "dynamodb",
                    "confidence_score": 70,
                    "workload_percent": 100.0,
                    "assigned_queries": 50,
                }
            ],
            "recommended_architecture": {"databases": [{"service": "dynamodb", "table_count": 0}]},
            "schema_designs": {},
            "table_mappings": [],
            "migration_waves": [],
            "source_table_count": 12,
        }
        f = pptx_report.derive(rep, {})
        assert f["migrated"] == 0
        assert f["basis_table_line"] == (
            "Schema mapping has not run, so there are no per-table fit scores "
            "yet; 12 source tables are assigned across the migration roadmap"
        )

    def test_comma_formats_counts_of_a_thousand_or_more(self) -> None:
        rep = _discourse_like_report_with_a_view()
        rep["source_table_count"] = 1234
        rep["summary_deterministic"] = (
            "Analyzed 1234 source tables and 1500 query patterns across 2 target " "database(s)."
        )
        rep["migration_waves"][1]["table_count"] = 1000
        rep["migration_waves"][1]["tables"] = [f"d.t{i}" for i in range(1000)]
        f = pptx_report.derive(rep, {})
        assert f["n_tables"] == 1234
        assert f["migrated"] == 1000
        assert "1,234 source tables" in f["basis_table_line"]
        assert "then 1,000 move on to" in f["basis_table_line"]

    def test_clip_cannot_cut_the_disclaimer_even_if_the_detail_overflows(self) -> None:
        # Structural guarantee, independent of whether real engine-name data
        # can ever actually reach it (#530 round 3 review): the disclaimer
        # leads the sentence, so clip(line, 220) can only ever truncate what
        # comes after the "; ", never the disclaimer itself.
        disclaimer = "Schema mapping has not run, so there are no per-table fit scores yet"
        overlong_detail = "all 999,999 source tables are placed across " + ("Engine, " * 40)
        line = clip(f"{disclaimer}; {overlong_detail}", 220)
        assert line.startswith(disclaimer)


class TestSlideTwoFooter:
    """Slide 2's footer ("N of M mapped source tables move to a purpose-built
    engine") reads the same ``n_tables``/``schema_mapped`` pair."""

    def test_does_not_say_mapped_without_a_schema_design(self, report: dict[str, Any]) -> None:
        text = " ".join(_deck_text(report).split())
        assert "8 of the 50 mapped source tables" not in text
        assert "8 of the 50 source tables move to a purpose-built engine" in text

    def test_still_says_mapped_with_a_completed_design(
        self, designed_report: dict[str, Any]
    ) -> None:
        text = " ".join(_deck_text(designed_report).split())
        assert "90 of the 92 mapped source tables" in text
