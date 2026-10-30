# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Issue #530: the deck's "Basis of the analysis" bullet said "0 tables mapped
to a named target" on a run with no schema design, while slide 2's "Tables
migrate" tile (and the migration roadmap) already said 8 tables migrate --
both read from the same ``report.json``, on the same deck.

The bullet read ``len(report["table_mappings"])``, which only a completed
schema design ever populates. ``architecture._n_tables_mapped`` now prefers
``source_table_count`` (``SynthesisOutputContract``, #530 review round 3):
the collected schema's own tables-only total, as a structured field, when no
design has completed. A report synthesized before that field existed falls
back to parsing the same number out of the deck's "Analyzed N source
tables" line (``summary_deterministic``), and only then to the migration
roadmap's own per-wave table lists (``resolve_migration_waves``). It reports
whether the figure came from a design so the deck can avoid claiming a
per-table fit score that does not exist without one, and
the retired deck's "Basis of the analysis" bullet used it.

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

from src.report.architecture import _n_tables_mapped

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_no_schema_design_report.json"
DESIGNED_FIXTURE = Path(__file__).parent / "fixtures" / "e2e09_report.json"


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
