# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synthesis counts the collector's rate window bases into ``facts.rate_windows`` (#1893),
and the engineering report and the executive PDF state the same sentence once.

wordpress and trailgear record each statement's first/last seen; discourse (PostgreSQL,
collected before the scripts read ``pg_stat_statements_info``) records only an uptime,
which is not its counters' window, so its rates assume 24 hours.
"""

from __future__ import annotations

import json
from collections import Counter

import pytest

from src.shared.rate_window import rate_window_facts
from tests.e2e.deliverable_figures import md_plain, pdf_page_texts
from tests.e2e.pipeline import PipelineResult


# A replayed or headless job may predate #1893's facts: the deterministic run checks it.
@pytest.mark.deterministic
def test_the_report_counts_every_statements_rate_window(run: PipelineResult) -> None:
    collector = json.loads((run.job_dir() / "collector" / "output.json").read_text())
    patterns = collector["queries"]["query_patterns"]
    bases = Counter(p.get("rate_window_basis") for p in patterns)
    facts = json.loads(run.report_json().read_text())["facts"]["rate_windows"]
    assert facts["statement_count"] == len(patterns)
    assert facts["observed_count"] == bases["observed"]
    assert facts["uptime_count"] == bases["uptime"]
    assert facts["assumed_24h_count"] == bases["assumed_24h"]
    assert facts["stats_reset_count"] == bases["stats_reset"]
    assert facts["statement"] == rate_window_facts(patterns)["statement"]
    line = f"Call rates: {facts['statement']}."
    md = md_plain(run.deliverable("engineering-report.md").read_text())
    assert md.count(line) == 1
    pdf = " ".join(" ".join(pdf_page_texts(str(run.deliverable("executive-report.pdf")))).split())
    assert pdf.count(line) == 1
    expected = {"wordpress": "observed", "trailgear": "observed", "discourse": "assumed_24h"}
    if run.db in expected:
        assert bases[expected[run.db]] == len(patterns), bases
