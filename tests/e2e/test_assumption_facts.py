# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1990: synthesis states the assumptions block and the growth scenario once, and the
engineering report and the executive PDF print each line once; growth is a separate
scenario (its 1× row is the headline, the 5× and 10× rows re-price the scaled rates)."""

from __future__ import annotations

import json

import pytest

from src.report.facts import RATE_WINDOW_DETAIL_KEYS
from tests.e2e.deliverable_figures import md_plain, pdf_page_texts
from tests.e2e.pipeline import PipelineResult


# A replayed or headless job may predate #1990's facts: the deterministic run checks it.
@pytest.mark.deterministic
def test_every_deliverable_states_the_assumptions_once(run: PipelineResult) -> None:
    facts = json.loads(run.report_json().read_text())["facts"]
    block, scenario = facts["assumptions"], facts["growth_scenarios"]
    assert [a["key"] for a in block["assumptions"]][:2] == ["growth", "peak"]
    assert {a["origin"] for a in block["assumptions"]} <= {"default", "collected", "customer"}
    rows = scenario["scenarios"]
    assert [r["growth"] for r in rows] == [1.0, 5.0, 10.0]
    assert rows[0]["monthly_cost_usd"] == facts["projected_monthly_cost_usd"]
    assert rows[0]["monthly_cost_usd"] <= rows[1]["monthly_cost_usd"] <= rows[2]["monthly_cost_usd"]
    md = md_plain(run.deliverable("engineering-report.md").read_text())
    pdf = " ".join(" ".join(pdf_page_texts(str(run.deliverable("executive-report.pdf")))).split())
    # #1991: the executive report leaves the rate window's detail to the engineering
    # report and the gate, and prints the scenario label instead.
    detail = {a["line"] for a in block["assumptions"] if a["key"] in RATE_WINDOW_DETAIL_KEYS}
    for line in [*block["lines"], *scenario["lines"]]:
        assert md.count(f"{line}.") == 1, line
        assert pdf.count(" ".join(f"{line}.".split())) == (0 if line in detail else 1), line
