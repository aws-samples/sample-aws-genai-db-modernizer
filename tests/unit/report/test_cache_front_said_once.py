# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The cache's staged front is said once in each deliverable (#2171, review).

"cache-aside in front of Aurora MySQL in wave 2, then DynamoDB once wave 3 moves these
reads" is stored in the cache layer's "Why" cell and stated by the wave 2 rationale. The
executive PDF and the engineering report print both, so their "Why" cell refers to the
wave plan. The chat summary prints the cell and no wave plan, so it keeps the front.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.report.architecture import _architecture_engines
from src.report.executive_report import render_executive_report_pdf
from src.report.printed_summary import render_final_summary
from src.report.renderers import render_engineering_report_md
from src.shared.migration_wave_engines import CACHE_FRONT_REFERENCE
from tests.executive_pdf import normalize, section_texts

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_model_run_1_60_report.json"
STAGED = "Aurora MySQL in wave 2, then DynamoDB once wave 3 moves these reads"


@pytest.fixture
def report() -> dict[str, Any]:
    saved: dict[str, Any] = json.loads(FIXTURE.read_text())
    return copy.deepcopy(saved)


def _final(report: dict[str, Any]) -> str:
    return render_final_summary(
        report,
        database_name="wordpress",
        job_id="job1",
        deliverables_dir="artifacts/wordpress/job1/deliverables/",
        deliverables=[],
    )


def _cache_row(report: dict[str, Any]) -> dict[str, Any]:
    return next(e for e in _architecture_engines(report) if e["role"] == "Cache layer")


def test_the_reports_refer_to_the_wave_plan_for_a_staged_front(report) -> None:
    assert STAGED not in _cache_row(report)["rationale"]
    assert f"cache-aside in front of {CACHE_FRONT_REFERENCE}" in _cache_row(report)["rationale"]
    md = render_engineering_report_md(report)
    assert md.count(STAGED) == 1
    sections = section_texts(render_executive_report_pdf(report), "wordpress")
    assert sum(body.count(STAGED) for body in sections.values()) == 1
    assert normalize(CACHE_FRONT_REFERENCE) in sections["Why this architecture"]


@pytest.mark.passes_on_main  # the review's finding: the chat summary must keep it
def test_the_chat_summary_states_the_staged_front_once(report) -> None:
    final = _final(report)
    assert final.count(STAGED) == 1
    assert CACHE_FRONT_REFERENCE not in final


@pytest.mark.passes_on_main  # the negative cases: no wave plan, or a one-stage front
def test_without_a_wave_plan_or_staging_the_cell_is_as_stored(report) -> None:
    stored = next(e for e in report["facts"]["engines"] if e["role"] == "Cache layer")
    no_waves = {**copy.deepcopy(report), "migration_waves": []}
    assert _cache_row(no_waves)["rationale"] == stored["rationale"]
    one_stage = copy.deepcopy(report)
    one_stage["cache_overlay"]["owners"] = {"aurora_mysql": 18}
    assert _cache_row(one_stage)["rationale"] == stored["rationale"]
