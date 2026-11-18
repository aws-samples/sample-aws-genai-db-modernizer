# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2316: the engineering report's cost table prints facts.engines (cost per engine) and
facts.engine_cost_total_usd as its Total, for the same report.json the results page reads.
The page's table is checked against the same fields in
src/ui/src/components/__tests__/CostBreakdownFacts.test.js, so the two agree."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.report.engineering_report import _cost_by_engine_md
from src.report.formatting import _fmt_usd

REPORT = Path("docs/examples/wordpress/sample-run/synthesis/v2/report.json")


def _rows() -> list[list[str]]:
    lines = _cost_by_engine_md(json.loads(REPORT.read_text()))
    return [[c.strip() for c in line.strip("|").split("|")] for line in lines[2:] if line]


@pytest.mark.passes_on_main  # the reports' table the page's table must equal
def test_the_engineering_report_prints_the_facts_engines_and_their_total() -> None:
    facts = json.loads(REPORT.read_text())["facts"]
    rows = _rows()
    assert [r[0] for r in rows[:-1]] and len(rows) - 1 == len(facts["engines"])
    assert [r[2] for r in rows[:-1]] == [_fmt_usd(e["monthly_cost_usd"]) for e in facts["engines"]]
    assert rows[-1][0] == "Total"
    assert rows[-1][2] == _fmt_usd(facts["engine_cost_total_usd"])
