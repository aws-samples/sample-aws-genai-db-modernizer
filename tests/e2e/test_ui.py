"""The local UI lists the jobs and renders their results against the local API.

Route selection: the live app only ever links to these pages for a completed job
(see src/ui/src/index.js and the pages that `href`/`navigate` to them). The legacy
`/analysis/results/:jobId` page (superseded by `/analysis/results-v2/:jobId`, which
every in-app link now points to) was removed (see #185 — it crashed with React error
#31 because it rendered `synthesis.trade_offs` entries, which are objects per the
current contract, directly as JSX children). `/analysis/results/:jobId` is now a
redirect to `/analysis/results-v2/:jobId`, covered by
`test_legacy_results_route_redirects_to_results_v2` below, so old bookmarks still
work. The legacy `/analysis/report/:jobId` page (the earlier, orange-styled report)
was removed the same way (see #356); `/analysis/report/:jobId` is now a redirect to
`/analysis/results-v2/:jobId` too, covered by
`test_legacy_report_route_redirects_to_results_v2` below. `/analysis/monitor/*` is
excluded for the same reason as before (it is for in-flight jobs, not completed
ones). `/analysis/patterns/:jobId` is exercised only with its required `?target=`
query param, since every real link to that route supplies one
(src/ui/src/pages/PatternAnalysis.js reads `target` from `useSearchParams` with no
default; a bare visit fetches `.../analysis/null` and 404s).
"""

from __future__ import annotations

import json
import re

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


def test_legacy_report_route_redirects_to_results_v2(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """Old bookmarks/links to the retired `/analysis/report/:jobId` page (#356,
    the earlier, orange-styled report) must land on the `/analysis/results-v2/:jobId`
    content instead of the now-removed page."""
    for r in all_runs:
        ev = _watch(page)
        page.goto(f"{ui}/analysis/report/{r.job_id}")
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


def _moved_query_trace(r: PipelineResult) -> dict | None:
    """The full decision trace (report.json ``query_traces``) of the first query whose
    engine moved, or None when no query moved (a real run may move none)."""
    report = json.loads((r.artifact_root / r.report["report"]).read_text())
    listed = ((report.get("facts") or {}).get("query_trace") or {}).get("listed_queries") or []
    traces = {q["query_id"]: q for q in (report.get("query_traces") or {}).get("queries") or []}
    moved = [q["query_id"] for q in listed if q.get("moved") and q["query_id"] in traces]
    trace: dict | None = traces[moved[0]] if moved else None
    return trace


OPEN_TRACE = "Open the journey and decision trace for query "


def test_a_querys_trace_opens_from_the_routing_table(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#822: the results page's query routing table opens a query's journey and its
    "Why this engine" trace from the keyboard, with no schema design in the run.

    On the deterministic wordpress sample (or an external job) it opens a moved
    query and checks the move; on a run where no query moved it opens the first
    query and checks the trace renders.
    """
    r = next((x for x in all_runs if x.db == "wordpress"), all_runs[0])
    if not r.external:
        assert not list(r.job_dir().glob("schema-*")), "the deterministic run has no schema design"
        assert _moved_query_trace(r) is not None, "the wordpress sample moves queries"
    trace = _moved_query_trace(r)

    ev = _watch(page)
    page.goto(f"{ui}/analysis/results-v2/{r.job_id}")
    page.wait_for_load_state("networkidle")
    section = page.locator("#query-routing-section")
    section.get_by_role("table", name="Query routing").wait_for()
    if not r.external:  # wordpress lists 107 queries: more than one page of 25
        section.get_by_role("button", name="Next page").wait_for()
        section.get_by_role("button", name="Previous page").wait_for()
    if trace is not None:
        qid = trace["query_id"]
        section.get_by_role("button", name="Routing changed or decided").click()
        section.get_by_label("Filter queries").fill(qid[:16])
        opener = section.get_by_role("button", name=f"{OPEN_TRACE}{qid}")
    else:
        opener = section.get_by_role("button", name=re.compile(f"^{OPEN_TRACE}")).first
        qid = (opener.get_attribute("aria-label") or "")[len(OPEN_TRACE) :]
    opener.focus()
    page.keyboard.press("Enter")

    modal = page.get_by_role("dialog")
    modal.get_by_text(f"Query Journey: {qid}").wait_for()
    modal.get_by_role("tab", name="Why this engine").click()
    modal.get_by_test_id("query-trace-final").first.wait_for()
    first = modal.get_by_test_id("query-trace-first")
    no_ledger = modal.get_by_test_id("query-trace-no-ledger")
    assert (
        first.count() + no_ledger.count() == 1
    ), "neither the first assignment nor the no-ledger note"
    text = modal.inner_text()
    if trace is not None:
        move = next(
            m for m in trace["moves"] if m["from_engine"] and m["from_engine"] != m["to_engine"]
        )
        modal.get_by_test_id("query-trace-moves").get_by_text(move["explanation"]).wait_for()
        assert trace["final_engine_name"] in text, (r.db, "final engine missing from the trace")
        assert "No later routing step changed this query." not in text
    assert "undefined" not in text and "NaN" not in text
    assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)
