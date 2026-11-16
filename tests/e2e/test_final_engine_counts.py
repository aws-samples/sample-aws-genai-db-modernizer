# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every deliverable shows the final assignment version's query count per engine (#2358).

The saved discourse run (``tests/fixtures/real_runs/discourse-both``) wrote assignment
versions after the reality check's v2: v3 is an ``--auto`` pick of 26 tied queries, v4 a
post-schema reroute of one query. The deterministic samples never do, which hid that the
results page read the reality check's ``after_distribution`` while the reports and the
facts stated v4's counts. This lays the saved run out, synthesizes it again with today's
code and holds the facts figure the UI reads, the ranking and the reports to one answer.

Deterministic: the replay renders a saved run with no model call, whatever the job's mode.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 -- runs this repo's own synthesis script with fixed argv
import sys
from pathlib import Path
from typing import Any

import pytest

from ci.llm.replay import prepare
from tests.e2e.deliverable_figures import (
    engineering_figures,
    expected_figures,
    final_query_counts,
    invariant_violations,
    pdf_figures,
    pdf_page_texts,
    report_invariant_violations,
)
from tests.e2e.pipeline import _env, from_existing_job
from tests.e2e.test_deliverable_figures import _engineering_required, _pdf_required, _problems

pytestmark = pytest.mark.deterministic

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "tests" / "fixtures" / "real_runs" / "discourse-both"


@pytest.fixture(scope="module")
def resynthesized(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, Any], Any]:
    root = tmp_path_factory.mktemp("final-engine-counts")
    info = prepare(FIXTURE, root)
    db, job = info["db"], info["job_id"]
    common = ["--job-id", job, "--db", db, "--artifact-root", str(root)]
    done = subprocess.run(  # nosec B603 -- fixed argv, this repo's own script
        [sys.executable, "scripts/run_synthesis.py", *common, "--llm-mode", "none"],
        cwd=REPO,
        env=_env(),
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    result = from_existing_job(db, job, root)
    return json.loads(result.report_json().read_text()), result


def _final_counts(report: dict[str, Any]) -> dict[str, int]:
    """The figure the UI reads. Read straight from the facts (no fallback), so a
    synthesis that does not write it fails the test that needs it."""
    counts: dict[str, int] = report["facts"]["engine_query_counts"]
    return counts


@pytest.mark.passes_on_main  # states a fact about the saved run, not about the fix
def test_the_saved_run_has_versions_after_the_reality_check(resynthesized: Any) -> None:
    """The reproduction holds: the reality check's distribution is not the final one."""
    report, _ = resynthesized
    ranking = {r["target"]: r["assigned_queries"] for r in report["ranking"]}
    after = report["reality_check"]["after_distribution"]
    assert {e: n for e, n in after.items() if n} != {e: n for e, n in ranking.items() if n}


def test_the_facts_figure_the_ui_reads_is_the_final_version(resynthesized: Any) -> None:
    report, _ = resynthesized
    ranking = {
        r["target"]: r.get("assigned_queries_in_scope", r["assigned_queries"])
        for r in report["ranking"]
    }
    counts = _final_counts(report)
    assert counts == {e: n for e, n in ranking.items() if n and e in counts}
    assert sum(counts.values()) == report["facts"]["in_scope_query_count"]


def test_report_json_agrees_with_the_facts_figure(resynthesized: Any) -> None:
    report, _ = resynthesized
    _final_counts(report)
    exp = expected_figures(report)
    as_observed = {k: [(v, "report.json")] for k, v in exp.items()}
    problems = report_invariant_violations(report)
    problems += invariant_violations(report, as_observed, "report.json")
    assert [msg for _, msg in problems] == []


def test_the_engineering_report_states_the_facts_figure(resynthesized: Any) -> None:
    """Every figure it parses (each engine's queries, the queries moved) is the one
    report.json states, checked as test_deliverable_figures checks the samples."""
    report, result = resynthesized
    counts = _final_counts(report)
    assert counts
    figs = engineering_figures(result.deliverable("engineering-report.md").read_text())
    problems = _problems(report, figs, _engineering_required)
    assert [msg for _, msg in problems] == []


def test_the_executive_pdf_states_the_facts_figure(resynthesized: Any) -> None:
    report, result = resynthesized
    assert _final_counts(report)
    text = " ".join(pdf_page_texts(str(result.deliverable("executive-report.pdf"))))
    problems = _problems(report, pdf_figures(text), _pdf_required)
    assert [msg for _, msg in problems] == []
    assert final_query_counts(report) == _final_counts(report)
