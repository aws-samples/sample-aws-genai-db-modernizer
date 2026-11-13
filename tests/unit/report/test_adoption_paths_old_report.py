# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A report that states no adoption path renders exactly as before #1131.

ADR-032 Decision 4: a report written before the slice plan, or rebuilt without the
assignment, states the empty plan and no path. Its engineering report must not change
at all: no "Adoption paths", no stages, and the roadmap's old one-step pointer. The
golden files were rendered by main's code before #1131 (``engineering_report_no_adoption*.golden.txt``,
stored as text so no Markdown linter rewrites them),
and this module imports nothing #1131 added, so it runs on main too.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.report.renderers import render_engineering_report_md
from src.shared.report_facts import compute_report_facts
from tests.unit.agents.referee import slice_plan_jobs as job

GOLDEN = Path(__file__).parent / "fixtures" / "engineering_report_no_adoption.golden.txt"


def report_without_adoption_paths() -> dict[str, Any]:
    """The hand-built slice-plan job as a report, its facts computed with no slice inputs:
    the empty plan, so no adoption path."""
    report: dict[str, Any] = {
        "database_name": "app",
        "job_id": "job-1",
        "timestamp": "2026-11-13T00:00:00Z",
        "engines_in_use": ["mysql"],
        "ranking": [
            {"target": "aurora_mysql", "confidence_score": 90, "assigned_queries": 3},
            {"target": "dynamodb", "confidence_score": 88, "assigned_queries": 8},
            {"target": "documentdb", "confidence_score": 80, "assigned_queries": 2},
            {
                "target": "elasticache",
                "confidence_score": 70,
                "assigned_queries": 0,
                "role": "cache_layer",
                "cache_overlay_queries": 2,
            },
        ],
        "recommended_architecture": {"databases": [{"service": "dynamodb", "table_count": 4}]},
        "schema_designs": {},
        "migration_waves": job.waves(),
        "tco_analysis": {
            "projected_monthly_cost": sum(job.COSTS.values()),
            "cost_breakdown": [
                {"database": engine, "monthly_cost_usd": cost} for engine, cost in job.COSTS.items()
            ],
        },
    }
    report["facts"] = compute_report_facts(report, waves=job.waves(), readiness=False)
    return report


@pytest.mark.passes_on_main  # main rendered the golden; the guard is that #1131 keeps it
def test_a_report_with_no_adoption_path_renders_byte_for_byte_as_before() -> None:
    report = report_without_adoption_paths()
    assert report["facts"]["adoption"]["paths"] == []
    assert render_engineering_report_md(report) == GOLDEN.read_text(encoding="utf-8")


@pytest.mark.passes_on_main
def test_a_report_with_no_facts_block_renders_byte_for_byte_as_before() -> None:
    report = report_without_adoption_paths()
    del report["facts"]
    golden = GOLDEN.with_name("engineering_report_no_adoption_no_facts.golden.txt")
    assert render_engineering_report_md(report) == golden.read_text(encoding="utf-8")
