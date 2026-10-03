"""The local UI lists the jobs and renders their results against the local API.

Route selection: the live app only ever links to these pages for a completed job
(see src/ui/src/index.js and the pages that `href`/`navigate` to them). The legacy
`/analysis/results/:jobId` page (superseded by `/analysis/results-v2/:jobId`, which
every in-app link now points to) was removed (see #185 — it crashed with React error
#31 because it rendered `synthesis.trade_offs` entries, which are objects per the
current contract, directly as JSX children). `/analysis/results/:jobId` is now a
redirect to `/analysis/results-v2/:jobId`, covered by
`test_legacy_results_route_redirects_to_results_v2` below, so old bookmarks still
work. `/analysis/monitor/*` is excluded for the same reason as before
(it is for in-flight jobs, not completed ones). `/analysis/patterns/:jobId` is
exercised only with its required `?target=` query param, since every real link to
that route supplies one (src/ui/src/pages/PatternAnalysis.js reads `target` from
`useSearchParams` with no default; a bare visit fetches `.../analysis/null` and 404s).
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page

from tests.e2e.pipeline import PipelineResult

# The product UI is a developer helper; the customer deliverables are the HTML/PDF
# reports (checked cross-browser in test_report_html.py). Restrict this file to
# Chromium only. NOTE for ci/e2e.sh: pytest-playwright implements only_browser via a
# runtime pytest.skip() in pytest_runtest_setup (not deselection), so if this suite
# is ever run with `--browser chromium --browser webkit` under `--fail-on-skip`, the
# webkit parametrizations of every test below will be skipped-then-failed. Run this
# file with a single `--browser chromium` invocation, separate from any multi-browser
# `--fail-on-skip` run.
pytestmark = pytest.mark.only_browser("chromium")

# Mirrors the ENGINE_LABELS map used by the UI's results-v2 and engine-analysis pages
# (src/ui/src/pages/AnalysisResults-02.js, src/ui/src/pages/EngineAnalysis.js) so this
# test can assert the real, human-readable engine name rather than the raw contract id.
ENGINE_LABELS = {
    "dynamodb": "DynamoDB",
    "documentdb": "DocumentDB",
    "opensearch": "OpenSearch",
    "elasticache": "ElastiCache",
    "aurora_mysql": "Aurora MySQL",
    "aurora_postgresql": "Aurora PostgreSQL",
}


def _watch(page: Page) -> dict[str, list[str]]:
    ev: dict[str, list[str]] = {"console": [], "pageerror": [], "failed": []}

    def on_console(msg: object) -> None:
        if getattr(msg, "type", None) == "error":
            ev["console"].append(msg.text)  # type: ignore[attr-defined]

    def on_pageerror(err: object) -> None:
        ev["pageerror"].append(str(err))

    def on_requestfailed(req: object) -> None:
        ev["failed"].append(req.url)  # type: ignore[attr-defined]

    def on_response(resp: object) -> None:
        if resp.status >= 400 and "/api/v1/" in resp.url:  # type: ignore[attr-defined]
            ev["failed"].append(f"{resp.status} {resp.url}")  # type: ignore[attr-defined]

    page.on("console", on_console)
    page.on("pageerror", on_pageerror)
    page.on("requestfailed", on_requestfailed)
    page.on("response", on_response)
    return ev


def _top_engine_label(r: PipelineResult) -> str:
    """The highest-ranked target engine from the synthesis report, as the UI labels it.

    Reads the path off ``r.report`` (the "report" step), not ``r.steps["synthesis"]``:
    the latter is only populated by the deterministic pipeline, while "report" is
    populated in every mode, including a job wrapped by ``from_existing_job``.
    """
    report_path = r.artifact_root / r.report["report"]
    report = json.loads(report_path.read_text())
    top: str = report["ranking"][0]["target"]
    return ENGINE_LABELS.get(top, top)


def test_dashboard_lists_both_jobs(page: Page, ui: str, all_runs: list[PipelineResult]) -> None:
    ev = _watch(page)
    page.goto(f"{ui}/dashboard")
    page.wait_for_load_state("networkidle")
    text = page.inner_text("body")
    for r in all_runs:
        assert r.job_id in text or r.db in text, f"{r.db}/{r.job_id} not on dashboard"
    assert ev == {"console": [], "pageerror": [], "failed": []}


def test_results_page_shows_the_ranked_engines(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    for r in all_runs:
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results-v2/{r.job_id}")
        page.wait_for_load_state("networkidle")
        text = page.inner_text("body")
        assert "undefined" not in text and "NaN" not in text, r.db
        assert r.db in text, (r.db, "source database name missing")
        assert _top_engine_label(r) in text, (
            r.db,
            "top-ranked engine missing",
            _top_engine_label(r),
        )
        assert "/mo" in text, (r.db, "no projected cost rendered")
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_legacy_results_route_redirects_to_results_v2(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """Old bookmarks/links to the retired `/analysis/results/:jobId` page (#185)
    must land on the `/analysis/results-v2/:jobId` content instead of the blank,
    crashed page it used to render."""
    for r in all_runs:
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results/{r.job_id}")
        page.wait_for_load_state("networkidle")
        assert page.url.rstrip("/").endswith(f"/analysis/results-v2/{r.job_id}"), (
            r.db,
            page.url,
        )
        text = page.inner_text("body")
        assert "undefined" not in text and "NaN" not in text, r.db
        assert r.db in text, (r.db, "source database name missing")
        assert _top_engine_label(r) in text, (
            r.db,
            "top-ranked engine missing",
            _top_engine_label(r),
        )
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_assignments_page_recommends_the_ranked_engines(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    for r in all_runs:
        ev = _watch(page)
        page.goto(f"{ui}/analysis/assignments/{r.job_id}")
        page.wait_for_load_state("networkidle")
        text = page.inner_text("body")
        assert "undefined" not in text and "NaN" not in text, r.db
        assert f"Your {r.db} workload" in text, (r.db, "workload summary missing")
        assert _top_engine_label(r) in text, (
            r.db,
            "top-ranked engine missing",
            _top_engine_label(r),
        )
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_report_page_renders_the_full_report(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    for r in all_runs:
        ev = _watch(page)
        page.goto(f"{ui}/analysis/report/{r.job_id}")
        page.wait_for_load_state("networkidle")
        text = page.inner_text("body")
        assert "undefined" not in text and "NaN" not in text, r.db
        assert r.job_id in text and r.db in text, (r.db, "job id / database name missing")
        assert "risk(s) identified" in text, (r.db, "executive summary missing")
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_engine_analysis_page_shows_per_engine_metrics(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    for r in all_runs:
        ev = _watch(page)
        page.goto(f"{ui}/analysis/engine-analysis/{r.job_id}")
        page.wait_for_load_state("networkidle")
        text = page.inner_text("body")
        assert "undefined" not in text and "NaN" not in text, r.db
        assert f"Engine analysis: {r.db}" in text, (r.db, "engine analysis header missing")
        assert "queries analyzed" in text, (r.db, "cross-engine summary missing")
        assert _top_engine_label(r) in text, (
            r.db,
            "top-ranked engine missing",
            _top_engine_label(r),
        )
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_pattern_analysis_page_renders_for_a_selected_engine(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """Both samples route 'key value lookups' queries to dynamodb, so it is always
    a valid, non-empty ``target`` regardless of which sample is loaded."""
    for r in all_runs:
        ev = _watch(page)
        page.goto(f"{ui}/analysis/patterns/{r.job_id}?target=dynamodb")
        page.wait_for_load_state("networkidle")
        text = page.inner_text("body")
        assert "undefined" not in text and "NaN" not in text, r.db
        assert "patterns detected for dynamodb" in text, (r.db, "pattern analysis body missing")
        assert "Key Value Lookups" in text, (r.db, "expected signal missing")
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)
