# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The local UI lists the jobs and renders their results against the local API.

Route selection: the live app only ever links to these pages for a completed job
(see src/ui/src/AppRoutes.js and the pages that `href`/`navigate` to them). The
results page lives at `/analysis/results/:jobId`. Two older URLs redirect to it so
bookmarks keep working, each covered below: `/analysis/results-v2/:jobId` (the
page's name until its version suffix was dropped, #957) and `/analysis/report/:jobId`
(the earlier, orange-styled report, removed in #356). The job-monitoring page
(`/analysis/monitor/:jobId`) and the engine-analysis page were removed (#957); their
URLs show the not-found page (#2393). The pattern-analysis page was removed (#2300);
`/analysis/patterns/:jobId?target=<engine>` redirects to that engine in the results
page's explorer (covered below).
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from pathlib import Path

import playwright.sync_api
import pytest
from playwright.sync_api import Browser, Locator, Page, expect

from src.report import escaping
from src.report.deliverables import DELIVERABLES_DIR, FOLDER_ENTRIES
from src.report.risks import shared_mitigation_refs
from src.shared.migration_risk_rules import backfill_report_risks
from tests.branding import hits as product_name_hits
from tests.e2e.pipeline import PipelineResult, real_designs
from tests.placeholders import placeholder_leaks

# The results page is a primary deliverable next to the executive report (#987); its
# browser checks (rendering, overflow, accessibility) live here. Restrict this file
# to Chromium only. NOTE for ci/e2e.sh: pytest-playwright implements only_browser via a
# runtime pytest.skip() in pytest_runtest_setup (not deselection), so if this suite
# is ever run with `--browser chromium --browser webkit` under `--fail-on-skip`, the
# webkit parametrizations of every test below will be skipped-then-failed. Run this
# file with a single `--browser chromium` invocation, separate from any multi-browser
# `--fail-on-skip` run.
pytestmark = pytest.mark.only_browser("chromium")

# Mirrors the ENGINE_LABELS map used by the UI's results page
# (src/ui/src/pages/AnalysisResults.js) so this
# test can assert the real, human-readable engine name rather than the raw contract id.
ENGINE_LABELS = {
    "dynamodb": "DynamoDB",
    "documentdb": "DocumentDB",
    "opensearch": "OpenSearch",
    "elasticache": "ElastiCache",
    "aurora_mysql": "Aurora MySQL",
    "aurora_postgresql": "Aurora PostgreSQL",
}


# How long a page may take to render its fetched data. A readiness wait returns the moment
# the content shows, so this is a ceiling for a slow runner and the large discourse
# sample (about 10 s cold on CI), never a delay; Playwright's 5 s default is too tight.
READY_MS = 60_000


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


# #2313: the results page's tabs, by the id ``?tab=`` names, with their labels, in order,
# and the sections each holds, in order (src/ui/src/utils/resultsTabs.js). A tab's
# sections are in the page only once the tab is open.
RESULTS_TABS = {
    "summary": "Summary",
    "plan": "Plan",
    "queries": "Queries",
    "data-model": "Data model",
    "risks": "Risks and decisions",
}
TAB_SECTIONS = {
    "summary": [
        "executive-summary-section",
        "cost-breakdown-section",
        "query-flow-section",
        "assumptions-panel",
    ],
    "plan": [
        "migration-roadmap-section",
        "step-gate-section",
        "unservable-section",
        "close-calls-section",
        "unreviewed-moves-note",
    ],
    "queries": ["query-routing-section"],
    "data-model": ["table-owners-section", "table-design-section", "source-tables-section"],
    "risks": [
        "risk-assessment-section",
        "dynamodb-merge-conflicts",
        "tradeoffs-section",
        "dynamodb-merge-reviews",
    ],
}
# The section each tab always shows once the results are loaded (the loading state shows
# none of them), so a wait on it is a wait on the data, never on network silence.
TAB_READY = {
    "summary": "#executive-summary-section",
    "plan": "#migration-roadmap-section",
    "queries": "#query-routing-section",
    "data-model": "#table-owners-section",
    "risks": "#risk-assessment-section",
}
# The notes on the Plan tab that are on the tab but not in its On this page navigation.
UNLISTED_SECTIONS = {"unreviewed-moves-note"}


def _results_tab(page: Page, tab: str) -> Locator:
    """The results page's tab ``tab`` (#2313): a tab of the page's own tab list, not one of
    the tabs inside a section (the risk severities, the Table design's engines)."""
    # #2363: a tab's label may carry its count, "Queries (107)".
    label = re.compile(rf"^{re.escape(RESULTS_TABS[tab])}( \([\d,]+\))?$")
    return page.get_by_role("tablist", name="Assessment results").get_by_role("tab", name=label)


def _show_tab(page: Page, tab: str) -> None:
    """Open the results page's tab ``tab`` (a click, as a reader would) and wait for its
    content. A shared page may be on any tab: a test reading a section opens its tab."""
    control = _results_tab(page, tab)
    control.wait_for(timeout=READY_MS)
    if control.get_attribute("aria-selected") != "true":
        control.click()
    expect(control).to_have_attribute("aria-selected", "true")
    page.locator(TAB_READY[tab]).wait_for(timeout=READY_MS)


def _goto_results(page: Page, ui: str, r: PipelineResult, tab: str) -> None:
    """Open ``r``'s results page on tab ``tab`` by its link (``?tab=``) and wait for it."""
    page.goto(f"{ui}/analysis/results/{r.job_id}?tab={tab}")
    expect(_results_tab(page, tab)).to_have_attribute("aria-selected", "true", timeout=READY_MS)
    page.locator(TAB_READY[tab]).wait_for(timeout=READY_MS)


def _each_tab(page: Page) -> Iterator[str]:
    """Open each tab of the results page in turn, yielding its id: a check of the whole
    page runs on every tab, since a closed tab's sections are not on the page."""
    for tab in RESULTS_TABS:
        _show_tab(page, tab)
        yield tab


# The sections stay unchanged for this many frames and this long before a snapshot counts.
SETTLED_FRAMES = 30
SETTLED_QUIET_MS = 500

_SETTLED_SECTIONS_JS = """
async ([ids, unlisted, frames, quietMs, timeoutMs]) => {
  const snapshot = () => {
    const present = ids.filter(id => document.getElementById(id));
    const listed = present.filter(id => !unlisted.includes(id));
    const nav = document.querySelector('nav[aria-labelledby="results-on-this-page-title"]');
    const hrefs = nav ? [...nav.querySelectorAll('a')].map(a => a.getAttribute('href')) : [];
    const want = listed.length < 2 ? [] : listed.map(id => '#' + id);
    const same = hrefs.length === want.length && hrefs.every((h, i) => h === want[i]);
    return { present, listed, same };
  };
  const nextFrame = () => new Promise(resolve => requestAnimationFrame(resolve));
  const start = performance.now();
  let last = null;
  let key = null;
  let stableFrames = 0;
  let since = start;
  while (performance.now() - start < timeoutMs) {
    await nextFrame();
    const now = snapshot();
    const nowKey = JSON.stringify(now);
    if (!now.same || nowKey !== key) {
      key = nowKey;
      last = now;
      stableFrames = 0;
      since = performance.now();
      continue;
    }
    stableFrames += 1;
    if (stableFrames >= frames && performance.now() - since >= quietMs) {
      return { present: last.present, listed: last.listed };
    }
  }
  return null;
}
"""


def _settled_sections(page: Page, tab: str) -> tuple[list[str], list[str]]:
    """The open tab's sections on the page and the ones its On this page navigation
    lists, once the two agree and stay unchanged (#2376, #2394). A section that renders
    late reaches the page first and the navigation a frame after, and a further section
    can still arrive after the two first agree. Both come from one DOM snapshot that is
    taken only after the page and the navigation have held the same sections for
    ``SETTLED_FRAMES`` animation frames and ``SETTLED_QUIET_MS``; a tab that never
    settles fails."""
    # A section that loads its own data (the assumptions panel) joins the page only when its
    # request returns, however long a loaded runner takes. A fixed quiet period cannot
    # tell that wait from a settled page, so the page's data requests must be idle first.
    page.wait_for_load_state("networkidle", timeout=READY_MS)
    settled = page.evaluate(
        _SETTLED_SECTIONS_JS,
        [TAB_SECTIONS[tab], sorted(UNLISTED_SECTIONS), SETTLED_FRAMES, SETTLED_QUIET_MS, READY_MS],
    )
    assert settled, f"the {tab} tab's sections and On this page links never settled"
    return settled["present"], settled["listed"]


def _loaded(page: Page, r: PipelineResult) -> None:
    """Wait until the results page has rendered ``r``'s data (#1320), on the Summary tab it
    opens on (#2313).

    Network idle can fire before the fetch that fills the page has started or finished,
    leaving only "Loading assessment results...". The executive summary and the source
    database name render only once the results are loaded (the loading state shows
    neither), and ``expect`` retries until they do.
    """
    expect(_results_tab(page, "summary")).to_have_attribute(
        "aria-selected", "true", timeout=READY_MS
    )
    page.locator(TAB_READY["summary"]).wait_for(timeout=READY_MS)
    expect(page.get_by_text(r.db).first).to_be_visible(timeout=READY_MS)


OpenResults = Callable[[PipelineResult], tuple[Page, dict[str, list[str]]]]


@pytest.fixture(scope="module")
def _shared_results(
    browser: Browser, browser_context_args: dict, ui: str
) -> Iterator[tuple[object, OpenResults]]:
    """The context and opener behind ``loaded_results``: one page per job for the module."""
    context = browser.new_context(**browser_context_args)
    # One trace for the module's shared pages; each test records a chunk of it.
    context.tracing.start(screenshots=True, snapshots=True)
    opened: dict[str, tuple[Page, dict[str, list[str]]]] = {}

    def open_results(r: PipelineResult) -> tuple[Page, dict[str, list[str]]]:
        if r.job_id not in opened:
            page = context.new_page()
            ev = _watch(page)
            page.goto(f"{ui}/analysis/results/{r.job_id}")
            _loaded(page, r)
            opened[r.job_id] = (page, ev)
        return opened[r.job_id]

    yield context, open_results
    context.tracing.stop()
    context.close()


@pytest.fixture
def loaded_results(
    _shared_results: tuple[object, OpenResults],
    request: pytest.FixtureRequest,
    pytestconfig: pytest.Config,
) -> Iterator[OpenResults]:
    """One loaded results page per job, shared by the tests that only read it (#1593).

    Each such test used to open its own page, so a large job's page (discourse: 1,654
    queries, about 11 MB of JSON) was fetched and rendered a dozen times per run. A
    test that clicks, types, resizes or expands anything opens its own ``page``
    instead, so every shared page stays as it loaded; the one exception is the tab
    (#2313): a test opens the tab it reads with ``_show_tab``, and never assumes one. The events ``_watch``
    records cover the shared page's whole life, so a test's "no console error, no
    failed request" check holds from the load to its own reads.

    The context is not pytest-playwright's, so its ``--screenshot`` and ``--tracing``
    options do not reach it: a failing test keeps a screenshot of each shared page it
    read and its own trace chunk here instead, under ``--output``."""
    context, open_results = _shared_results
    used: dict[str, Page] = {}

    def open_and_record(r: PipelineResult) -> tuple[Page, dict[str, list[str]]]:
        page, ev = open_results(r)
        used[r.job_id] = page
        return page, ev

    context.tracing.start_chunk()  # type: ignore[attr-defined]
    yield open_and_record
    report = getattr(request.node, "rep_call", None)
    if report is None or not report.failed:
        context.tracing.stop_chunk()  # type: ignore[attr-defined]
        return
    out = Path(pytestconfig.getoption("output")) / re.sub(r"[^\w.-]+", "-", request.node.nodeid)
    out.mkdir(parents=True, exist_ok=True)
    context.tracing.stop_chunk(path=str(out / "trace.zip"))  # type: ignore[attr-defined]
    for job_id, page in used.items():
        page.screenshot(path=str(out / f"shared-{job_id}.png"), full_page=True)


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
    for r in all_runs:
        expect(page.get_by_text(r.job_id).or_(page.get_by_text(r.db)).first).to_be_visible(
            timeout=READY_MS
        )
    text = page.inner_text("body")
    for r in all_runs:
        assert r.job_id in text or r.db in text, f"{r.db}/{r.job_id} not on dashboard"
    # #2304: an unknown duration reads as a dash, never as a negative time.
    assert "-1h" not in text and "-1m" not in text, text
    assert ev == {"console": [], "pageerror": [], "failed": []}


@pytest.mark.parametrize("width", [1440, 390])
def test_dashboard_rows_show_engines_and_projected_cost(
    page: Page, ui: str, all_runs: list[PipelineResult], width: int
) -> None:
    """#2345: each finished job's row shows its target engines as catalog badges and the
    projected monthly cost from its report facts; at phone width the list does not
    scroll the page sideways."""
    page.set_viewport_size({"width": width, "height": 900})
    r = all_runs[0]
    page.goto(f"{ui}/dashboard")
    row = page.locator("tbody tr", has_text=r.db).first
    expect(row).to_be_visible(timeout=READY_MS)
    expect(row.locator("[data-engine-badge]").first).to_be_visible(timeout=READY_MS)
    assert re.search(r"\$[\d,]+\.\d\d/mo", row.inner_text()), row.inner_text()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


def test_dashboard_stats_are_plain_values_with_real_figures(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#2306: the status figures are one container of plain values, none a link
    without a target, and "Active jobs" appears once, not in a second container."""
    page.goto(f"{ui}/dashboard")
    expect(page.get_by_text("Total analyses")).to_be_visible(timeout=READY_MS)
    expect(page.locator("dd").first).to_contain_text(re.compile(r"\d"))
    assert page.get_by_text("Active jobs", exact=True).count() == 1
    assert page.get_by_text("Recent activity").count() == 0
    # A link without an href is focusable and does nothing.
    assert page.locator("dl a").count() == 0, page.locator("dl a").all_inner_texts()


def test_dashboard_with_the_api_down_shows_an_error_and_no_zeros(page: Page, ui: str) -> None:
    """#2306: unreachable API -> one error alert with a retry, dashes for the
    figures, no request URL and never 0 / 0.0% / 0.0h."""
    page.route(re.compile(r"/api/v1/"), lambda route: route.abort())
    page.goto(f"{ui}/dashboard")
    expect(page.get_by_text("The dashboard could not load")).to_be_visible(timeout=READY_MS)
    expect(page.get_by_role("button", name="Retry")).to_be_visible()
    text = page.inner_text("body")
    assert "0.0%" not in text and "0.0h" not in text, text
    assert "/api/v1" not in text and "No records" not in text, text
    assert "(0)" not in text, text  # no count while the list has not loaded
    assert "Assessments unavailable" in text, text


def test_a_failed_results_request_shows_a_fixed_message_and_no_server_text(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#2346: a 500 whose body holds HTML and a URL reads as one short message with a
    Retry, never the status, the request URL or the server's text."""
    r = all_runs[0]
    hostile = "<script>window.__pwned=1</script> GET http://10.0.0.5:8000/api/v1/x failed (500)"
    page.route(
        re.compile(rf"/api/v1/assessments/{re.escape(r.job_id)}/results$"),
        lambda route: route.fulfill(status=500, content_type="text/html", body=hostile),
    )
    page.goto(f"{ui}/analysis/results/{r.job_id}")
    expect(page.get_by_text("The service returned an error.")).to_be_visible(timeout=READY_MS)
    expect(page.get_by_role("button", name="Retry")).to_be_visible()
    text = page.inner_text("body")
    for leak in ("10.0.0.5", "/api/v1", "500", "pwned", "<script"):
        assert leak not in text, (leak, text)
    assert page.evaluate("window.__pwned") is None


# #2327: with the local API the new-assessment page is one upload form, one primary action.
def _open_new_assessment(page: Page, ui: str) -> None:
    page.goto(f"{ui}/analysis/create")
    expect(page.get_by_role("heading", name="New assessment")).to_be_visible(timeout=READY_MS)
    expect(page.get_by_text("It stays on this machine, under the artifact folder.")).to_be_visible(
        timeout=READY_MS
    )


def test_new_assessment_opens_on_the_upload_form_locally(page: Page, ui: str) -> None:
    ev = _watch(page)
    _open_new_assessment(page, ui)
    text = page.inner_text("body")
    assert "S3" not in text and "presigned" not in text.lower(), text
    assert "Connect to a live database" not in text and "Coming soon" not in text, text
    assert page.get_by_role("button", name="Start assessment").count() == 1
    assert page.get_by_role("button", name="Upload file").count() == 0
    assert page.get_by_role("button", name="Next").count() == 0  # no Wizard
    assert page.get_by_role("checkbox", name=re.compile("^ElastiCache")).is_checked() is False
    assert ev == {"console": [], "pageerror": [], "failed": []}, ev


def test_new_assessment_empty_submit_shows_inline_errors_only(page: Page, ui: str) -> None:
    requests: list[str] = []
    page.on("request", lambda req: requests.append(req.url))
    _open_new_assessment(page, ui)
    page.get_by_role("button", name="Start assessment").click()
    expect(page.get_by_text("Choose a collector output file (.json).")).to_be_visible()
    expect(page.get_by_text("Enter a database name.")).to_be_visible()
    assert page.evaluate("document.activeElement && document.activeElement.type") == "file"
    assert page.locator('[class*="awsui_flashbar"]').count() == 0
    assert not [u for u in requests if "/assessments" in u], requests


def test_new_assessment_fills_the_name_in_from_the_file(
    page: Page, ui: str, tmp_path: Path
) -> None:
    collection = tmp_path / "wordpress-collection.json"
    # The shape the MySQL collection script prints: a header line, then the object.
    collection.write_text(
        "collection_output\n"
        + json.dumps({"tables": [], "queries": [], "metadata": {"database_name": "wordpress"}})
    )
    _open_new_assessment(page, ui)
    page.locator('input[type="file"]').set_input_files(str(collection))
    expect(page.get_by_text("Read from the file: database wordpress")).to_be_visible(
        timeout=READY_MS
    )
    expect(page.get_by_label("Database name")).to_have_value("wordpress")


def test_results_page_shows_the_ranked_engines(
    loaded_results: OpenResults, all_runs: list[PipelineResult]
) -> None:
    for r in all_runs:
        page, ev = loaded_results(r)
        _show_tab(page, "summary")
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


def test_old_results_v2_route_redirects_to_results(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """Old bookmarks/links to `/analysis/results-v2/:jobId` (#957 dropped the
    version suffix) must land on the `/analysis/results/:jobId` content."""
    for r in all_runs:
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results-v2/{r.job_id}")
        expect(page).to_have_url(re.compile(rf"/analysis/results/{r.job_id}/?$"), timeout=READY_MS)
        _loaded(page, r)
        assert page.url.rstrip("/").endswith(f"/analysis/results/{r.job_id}"), (
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


def _open_per_query_view(page: Page) -> None:
    """Switch the assignment review to its per-query view: a segmented control, which
    Cloudscape shows as a dropdown on a narrow screen (#2326)."""
    segment = page.get_by_role("radio", name="Per query").or_(
        page.get_by_role("button", name="Per query", exact=True)
    )
    if segment.count():
        segment.first.click()
    else:
        page.get_by_role("button", name=re.compile("View|Per query|Summary")).first.click()
        page.get_by_role("option", name="Per query").click()


def test_assignment_review_of_a_finished_job_states_the_approval_and_offers_none(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#2326: a finished job's assignment review says which version was approved, links
    the results, and offers neither approval nor engine overrides."""
    for r in all_runs:
        page.goto(f"{ui}/analysis/assignments/{r.job_id}")
        alert = page.locator("[data-gate-state]")
        expect(alert).to_contain_text("Approved: assignment version", timeout=READY_MS)
        assert alert.get_attribute("data-gate-state") == "done", r.db
        expect(alert.get_by_text("View results").first).to_be_visible()
        body = page.inner_text("body")
        assert "Approve assignment" not in body and "Continue with recommendation" not in body, r.db
        _open_per_query_view(page)
        expect(page.locator(".query-list-row__summary").first).to_be_visible(timeout=READY_MS)
        assert page.locator(".query-list-row button[aria-haspopup=listbox]").count() == 0, r.db
        assert page.locator(".query-list-row input[type=checkbox]").count() == 0, r.db


def test_assignment_review_states_the_chat_gates_figures(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#2326: the assignment review's figures are the chat gate's (gate_block_facts on the
    same artifacts): queries, source tables, the engines kept and the cache candidates."""
    from scripts import run_assessment
    from src.shared.gate_facts import gate_block_facts
    from src.storage.local_store import LocalArtifactStore

    for r in all_runs:
        store = LocalArtifactStore(base_dir=str(r.artifact_root))
        chat = gate_block_facts(run_assessment._reality_check_artifact(store, r.job_id, r.db))
        page.goto(f"{ui}/analysis/assignments/{r.job_id}")
        figures = page.locator("[data-figure=gate-figures]")
        expect(figures).to_be_visible(timeout=READY_MS)
        text = " ".join(figures.inner_text().split())
        assert f"Queries {chat['query_total_count']:,}" in text, (r.db, text)
        assert "Source tables" in text, (r.db, text)
        for row in chat["rows"]:
            if row["engine"] in chat["surviving"] and not row["dropped"]:
                assert f"{row['after']:,}" in text, (r.db, row["engine"], text)
        cache = chat["cache_candidates"]
        if cache and cache["reads"]:
            assert f"Cache candidates {cache['read_count']:,}" in text, (r.db, text)
            table = page.locator("[data-figure=gate-cache-candidates] tbody tr")
            assert table.count() == len(cache["reads"]), r.db


def test_assignments_page_recommends_the_ranked_engines(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    for r in all_runs:
        ev = _watch(page)
        page.goto(f"{ui}/analysis/assignments/{r.job_id}")
        expect(page.get_by_text(f"Your {r.db} workload").first).to_be_visible(timeout=READY_MS)
        text = page.inner_text("body")
        assert "undefined" not in text and "NaN" not in text, r.db
        assert f"Your {r.db} workload" in text, (r.db, "workload summary missing")
        assert _top_engine_label(r) in text, (
            r.db,
            "top-ranked engine missing",
            _top_engine_label(r),
        )
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_legacy_report_route_redirects_to_results(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """Old bookmarks/links to the retired `/analysis/report/:jobId` page (#356,
    the earlier, orange-styled report) must land on the `/analysis/results/:jobId`
    content instead of the now-removed page."""
    for r in all_runs:
        ev = _watch(page)
        page.goto(f"{ui}/analysis/report/{r.job_id}")
        expect(page).to_have_url(re.compile(rf"/analysis/results/{r.job_id}/?$"), timeout=READY_MS)
        _loaded(page, r)
        assert page.url.rstrip("/").endswith(f"/analysis/results/{r.job_id}"), (
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


@pytest.mark.parametrize("job_id", ["doesnotexist", "does.not.exist"])
@pytest.mark.parametrize("route", ["results", "monitor/summary", "assignments"])
def test_an_unknown_assessment_id_shows_one_not_found_state(
    page: Page, ui: str, all_runs: list[PipelineResult], route: str, job_id: str
) -> None:
    """#2307: a mistyped or stale job link (also an id the API refuses, such as one with a
    dot) shows one not-found state, with no half-built page, no request URL or status
    code, and no approve button."""
    page.goto(f"{ui}/analysis/{route}/{job_id}")
    expect(page.get_by_role("heading", name="Assessment not found")).to_be_visible(timeout=READY_MS)
    expect(page.get_by_text(f"No assessment has the id {job_id}.")).to_be_visible()
    expect(page.get_by_role("link", name="Go to assessments")).to_be_visible()
    text = page.inner_text("body")
    assert "/api/v1/" not in text and "Failed to fetch" not in text and "404" not in text, text
    assert page.get_by_role("button", name=re.compile("Approve assignment")).count() == 0


@pytest.mark.parametrize("route", ["analysis/monitor", "analysis/engine-analysis"])
def test_removed_job_pages_show_the_not_found_page(
    page: Page, ui: str, all_runs: list[PipelineResult], route: str
) -> None:
    """#957 removed the job-monitoring page (dead download buttons) and the engine-analysis
    page (pre-assignment figures that contradicted the results page). #2393: their URLs
    used to fall through to the dashboard with no hint the page was gone. They now show
    the not-found page at the address asked for, with links to the dashboard and the
    assessments list, and a document title that says so."""
    r = all_runs[0]
    ev = _watch(page)
    page.goto(f"{ui}/{route}/{r.job_id}")
    expect(page.get_by_role("heading", level=1, name="Page not found")).to_be_visible(
        timeout=READY_MS
    )
    assert page.url.rstrip("/").endswith(f"/{route}/{r.job_id}"), (route, page.url)
    expect(page).to_have_title(re.compile("Page not found"))
    text = page.inner_text("body")
    assert f"/{route}/{r.job_id}" in text, route
    assert "Engine analysis:" not in text, route
    assert "Download artifact" not in text and "Download logs" not in text, route
    expect(page.get_by_role("link", name="Open the assessments list")).to_be_visible()
    page.get_by_role("link", name="Go to the dashboard").click()
    expect(page).to_have_url(re.compile(r"/dashboard/?$"), timeout=READY_MS)
    expect(page.get_by_text(r.db).or_(page.get_by_text(r.job_id)).first).to_be_visible(
        timeout=READY_MS
    )
    assert ev == {"console": [], "pageerror": [], "failed": []}, (route, ev)


def _designed_aurora_engines(r: PipelineResult) -> list[str]:
    return sorted(
        {
            p.parent.parent.name.removeprefix("schema-")
            for p in real_designs(r.job_dir())
            if p.parent.parent.name.startswith("schema-aurora_")
        }
    )


def test_old_pattern_analysis_link_lands_on_the_engine_in_the_results_page(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#2300: the pattern-analysis page is gone; its old URL redirects to the results
    page. A designed job lands on the query table filtered to a NoSQL target (#2446: the
    explorer folded into it), with the counter and the token the table's own picker
    would show, and on the Table design of an Aurora target; focus moves to that section.
    A job without a design only has to reach the results page. #2313: the query table
    is on the Queries tab and the Table design on the Data model tab; the link opens
    that tab and the URL names it. The cache layer's link filters to cached queries."""
    checked = 0
    for r in all_runs:
        ev = _watch(page)
        page.goto(f"{ui}/analysis/patterns/{r.job_id}?target=dynamodb")
        expect(_results_tab(page, "queries")).to_have_attribute(
            "aria-selected", "true", timeout=READY_MS
        )
        expect(page.locator(TAB_READY["queries"])).to_be_visible(timeout=READY_MS)
        expect(page).to_have_url(
            re.compile(
                rf"/analysis/results/{r.job_id}\?engine=dynamodb&tab=queries#query-routing-section$"
            ),
            timeout=READY_MS,
        )
        text = page.inner_text("body")
        assert "undefined" not in text and "NaN" not in text, r.db
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)
    for r in _designed_runs(all_runs):
        if r.external:
            continue  # a real run's engine plan is its own; the deterministic job is fixed
        ev = _watch(page)
        page.goto(f"{ui}/analysis/patterns/{r.job_id}?target=dynamodb")
        section = page.locator("#query-routing-section")
        expect(section).to_be_visible(timeout=READY_MS)
        _wait_for_query_routing_rows(section, r, ev)
        header = section.locator("h2").first
        expect(header).to_contain_text(re.compile(r"\([\d,]+ of [\d,]+\)"), timeout=READY_MS)
        found = re.search(r"\(([\d,]+) of ([\d,]+)\)", header.inner_text())
        assert found, header.inner_text()
        shown, total = (int(g.replace(",", "")) for g in found.groups())
        assert 0 < shown < total, (r.job_id, shown, total)
        # The token reads as the engine's name, as the table's own engine picker gives.
        expect(section).to_contain_text(re.compile(r"Engine\s*=\s*DynamoDB"))
        assert page.evaluate("document.activeElement && document.activeElement.id") == (
            "query-routing-section"
        ), r.job_id
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.job_id, ev)
        # #2480 review: the cache layer owns no query, so its link lists the queries it
        # caches ("Cached = Yes"), never an engine filter that would match none.
        overlay = json.loads(r.report_json().read_text()).get("cache_overlay")
        cache = overlay.get("engine") if isinstance(overlay, dict) else None
        if cache:
            page.goto(f"{ui}/analysis/results/{r.job_id}?engine={cache}#query-routing-section")
            _wait_for_query_routing_rows(section, r, ev)
            expect(section).to_contain_text(re.compile(r"Cached\s*=\s*Yes"), timeout=READY_MS)
            assert not re.search(r"Engine\s*=", section.inner_text()), r.job_id
            # The filter's own words resolve: no raw locale key (e.g. its Clear filters).
            assert "analysis-results." not in section.inner_text(), r.job_id
        for engine in _designed_aurora_engines(r):
            page.goto(f"{ui}/analysis/patterns/{r.job_id}?target={engine}")
            expect(page.locator("#table-design-section")).to_be_visible(timeout=READY_MS)
            expect(_results_tab(page, "data-model")).to_have_attribute("aria-selected", "true")
            expect(page).to_have_url(re.compile(r"&tab=data-model#table-design-section$"))
            selected = page.locator("#table-design-section [role=tab][aria-selected=true]")
            expect(selected).to_contain_text(ENGINE_LABELS[engine])
            assert page.evaluate("document.activeElement && document.activeElement.id") == (
                "table-design-section"
            ), (r.job_id, engine)
        checked += 1
    assert checked or any(r.external for r in all_runs), "no deterministic designed job"


def test_a_link_to_an_engine_the_plan_has_no_part_for_says_so(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#2300: the old link names an engine the job's plan has nothing for (here an id no
    plan has); the results page opens as usual and a notice says why nothing is filtered."""
    for r in all_runs:
        ev = _watch(page)
        page.goto(f"{ui}/analysis/patterns/{r.job_id}?target=no-such-engine")
        # #2313: the link names the explorer's section, so the Queries tab opens.
        expect(page.locator(TAB_READY["queries"])).to_be_visible(timeout=READY_MS)
        expect(_results_tab(page, "queries")).to_have_attribute("aria-selected", "true")
        expect(page.get_by_text("no-such-engine is not in this plan")).to_be_visible(
            timeout=READY_MS
        )
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.job_id, ev)


def test_a_link_to_a_routed_engine_without_a_design_says_its_design_has_not_been_made(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#2300: queries are routed to an engine (its node is in the Sankey) but no schema
    design exists for it, as on a job whose design did not run. The link says that, not
    that the engine is missing from the plan."""
    checked = 0
    for r in all_runs:
        page.goto(f"{ui}/analysis/results/{r.job_id}")
        expect(page.locator("#executive-summary-section")).to_be_visible(timeout=READY_MS)
        routed = page.locator("[data-sankey-node]").evaluate_all(
            "nodes => nodes.map(n => n.getAttribute('data-sankey-node'))"
        )
        designed = {p.parent.parent.name.removeprefix("schema-") for p in real_designs(r.job_dir())}
        engines = [
            e
            for e in routed
            if e in ENGINE_LABELS and not e.startswith("aurora_") and e not in designed
        ]
        for engine in engines[:1]:
            ev = _watch(page)
            page.goto(f"{ui}/analysis/patterns/{r.job_id}?target={engine}")
            expect(page.locator(TAB_READY["queries"])).to_be_visible(timeout=READY_MS)
            notice = f"Queries are routed to {ENGINE_LABELS[engine]}, but its schema design has not been made"
            expect(page.get_by_text(notice)).to_be_visible(timeout=READY_MS)
            assert "is not in this plan" not in page.inner_text("body"), (r.job_id, engine)
            assert ev == {"console": [], "pageerror": [], "failed": []}, (r.job_id, ev)
            checked += 1
    # An external (model-run) job designs every routed engine, so it may have none to check.
    assert checked or any(
        r.external for r in all_runs
    ), "no job routes queries to an engine that has no design"


def test_a_link_opens_the_linked_table_design_tab_when_it_is_not_the_first(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#2300: Cloudscape tabs decide on their first render whether they are controlled,
    so the linked engine has to be known then. When the job designed one Aurora engine, the
    other relational engine's design is added to the served schema designs (no e2e job has
    two), and the link to the engine that sorts second opens its tab. The engines are the
    job's own (#2488): PostgreSQL sorts after MySQL, so the link names PostgreSQL. The
    served Aurora designs list no access patterns, as the deterministic design has none."""
    checked = 0
    for r in all_runs:
        if len(_designed_aurora_engines(r)) != 1:
            continue  # no Aurora design, or two already: nothing to add a second tab to
        _goto_results(page, ui, r, "data-model")
        expect(page.locator("#table-design-section")).to_be_visible(timeout=READY_MS)
        _serve_extra_aurora_design(page, _another_aurora_engine(r), drop_patterns=True)
        try:
            page.goto(f"{ui}/analysis/patterns/{r.job_id}?target=aurora_postgresql")
            selected = page.locator("#table-design-section [role=tab][aria-selected=true]")
            expect(selected).to_contain_text("Aurora PostgreSQL", timeout=READY_MS)
            tabs = page.locator("#table-design-section [role=tab]").all_inner_texts()
            # A real design's own sections add tabs (validation errors, warnings): count
            # the engine tabs only.
            assert len([t for t in tabs if t.startswith("Aurora")]) == 2, tabs
        finally:
            page.unroute("**/schema-designs")
        checked += 1
    # An external job's designed engines are its own, so it may have no single Aurora design.
    assert checked or any(
        r.external for r in all_runs
    ), "no job with a single Aurora design to add a second design to"


def _moved_query_trace(r: PipelineResult) -> dict | None:
    """The full decision trace (report.json ``query_traces``) of the first query whose
    engine moved, or None when no query moved (a real run may move none)."""
    report = json.loads((r.artifact_root / r.report["report"]).read_text())
    listed = ((report.get("facts") or {}).get("query_trace") or {}).get("listed_queries") or []
    traces = {q["query_id"]: q for q in (report.get("query_traces") or {}).get("queries") or []}
    moved = [q["query_id"] for q in listed if q.get("moved") and q["query_id"] in traces]
    trace: dict | None = traces[moved[0]] if moved else None
    return trace


OPEN_TRACE = "Open the history and decision trace for query "


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
        # The deterministic run designs nothing: only skip records (#2154).
        assert real_designs(r.job_dir()) == [], "the deterministic run has no schema design"
        assert _moved_query_trace(r) is not None, "the wordpress sample moves queries"
    trace = _moved_query_trace(r)

    ev = _watch(page)
    _goto_results(page, ui, r, "queries")
    section = page.locator("#query-routing-section")
    expect(section).to_be_visible(timeout=READY_MS)
    # #2319: the table is open when the tab opens, with nothing to expand first.
    assert section.locator("button[aria-expanded]").count() == 0
    section.get_by_role("table", name="Query routing").wait_for(timeout=READY_MS)
    _wait_for_query_routing_rows(section, r, ev)
    if not r.external:  # wordpress lists 107 queries: more than one page of 25
        section.get_by_role("button", name="Next page").wait_for(timeout=READY_MS)
        section.get_by_role("button", name="Previous page").wait_for(timeout=READY_MS)
    if trace is not None:
        qid = trace["query_id"]
        _filter_queries(section, qid[:16])
        opener = section.get_by_role("button", name=f"{OPEN_TRACE}{qid}")
    else:
        opener = section.get_by_role("button", name=re.compile(f"^{OPEN_TRACE}")).first
        qid = (opener.get_attribute("aria-label") or "")[len(OPEN_TRACE) :]
    opener.focus()
    page.keyboard.press("Enter")

    modal = _query_panel(page, qid)
    modal.get_by_role("tab", name="Why this engine").click()
    modal.get_by_test_id("query-trace-final").first.wait_for(timeout=READY_MS)
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
        modal.get_by_test_id("query-trace-moves").get_by_text(move["explanation"]).wait_for(
            timeout=READY_MS
        )
        assert trace["final_engine_name"] in text, (r.db, "final engine missing from the trace")
        assert "No later routing step changed this query." not in text
    assert "undefined" not in text and "NaN" not in text
    assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def _consolidated_query(r: PipelineResult) -> tuple[str, int, int] | None:
    """A query the effective assignment's own Moves took to another engine with a
    different score: its id, the figure the journey must show (the Move's score for
    the query on its new engine, read by the ledger's own rule, ``query_score``,
    which takes the query's ladder rung before a group's mean ``score_after``),
    and the score on the engine it left. None when the run consolidated no such
    query."""
    from src.agents.referee.move_ledger import query_score
    from src.contracts.move_ledger import Move

    report = json.loads((r.artifact_root / r.report["report"]).read_text())
    version = (report.get("query_traces") or {}).get("effective_version")
    path = r.job_dir() / "assignment" / f"v{version}" / "assignment.json"
    if not version or not path.exists():
        return None
    for raw in json.loads(path.read_text()).get("moves") or []:
        m = Move.model_validate(raw)
        after = query_score(m)
        if m.from_engine and m.from_engine != m.to_engine and m.score_before is not None:
            if after is not None and round(after) != round(m.score_before):
                return m.query_id, round(after), round(m.score_before)
    return None


def test_a_consolidated_querys_journey_shows_its_new_engines_fit(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#1373: the query journey's "Workload Fit" is the query's score on the engine
    the reality check moved it to, not the score of the engine it left."""
    r = next((x for x in all_runs if x.db == "wordpress"), all_runs[0])
    found = _consolidated_query(r)
    if not r.external:
        assert found is not None, "the wordpress sample consolidates queries"
    if found is None:
        return
    qid, score, left = found

    ev = _watch(page)
    _goto_results(page, ui, r, "queries")
    section = page.locator("#query-routing-section")
    expect(section).to_be_visible(timeout=READY_MS)
    section.get_by_role("table", name="Query routing").wait_for(timeout=READY_MS)
    _filter_queries(section, qid[:16])
    section.get_by_role("button", name=f"{OPEN_TRACE}{qid}").click()
    modal = _query_panel(page, qid)
    expect(modal).to_contain_text("Workload fit", timeout=READY_MS)
    text = modal.inner_text()
    fit = re.search(r"Workload fit\s+(\d+)%", text)
    assert fit is not None, text[:2000]
    assert int(fit.group(1)) == score, (qid, "shown", fit.group(1), "engine it left", left)
    assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def _utility_pin(r: PipelineResult) -> str | None:
    """A query the effective assignment marks pinned as a utility statement (#1373)."""
    report = json.loads((r.artifact_root / r.report["report"]).read_text())
    version = (report.get("query_traces") or {}).get("effective_version")
    path = r.job_dir() / "assignment" / f"v{version}" / "assignment.json"
    if not version or not path.exists():
        return None
    pins = [
        qa["query_id"]
        for qa in json.loads(path.read_text())["query_assignments"]
        if qa.get("pinned") == "utility"
    ]
    return str(sorted(pins)[0]) if pins else None


def test_a_utility_pins_journey_shows_pinned_not_a_percentage(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#1373 (maintainer decision on #1381): the journey of a query pinned as a
    utility statement shows "pinned (utility)" as its Workload Fit, not a percentage."""
    r = next((x for x in all_runs if x.db == "wordpress"), all_runs[0])
    qid = _utility_pin(r)
    if not r.external:
        assert qid is not None, "the wordpress sample pins utility statements"
    if qid is None:
        return
    ev = _watch(page)
    _goto_results(page, ui, r, "queries")
    section = page.locator("#query-routing-section")
    expect(section).to_be_visible(timeout=READY_MS)
    section.get_by_role("table", name="Query routing").wait_for(timeout=READY_MS)
    _filter_queries(section, qid[:16])
    section.get_by_role("button", name=f"{OPEN_TRACE}{qid}").click()
    modal = _query_panel(page, qid)
    expect(modal).to_contain_text("Workload fit", timeout=READY_MS)
    text = modal.inner_text()
    assert re.search(r"Workload fit\s+pinned \(utility\)", text), text[:2000]
    assert not re.search(r"Workload fit\s+\d+%", text), text[:2000]
    assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_a_selected_query_opens_in_a_split_panel_beside_the_table(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#2319: selecting a query from the keyboard opens its detail in a split panel, not a
    dialog: the table stays on the page, the title is the short id and the operation, the
    raw JSON is behind "Developer details", axe finds nothing blocking with the panel
    open, and closing the panel leaves the table as it was."""
    r = next((x for x in all_runs if x.db == "wordpress"), all_runs[0])
    ev = _watch(page)
    _goto_results(page, ui, r, "queries")
    section = page.locator("#query-routing-section")
    expect(section).to_be_visible(timeout=READY_MS)
    _wait_for_query_routing_rows(section, r, ev)
    opener = section.get_by_role("button", name=re.compile(f"^{OPEN_TRACE}")).first
    qid = (opener.get_attribute("aria-label") or "")[len(OPEN_TRACE) :]
    opener.focus()
    page.keyboard.press("Enter")

    panel = _query_panel(page, qid)
    assert page.get_by_role("dialog").count() == 0, "a query opens in a split panel, not a dialog"
    expect(section.get_by_role("table", name="Query routing")).to_be_visible()
    title = panel.get_by_role("heading", level=2).first.inner_text()
    assert re.fullmatch(rf"Query {re.escape(qid[:8])}( · \S.*)?", title.strip()), title
    assert qid not in title, "the title uses the short id"
    expect(panel).to_contain_text("Calls per hour", timeout=READY_MS)
    text = panel.inner_text()
    assert "Assigned Engine" not in text and "Frequency (per hour)" not in text, text[:600]
    assert panel.get_by_role("tab", name="JSON").count() == 0
    dev = panel.get_by_role("button", name="Developer details")
    assert dev.get_attribute("aria-expanded") == "false"
    blocking, where, _ = _axe_blocking(page)
    assert blocking == [], (r.db, blocking, where)

    page.get_by_role("button", name="Close the query details").click()
    expect(panel).to_be_hidden(timeout=READY_MS)
    expect(section.get_by_role("table", name="Query routing")).to_be_visible()
    assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_the_query_table_filters_and_pages_a_large_job(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#2319: discourse lists 1,654 queries. The table renders one page of rows, not all of
    them, and a property filter token narrows the list and its counter."""
    r = next((x for x in all_runs if x.db == "discourse"), all_runs[0])
    ev = _watch(page)
    _goto_results(page, ui, r, "queries")
    section = page.locator("#query-routing-section")
    expect(section).to_be_visible(timeout=READY_MS)
    _wait_for_query_routing_rows(section, r, ev)
    table = section.get_by_role("table", name="Query routing")
    rows = table.locator("tbody tr")
    assert 0 < rows.count() <= 25, rows.count()
    counter = section.get_by_role("heading", level=2).first
    total = int(re.search(r"\(([\d,]+)\)", counter.inner_text()).group(1).replace(",", ""))
    if not r.external:
        assert total > 1000, total
        section.get_by_role("button", name="Next page").wait_for(timeout=READY_MS)
    _filter_queries(section, "SELECT")
    expect(counter).to_contain_text(f"of {total:,}", timeout=READY_MS)
    assert rows.count() <= 25
    assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def _filter_queries(section: Locator, text: str) -> None:
    """Free text in the query table's property filter (#2319): type it and press Enter,
    which turns it into a token."""
    box = section.get_by_label("Filter queries")
    box.fill(text)
    box.press("Enter")


def _query_panel(page: Page, qid: str) -> Locator:
    """The split panel a selected query opens (#2319): titled with the short id, which
    is the first eight characters of the query id."""
    panel = page.get_by_role("region", name=re.compile(f"^Query {re.escape(qid[:8])}"))
    panel.first.wait_for(timeout=READY_MS)
    return panel.first


# #887: the table lists GET /assessments/{job_id}/query-journeys, read from the
# context graph. The wait is on the table's own outcome, its first query or the
# load error the page shows (the UI client gives a request 30 s, see
# GlobalConfigurations.timeout), not on network silence. The budget covers
# rendering the rows on a loaded machine; CI measures a few seconds.
ROUTING_DATA_TIMEOUT_MS = 60_000
ROUTING_LOAD_ERROR = "Query details aren't available for this assessment"


def _wait_for_query_routing_rows(
    section: Locator, r: PipelineResult, ev: dict[str, list[str]]
) -> None:
    """Wait until the opened query routing section lists its queries. A failure names
    the job, the request the table waits for and the API errors seen so far, instead
    of a bare locator timeout (#887)."""
    # A query's open control exists only in a data row; the table's loading and
    # empty states are one merged placeholder row, so a bare row is no signal.
    queries = section.get_by_role("button", name=re.compile(f"^{OPEN_TRACE}"))
    settled = queries.first.or_(section.get_by_text(ROUTING_LOAD_ERROR)).first
    try:
        settled.wait_for(timeout=ROUTING_DATA_TIMEOUT_MS)
    except playwright.sync_api.TimeoutError:
        pass
    if queries.count() == 0:
        pytest.fail(
            f"{r.db}/{r.job_id}: the Query routing table lists no query after waiting up "
            f"to {ROUTING_DATA_TIMEOUT_MS // 1000}s for its first row or its load error. "
            f"It lists GET /api/v1/assessments/{r.job_id}/query-journeys; API errors so "
            f"far: {ev['failed']}; console errors: {ev['console']}; the section reads: "
            f"{section.inner_text()[:300]!r}"
        )


def _report_wave_stats(r: PipelineResult) -> dict[str, tuple[str, str]]:
    """Each wave's headline as the reports compute it (``roadmap._wave_headline_stats``)
    and its count phrase as the engineering report prints it, by wave number."""
    from src.report.escaping import md_text
    from src.report.roadmap import _facts_by_wave, _wave_headline_stats, resolve_migration_waves

    report = json.loads((r.artifact_root / r.report["report"]).read_text())
    facts = _facts_by_wave(report)
    out = {}
    for w in resolve_migration_waves(report):
        count_phrase, table_phrase = _wave_headline_stats(w, facts.get(int(w.get("wave") or 0)))
        headline = " ".join(f"{count_phrase}{table_phrase}".split())
        out[str(w.get("wave"))] = (headline, f"- {md_text(count_phrase)}")
    return out


def _assert_readiness_shown(shown: Locator, wave: dict, db: str, n: str) -> None:
    """A wave's readiness is a status beside the facts' readiness line (#2318). The line
    is shown exactly as the reports print it; the status never replaces or trims it."""
    line = wave["readiness_line"]
    state = wave.get("readiness")
    assert shown.locator("[data-figure=wave-readiness-line]").inner_text() == line, (db, n)
    assert shown.inner_text().endswith(line), (db, n)
    status = shown.locator("[class*=awsui_status-]").first
    open_count = wave.get("open_warning_count") or 0
    if state == "suggested":
        want = (
            f"Suggested, {open_count} warning{'' if open_count == 1 else 's'} to accept"
            if open_count
            else "Suggested"
        )
    else:
        want = {
            "held": "Not suggested yet",
            "not_designed": "Not designed",
            "not_reviewed": "Not reviewed",
        }[state]
    assert status.inner_text().strip() == want, (db, n, status.inner_text())


def test_results_page_shows_the_reports_wave_plan(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#957: the results page's migration roadmap shows the same waves as the
    PDF report and the engineering report, with the same headline for each wave,
    read from report.json."""
    for r in all_runs:
        stats = _report_wave_stats(r)
        assert stats, (r.db, "the report has no waves")
        md = r.deliverable("engineering-report.md").read_text(encoding="utf-8")
        for _, md_line in stats.values():
            assert md_line in md.splitlines(), (r.db, md_line)
        expected = {n: headline for n, (headline, _) in stats.items()}
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        gates = {str(w["wave"]): w.get("gate") or "" for w in report["migration_waves"]}
        ev = _watch(page)
        _goto_results(page, ui, r, "plan")
        section = page.locator("#migration-roadmap-section")
        section.wait_for(timeout=READY_MS)
        cards = section.locator("[data-wave]").all()
        shown = {
            str(c.get_attribute("data-wave")): " ".join(
                c.locator("[data-figure=wave-stats]").inner_text().split()
            )
            for c in cards
        }
        assert shown == expected, r.db
        lists = {
            str(w["wave"]): [p["items"] for p in (w.get("gate_parts") or []) if p["items"]]
            for w in report["migration_waves"]
        }
        for c in cards:
            n = str(c.get_attribute("data-wave"))
            if gates[n]:
                gate = c.locator("[data-figure=wave-gate]")
                # #1028: each list of the gate shows as an inline "(…)" until
                # clicked; opened, the gate reads exactly as report.json's.
                closed = gate.locator('button[aria-expanded="false"]')
                assert closed.count() == len(lists[n]), (r.db, n)
                for items in lists[n]:
                    assert ", ".join(items) not in gate.inner_text(), (r.db, n)
                while closed.count():
                    closed.first.click()
                # Read as shown, without the small "hide" control after each list.
                shown_gate = gate.evaluate(
                    "el => { const c = el.cloneNode(true);"
                    " c.querySelectorAll('[data-gate-hide]').forEach(n => n.remove());"
                    " return c.textContent; }"
                )
                assert shown_gate == f"Gate before the next wave: {gates[n]}", (r.db, n)
                assert gate.locator("[data-gate-hide]").count() == len(lists[n]), (r.db, n)
        # #1120: a cross-engine wave 1 states the facts layer's note and lists every
        # relational gap by feature, as both reports do; no other wave does.
        notes = {str(w["wave"]): w.get("heterogeneous_note") for w in report["facts"]["waves"]}
        listed = {
            str(w["wave"]) for w in report["facts"]["waves"] if w.get("lists_relational_gaps")
        }
        all_gaps = (report["facts"].get("relational_gaps") or {}).get("gaps") or []
        for c in cards:
            n = str(c.get_attribute("data-wave"))
            note = c.locator("[data-figure=wave-heterogeneous-note]")
            shown_gaps = c.locator("[data-figure=wave-gap]").all_inner_texts()
            gaps = all_gaps if n in listed else []
            if notes.get(n):
                assert note.inner_text() == notes[n], (r.db, n)
                assert len(shown_gaps) == len(gaps), (r.db, n)
                for shown_gap, gap in zip(shown_gaps, gaps, strict=True):
                    assert shown_gap.startswith(f"{gap['feature']} ("), (r.db, n, shown_gap)
            else:
                assert note.count() == 0 and shown_gaps == [], (r.db, n)
        # #1992: each card states the facts' readiness line, the rule once.
        wave_facts = {str(w["wave"]): w for w in report["facts"]["waves"]}
        readiness = {
            str(w["wave"]): (w.get("readiness"), w.get("readiness_line"))
            for w in report["facts"]["waves"]
        }
        for c in cards:
            n = str(c.get_attribute("data-wave"))
            state, line = readiness[n]
            shown_line = c.locator("[data-figure=wave-readiness]")
            if line:
                assert shown_line.get_attribute("data-readiness") == state, (r.db, n)
                _assert_readiness_shown(shown_line, wave_facts[n], r.db, n)
            else:
                assert shown_line.count() == 0, (r.db, n)
        rules = section.locator("[data-figure=wave-readiness-rule]").count()
        assert rules == (1 if any(line for _, line in readiness.values()) else 0), r.db
        # #2107: the cutover is the customer's, said once, in the facts layer's words.
        cutover = section.locator("[data-figure=cutover-responsibility]")
        assert cutover.count() == 1, r.db
        assert cutover.inner_text() == report["facts"]["cutover_responsibility"], r.db
        # #2318: it is in the roadmap's header.
        header = section.get_by_role("heading", name=re.compile("^Migration roadmap")).locator(
            "xpath=ancestor::div[contains(@class, 'header')][1]"
        )
        assert header.locator("[data-figure=cutover-responsibility]").count() == 1, r.db
        # The gates and routine lines state it in their own stored words; the roadmap adds
        # no second statement of its own.
        assert (
            section.locator("[data-wave] [data-figure=cutover-responsibility]").count() == 0
        ), r.db
        count = report["facts"]["migration_wave_count"]
        assert f"Migration roadmap ({count} waves)" in section.inner_text(), r.db
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def _adoption_tiles(path: dict, adoption: dict) -> dict[str, str | None]:
    """The four tiles of a path as the results page must state them (#1132): read from
    ``facts.adoption.paths[]``, an earlier path's cost only with per-path sizing, and no
    difference from the relational move only when it has no cost."""
    from src.shared.engine_names import display_engine

    per_path = adoption["cost_basis"] == "per_path_sizing"
    stated = per_path or path["path_id"] == "full_target"
    difference = None
    delta = path["cost_delta_vs_relational_usd"]
    if (
        adoption.get("relational_monthly_cost_usd") is not None
        and path["path_id"] != "relational_only"
        and stated
    ):
        difference = (
            "the same as the relational move only"
            if delta == 0
            else f"${abs(delta):,.2f} {'less' if delta < 0 else 'more'} than the relational move only"
        )
    return {
        "engines": ", ".join(display_engine(e) for e in path["engines"]),
        "queries": f"{path['queries_moved']:,}",
        "cost": (
            f"${path['monthly_cost_usd']:,.2f}" if stated else "Stated for the full target only"
        ),
        "difference": difference,
        "risks": f"{path['critical_high_risks']:,}",
    }


def _path_label(adoption: dict, path_id: str) -> str:
    """The label the path selector shows (``adoptionPaths.pathLabel``)."""
    path = next(p for p in adoption["paths"] if p["path_id"] == path_id)
    if path_id == "relational_only":
        label = "Relational move only"
    elif path_id == "full_target":
        label = "Full target"
    else:
        label = f"Through step {path['stops_after_step']}"
    return f"{label}, the default" if path_id == adoption["default_path_id"] else label


def _segments(panel: Locator) -> Locator:
    """The segments of the path selector, when it shows as a segmented control. In a narrow
    container Cloudscape shows the same choice as a dropdown instead, and past six paths
    the page itself uses one; the segments are then absent or hidden."""
    return panel.locator("[data-figure=adoption-path-selector] button[data-testid]")


def _selector_labels(panel: Locator) -> list[str]:
    """The paths the selector offers, by label."""
    segments = _segments(panel)
    if segments.count() and segments.first.is_visible():
        return [b.strip() for b in segments.all_inner_texts()]
    trigger = panel.locator("[data-figure=adoption-path-selector] [aria-haspopup=listbox]").first
    trigger.click()
    # The selected option's text can repeat on a second line.
    labels = [o.strip().split("\n")[0] for o in panel.page.get_by_role("option").all_inner_texts()]
    panel.page.keyboard.press("Escape")
    return labels


def _choose_path(panel: Locator, path_id: str, adoption: dict) -> None:
    """Pick a path in the selector (#2318: a segmented control, a dropdown past six paths)."""
    segments = _segments(panel)
    if segments.count() and segments.first.is_visible():
        panel.locator(
            f'[data-figure=adoption-path-selector] button[data-testid="{path_id}"]'
        ).click()
        return
    panel.locator("[data-figure=adoption-path-selector] [aria-haspopup=listbox]").first.click()
    panel.page.get_by_role("option", name=_path_label(adoption, path_id), exact=True).click()


def _shown_tiles(panel: Locator) -> dict[str, str | None]:
    cost = panel.locator("[data-figure=path-cost]")
    difference = panel.locator("[data-figure=path-cost-difference]")
    shown_difference = difference.inner_text().strip() if difference.count() else None
    cost_text = cost.inner_text().strip()
    if shown_difference:
        cost_text = cost_text.replace(shown_difference, "").strip()
    return {
        "engines": panel.locator("[data-figure=path-engines]").inner_text().strip(),
        "queries": panel.locator("[data-figure=path-queries-moved]").inner_text().strip(),
        "cost": cost_text,
        "difference": shown_difference,
        "risks": panel.locator("[data-figure=path-risks]").inner_text().strip(),
    }


def test_results_page_shows_the_adoption_paths_stages_and_one_step_option(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#1132: above the roadmap's wave cards, a path selector opens on the full target
    (the default, so the roadmap reads as before), each tile reads as report.json's
    ``facts.adoption.paths[]``, and the selected path's steps and cost lines are the
    engineering report's. Choosing an earlier path greys out the waves and stages it
    leaves out and sends nothing to the API; each wave states its stages as
    ``facts.slice_plan.wave_phases`` and the engineering report do; the one-step option
    states the report's one-step lines. A report without adoption paths shows none."""
    from tests.e2e.deliverable_figures import md_plain

    writes: list[str] = []

    def on_request(req: playwright.sync_api.Request) -> None:
        if req.method not in ("GET", "HEAD", "OPTIONS"):
            writes.append(f"{req.method} {req.url}")

    page.on("request", on_request)
    checked = 0
    for r in all_runs:
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        adoption = (report.get("facts") or {}).get("adoption") or {}
        plan = (report.get("facts") or {}).get("slice_plan") or {}
        ev = _watch(page)
        writes.clear()
        _goto_results(page, ui, r, "plan")
        section = page.locator("#migration-roadmap-section")
        panel = section.locator("[data-figure=adoption-paths]")
        if not adoption.get("paths"):
            assert panel.count() == 0, r.db
            continue
        md = md_plain(r.deliverable("engineering-report.md").read_text(encoding="utf-8"))
        assignment = r.job_dir() / "assignment" / f"v{report['assignment_summary']['version']}"
        before = {p.name: p.read_bytes() for p in assignment.iterdir() if p.is_file()}
        paths = {p["path_id"]: p for p in adoption["paths"]}
        summary = adoption["summary_path_ids"]
        # #2318: a segmented control, one segment per summary path, in the facts' order.
        assert _selector_labels(panel) == [_path_label(adoption, p) for p in summary], r.db
        assert panel.locator("[role=toolbar]").count() == 1, r.db
        assert panel.locator("[role=group] [data-path]").count() == 0, r.db
        selected = panel.locator("[data-figure=selected-path]")
        # The full target opens selected, and nothing is greyed: the roadmap as before.
        assert selected.get_attribute("data-path") == adoption["default_path_id"], r.db
        assert section.locator("[data-left-out]").count() == 0, r.db
        assert panel.locator("[data-figure=adoption-path-count]").inner_text() == (
            f"({adoption['path_count']} paths)"
        ), r.db
        assert f"## Adoption paths ({adoption['path_count']})" in md, r.db
        text = panel.inner_text()
        assert "tables migrat" not in text.lower(), r.db
        for path_id in summary:
            _choose_path(panel, path_id, adoption)
            expect(selected).to_have_attribute("data-path", path_id)
            path = paths[path_id]
            want = _adoption_tiles(path, adoption)
            assert _shown_tiles(selected) == want, (r.db, path_id)
            # The same steps line and cost line as the path's block in the engineering
            # report.
            steps = selected.locator("[data-figure=path-steps]").inner_text().strip()
            assert f"- {steps}\n" in md, (r.db, path_id, steps)
            cost_line = f"- Monthly cost: {want['cost']}"
            if want["difference"]:
                cost_line += f", {want['difference']}"
            if str(want["cost"]).startswith("$"):
                assert f"{cost_line}\n" in md, (r.db, path_id, cost_line)
            # Waves and stages the path leaves out are greyed; the others are not.
            stop = path["stops_after_step"]
            for phase in plan.get("wave_phases") or []:
                card = section.locator(f'[data-wave="{phase["phase_wave"]}"]')
                out = all(n > stop for n in phase["step_numbers"])
                default = path_id == adoption["default_path_id"]
                assert (card.get_attribute("data-left-out") == "true") == (out and not default), (
                    r.db,
                    path_id,
                    phase["phase_wave"],
                )
        # The capped selector's "Show all" lists every offered path.
        show_all = panel.locator("[data-figure=adoption-show-all] button")
        if len(summary) < adoption["path_count"]:
            show_all.click()
            assert _selector_labels(panel) == [_path_label(adoption, p) for p in paths], r.db
        else:
            assert show_all.count() == 0, r.db
        # Each wave states its steps and stages as the engineering report does; open,
        # the stages are the plan's, in run order.
        _choose_path(panel, "full_target", adoption)
        for phase in plan.get("wave_phases") or []:
            card = section.locator(f'[data-wave="{phase["phase_wave"]}"]')
            line = card.locator("[data-figure=wave-stages-line]").inner_text().strip()
            numbers = phase["step_numbers"]
            span = (
                f"Step {numbers[0]}"
                if len(numbers) == 1
                else f"Steps {numbers[0]} to {numbers[-1]}"
            )
            n = phase["stage_count"]
            stages = f"{n} {'stage' if n == 1 else 'stages'}"
            assert line == f"{span}: {stages}", (r.db, line)
            assert f"- {span} ({stages}):" in md, (r.db, span)
            card.locator('[data-disclosure="wave-stage-list"]').click()
            shown = card.locator("[data-stage]")
            assert [s.get_attribute("data-stage") for s in shown.all()] == phase["stage_labels"]
            # #1699: open, each step's why sentence is report.json's, word for word, and
            # the engineering report's; its figures are headed by the facts' labels.
            by_number = {s["step_number"]: s for s in plan.get("plan_steps") or []}
            whys = [w.inner_text().strip() for w in card.locator("[data-figure=step-why]").all()]
            assert whys == [by_number[n]["why"] for n in numbers], (r.db, phase["phase_wave"])
            # The engineering report prints the same raw sentence, escaped as Markdown.
            report_md = r.deliverable("engineering-report.md").read_text(encoding="utf-8")
            for sentence in whys:
                line = f"  - {escaping.md_text(sentence)}"
                assert line in report_md.splitlines(), (r.db, sentence)
            labels = plan.get("metric_labels") or {}
            for heading in card.locator("[data-figure=step-metrics] th[title]").all():
                assert heading.get_attribute("title") in labels.values(), (r.db, heading)
        # The one-step option: the engineering report's lines and checks.
        one = adoption["one_step"]
        option = panel.locator("[data-figure=one-step-option]")
        option.get_by_text("Show the one-step option").click()
        for name in ("one-step-in-place", "one-step-gate", "one-step-streams"):
            hook = option.locator(f"[data-figure={name}]")
            if hook.count():
                assert f"- {hook.inner_text().strip()}\n" in md, (r.db, name)
        verdict = option.locator("[data-figure=one-step-verdict]").inner_text().strip()
        assert verdict in md, r.db
        rows = option.locator("[data-figure=one-step-check]")
        assert rows.count() == len(one["one_step_checks"]), r.db
        for row in rows.all():
            cells = [c.strip() for c in row.locator("td").all_inner_texts()]
            assert "| " + " | ".join(cells) + " |" in md, (r.db, cells)
        assert "cutover" not in option.inner_text().lower(), r.db
        # The one-step link names what the engineering report names, and lands on it.
        adopt = option.locator("[data-figure=one-step-adopt]").inner_text().strip()
        adopt = " ".join(adopt.split())
        # #2362: the page names the tab where the report says "above" or "below".
        if "the target schemas on the " in adopt:
            # A completed design: the report points at its "Target schemas by engine",
            # the page at its own design section; both say "the target schemas".
            assert "adopt the target schemas in " in md, (r.db, adopt)
        else:
            on_tab = " table on the Summary tab "
            assert on_tab in adopt, (r.db, adopt)
            assert f"{adopt.replace(on_tab, ' table above ')}\n" in md, (r.db, adopt)
        assert " above" not in adopt and " below" not in adopt, (r.db, adopt)
        # #2313: what it lands on is on another tab (the target schemas on Queries or
        # Data model, the Target engines on Summary); following it opens that tab and
        # moves focus to it, and the URL names both.
        link = option.locator("[data-figure=one-step-adopt] a")
        href = link.get_attribute("href") or ""
        assert href.startswith("#"), (r.db, href)
        target = href[1:]
        target_tab = next(
            (t for t, ids in TAB_SECTIONS.items() if target in ids),
            "summary" if target == "target-engines" else None,
        )
        assert target_tab and target_tab != "plan", (r.db, href)
        link.click()
        expect(_results_tab(page, target_tab)).to_have_attribute("aria-selected", "true")
        expect(page.locator(href)).to_be_visible(timeout=READY_MS)
        assert page.locator(href).count() == 1, (r.db, href)
        expect(page).to_have_url(re.compile(rf"tab={target_tab}{re.escape(href)}$"))
        expect(page.locator(href)).to_be_focused()
        # Choosing a path changed only what is shown.
        assert writes == [], (r.db, writes)
        after = {p.name: p.read_bytes() for p in assignment.iterdir() if p.is_file()}
        assert after == before, r.db
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)
        checked += 1
    # Both deterministic samples offer paths; an external (model-run) job written before
    # them offers none, and then the page shows none.
    assert checked or any(r.external for r in all_runs), "no sample offers an adoption path"


def test_back_after_the_one_step_link_returns_to_the_plan_tab(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#2313 review: a jump from one tab to a section on another is a step the reader can
    go back from, as choosing a tab is. Summary, then Plan, then the one-step option's
    link (its target is on another tab), then Back: the Plan tab, at the URL it had; Back
    again: the Summary."""
    checked = 0
    for r in all_runs:
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        if not ((report.get("facts") or {}).get("adoption") or {}).get("paths"):
            continue
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results/{r.job_id}")
        _loaded(page, r)
        _show_tab(page, "plan")
        expect(page).to_have_url(re.compile(rf"/analysis/results/{r.job_id}\?tab=plan$"))
        option = page.locator("#migration-roadmap-section [data-figure=one-step-option]")
        option.get_by_text("Show the one-step option").click()
        option.locator("[data-figure=one-step-adopt] a").click()
        expect(_results_tab(page, "plan")).to_have_attribute("aria-selected", "false")
        page.go_back()
        expect(_results_tab(page, "plan")).to_have_attribute("aria-selected", "true")
        expect(page).to_have_url(re.compile(rf"/analysis/results/{r.job_id}\?tab=plan$"))
        page.locator(TAB_READY["plan"]).wait_for(timeout=READY_MS)
        page.go_back()
        expect(_results_tab(page, "summary")).to_have_attribute("aria-selected", "true")
        expect(page).to_have_url(re.compile(rf"/analysis/results/{r.job_id}$"))
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)
        checked += 1
    assert checked or any(r.external for r in all_runs), "no sample offers an adoption path"


def test_results_page_states_each_tables_source_of_truth_and_replicas(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#992, #2321: the Table owners table states the replicated tables report.json's
    facts.table_sources lists (the rows the engineering report states): each row's owner
    is its source of truth, its CDC to cell the replicas the CDC runs to and its Replica
    reads cell the reads a replica serves. #1232: the eventual-consistency risk is stated
    once, in the sentence the engineering report prints (facts.table_sources.
    shared_risk_sentence), behind the Info link. There is no section of its own; an old
    #sources-of-truth-section link lands inside Table owners."""
    from src.shared.engine_names import ENGINE_DISPLAY_NAMES, SOURCE_DATABASE_NAME, display_engine

    sources = [*ENGINE_DISPLAY_NAMES.values(), SOURCE_DATABASE_NAME]

    for r in all_runs:
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        facts = (report.get("facts") or {}).get("table_sources") or {}
        rows = facts.get("replicated_tables") or []
        assert rows, r.db
        ev = _watch(page)
        _goto_results(page, ui, r, "data-model")
        section = page.locator("#table-owners-section")
        section.wait_for(timeout=READY_MS)
        assert page.locator("#sources-of-truth-section").count() == 1, r.db
        assert section.locator("#sources-of-truth-section").count() == 1, r.db
        assert "Sources of truth and replicas" not in page.locator("body").inner_text(), r.db
        first = rows[0]["table"]
        assert not section.locator(f'[data-table="{first}"]').is_visible(), r.db
        # Opened from the keyboard, as a reader without a mouse would.
        section.locator('[aria-expanded="false"]').first.focus()
        page.keyboard.press("Enter")
        section.locator('[aria-expanded="true"]').first.wait_for(timeout=READY_MS)
        text = section.inner_text()
        if facts["single_engine_table_count"] and "table_owner_lines" not in report["facts"]:
            assert ", ".join(facts["single_engine_tables"]) in text, r.db
        for row in rows:
            tr = section.locator(f'[data-table="{row["table"]}"]').locator("xpath=ancestor::tr")
            replicas = [display_engine(e) for e in row["replica_engines"]]
            assert tr.locator('[data-cell="owner"]').inner_text() == display_engine(
                row["source_of_truth"]
            ), (r.db, row["table"])
            assert (
                tr.locator('[data-cell="cdc-to"] [data-engine-badge]').all_inner_texts() == replicas
            ), (
                r.db,
                row["table"],
            )
            assert tr.locator('[data-cell="replica-reads"]').inner_text() == str(
                row["replica_read_count"]
            ), (r.db, row["table"])
            assert row["shares_replica_risk"], (r.db, row["table"])  # a fresh report's rows
            assert row["risk"] not in text, (r.db, row["table"])
        # The consistency note is behind the Info link, stated once.
        assert "Eventually consistent: " not in text, r.db
        section.get_by_role("button", name="Info about the consistency of replica reads").click()
        text = section.inner_text()
        assert facts["shared_risk_sentence"].startswith("Every replicated table above"), r.db
        assert text.count(facts["shared_risk_sentence"]) == 1, r.db
        assert text.count("Eventually consistent: ") == 1, r.db
        # #2321 review: the sentence sits under every row with a CDC to engine, also a
        # table nothing writes (owned by the relational step, CDC to DynamoDB), so it
        # names no source that disagrees with any such row's owner.
        shared = section.locator('[data-figure="shared-risk"]').inner_text()
        assert shared == facts["shared_risk_sentence"], r.db
        named = {n for n in sources if n.casefold() in shared.casefold()}
        replicated = 0
        for tr in section.locator("tbody tr").all():
            if tr.locator('[data-cell="cdc-to"] [data-engine-badge]').count() == 0:
                continue
            replicated += 1
            owner = tr.locator('[data-cell="owner"]').inner_text()
            table = tr.locator("[data-table]").inner_text()
            assert named <= {n for n in sources if n in owner}, (r.db, table, owner, named)
        assert replicated >= len(rows), (r.db, replicated)
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_results_page_states_each_tables_owner_cdc_replicas_and_cache(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#212: the results page states one line per mapped table, as report.json's
    facts.table_owner_lines and the engineering report's Migration map do: its owner,
    the engines that read a CDC copy of it and its cache, with the same counts. Collapsed
    when the page opens; a job with no table_mappings has no owner lines, so its table
    (#2321) lists the replicated tables only, with no cache column. On the designed
    wordpress job, wp_postmeta is owned by DynamoDB and also read by Aurora MySQL, and
    wp_wc_tax_rate_classes, which no query writes and only DynamoDB reads, stays with the
    relational step, Aurora MySQL, and is also read by DynamoDB (#2166, ADR-033 Amendment
    1; wp_usermeta until #1893, then wp_users until the refreshed sample of #1955 wrote
    it; tests/e2e/test_table_owners.py). On the same job on the collection bundled before
    #1955, wp_users, which no query writes there, is owned by Aurora MySQL and also read
    by DynamoDB."""
    from src.shared.engine_names import display_engine

    runs = list(all_runs)
    pre_1955 = None
    if not any(r.external for r in all_runs):
        # The designed job on the collection bundled before #1955, built into the UI's
        # artifact root: its wp_users, which no query writes, is owned by Aurora MySQL
        # and also read by DynamoDB, a row the refreshed sample has no table for.
        from tests.e2e.designed_run import run_pre_1955_designed_wordpress

        pre_1955 = run_pre_1955_designed_wordpress(all_runs[0].artifact_root, "ui-pre1")
        runs.append(pre_1955)
    designed = _designed_runs(runs)
    assert designed, "no job with a schema design in this session"
    for r in runs:
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        facts = (report.get("facts") or {}).get("table_owner_lines")
        ev = _watch(page)
        _goto_results(page, ui, r, "data-model")
        section = page.locator("#table-owners-section")
        if r not in designed:
            assert facts is None, r.job_id
            assert section.locator('[data-figure="owned-tables"]').count() == 0, r.job_id
            assert section.get_by_role("columnheader", name="Cached by").count() == 0, r.job_id
            continue
        n = facts["table_count"]
        assert f"Table owners ({n} mapped tables)" in section.inner_text(), r.job_id
        first = facts["table_lines"][0]["table"]
        assert not section.locator(f'[data-table="{first}"]').is_visible(), r.job_id
        section.locator('[aria-expanded="false"]').first.focus()
        page.keyboard.press("Enter")
        section.locator('[aria-expanded="true"]').first.wait_for(timeout=READY_MS)
        owned = section.locator('[data-figure="owned-tables"] [data-engine]').all_inner_texts()
        assert owned == [
            f"{display_engine(e)} owns {k}" for e, k in facts["owned_table_counts"].items()
        ], (r.job_id, owned)
        k = facts["also_read_table_count"]
        also = section.locator('[data-figure="also-read-tables"]').inner_text()
        assert also.startswith(f"{k} {'table is' if k == 1 else 'tables are'} also read"), also
        k = facts["cached_table_count"]
        cached = section.locator('[data-figure="cached-tables"]')
        if k:
            text = cached.inner_text()
            assert text.startswith(f"{k} {'table is' if k == 1 else 'tables are'} cached by"), text
        else:
            assert cached.count() == 0, r.job_id
        # A listed replicated table whose source and replicas are the line's owner and CDC
        # engines counts the same reads (#2321).
        replica_reads = {
            (x["table"], x["source_of_truth"], tuple(sorted(x["replica_engines"]))): x[
                "replica_read_count"
            ]
            for x in (report["facts"].get("table_sources") or {}).get("replicated_tables") or []
        }
        for line in facts["table_lines"]:
            row = section.locator(f'[data-table="{line["table"]}"]').locator("xpath=ancestor::tr")
            owner = display_engine(line["owner"]) if line["owner"] else "—"
            readers = [display_engine(e) for e in line["also_read_by"]]
            cache = display_engine(line["cached_by"]) if line["cached_by"] else "—"
            assert row.locator('[data-cell="owner"]').inner_text() == owner, line
            cdc = row.locator('[data-cell="cdc-to"]')
            assert (cdc.locator("[data-engine-badge]").all_inner_texts() or ["—"]) == (
                readers or ["—"]
            ), line
            assert row.locator('[data-cell="cached-by"]').inner_text() == cache, line
            # #2321: the reads a replica serves, the line's own count for every mapped
            # table (0: none), the same as facts.table_sources for a table it lists.
            reads = line["replica_read_count"]
            assert isinstance(reads, int), line
            key = (line["table"], line["owner"], tuple(sorted(line["also_read_by"])))
            assert replica_reads.get(key, reads) == reads, line
            assert row.locator('[data-cell="replica-reads"]').inner_text() == str(reads), line
        # #2321: the filter narrows the rows to the tables whose name or engines match.
        wanted = facts["table_lines"][0]["table"]
        section.get_by_placeholder("Find a table").fill(wanted)
        shown = section.locator("tbody [data-table]").all_inner_texts()
        assert wanted in shown and set(shown) <= {
            x["table"] for x in facts["table_lines"] if wanted.lower() in x["table"].lower()
        }, (r.job_id, shown)
        section.get_by_placeholder("Find a table").fill("")
        if not r.external:
            postmeta = section.locator('[data-table="wordpress.wp_postmeta"]').locator(
                "xpath=ancestor::tr"
            )
            assert postmeta.locator('[data-cell="owner"]').inner_text() == "DynamoDB"
            assert postmeta.locator('[data-cell="cdc-to"]').inner_text() == "Aurora MySQL"
            if r is pre_1955:
                users = section.locator('[data-table="wordpress.wp_users"]').locator(
                    "xpath=ancestor::tr"
                )
                assert users.locator('[data-cell="owner"]').inner_text() == "Aurora MySQL"
                assert users.locator('[data-cell="cdc-to"]').inner_text() == "DynamoDB"
            else:
                unwritten = section.locator(
                    '[data-table="wordpress.wp_wc_tax_rate_classes"]'
                ).locator("xpath=ancestor::tr")
                # #2166: no query writes it, so the relational step keeps it.
                assert unwritten.locator('[data-cell="owner"]').inner_text() == "Aurora MySQL"
                assert unwritten.locator('[data-cell="cdc-to"]').inner_text() == "DynamoDB"
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.job_id, ev)


def _download_menu(page: Page) -> Locator:
    """The results page header's one Download menu (#2314), opened."""
    header = page.locator("#deliverables-section")
    trigger = header.get_by_role("button", name="Download", exact=True)
    expect(trigger).to_be_enabled(timeout=READY_MS)
    trigger.click()
    menu = header.get_by_role("menu")
    expect(menu).to_be_visible(timeout=READY_MS)
    return header


def test_results_page_downloads_the_deliverables_folder(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#957, #1028, #2314: the results page header has one Download menu. It lists every
    deliverable the job's deliverables/ folder holds, in FOLDER_ENTRIES' order, by the
    entry's title (which names the file type, the detailed engineering report included),
    and each item downloads its file as it is on disk. The folder index has no item."""
    for r in all_runs:
        folder = r.job_dir() / DELIVERABLES_DIR
        entries = [e for e in FOLDER_ENTRIES if (folder / e.filename).is_file()]
        assert [e.filename for e in entries] == [e.filename for e in FOLDER_ENTRIES], r.db
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results/{r.job_id}")
        _loaded(page, r)
        # The old three buttons are gone; the menu is the one download control.
        for old in ("Executive Summary", "Engineering Report", "Assessment Detail"):
            assert page.get_by_role("link", name=old, exact=True).count() == 0, (r.db, old)
        section = _download_menu(page)
        rows = section.locator("[data-deliverable]")
        assert [x.get_attribute("data-deliverable") for x in rows.all()] == [
            e.filename for e in entries
        ], r.db
        assert "Detailed engineering report (Markdown)" in [e.title for e in entries]
        for row, entry in zip(rows.all(), entries, strict=True):
            text = row.inner_text()
            assert entry.title in text and entry.filename in text, (r.db, text)
        assert "index.md" not in section.inner_text(), r.db
        for i, entry in enumerate(entries):
            if i:
                _download_menu(page)
            with page.expect_download() as info:
                section.get_by_role(
                    "menuitem", name=re.compile(rf"^Download {re.escape(entry.filename)},")
                ).click()
            download = info.value
            assert download.suggested_filename == entry.filename, (r.db, entry.filename)
            assert Path(download.path()).read_bytes() == (folder / entry.filename).read_bytes(), (
                r.db,
                entry.filename,
            )
        # A download ends its navigation, which the browser reports as a failed
        # request for exactly that file; anything else failing is a real error.
        downloads = {f"{r.job_id}/deliverables/{e.filename}" for e in entries}
        ev["failed"] = [u for u in ev["failed"] if u.split("/assessments/", 1)[-1] not in downloads]
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def _source_label(r: PipelineResult) -> str | None:
    from src.shared.engine_names import source_engine_label

    collector = json.loads((r.job_dir() / "collector" / "output.json").read_text())
    return source_engine_label(collector["metadata"]["source_database"]["engine"])


def _report_date(r: PipelineResult) -> str:
    """The report's timestamp date as the header prints it ("Nov 18, 2026")."""
    import datetime as dt

    day = dt.date.fromisoformat(str(json.loads(r.report_json().read_text())["timestamp"])[:10])
    return f"{day.strftime('%b')} {day.day}, {day.year}"


def test_results_page_header_names_the_assessment(
    loaded_results: OpenResults, all_runs: list[PipelineResult]
) -> None:
    """#2314: the h1 names the assessment ("wordpress assessment"); its description states
    the source engine, the assessment id with a copy button and the report's date (the
    executive report cover's); the breadcrumb reads Assessments > the database; the top
    navigation's Help link opens the help site's results page in a new tab."""
    for r in all_runs:
        page, ev = loaded_results(r)
        h1 = page.get_by_role("heading", level=1)
        expect(h1).to_have_count(1)
        expect(h1).to_have_text(f"{r.db} assessment")
        header = page.locator("#results-header")
        text = " ".join(header.inner_text().split())
        source = _source_label(r)
        assert source, r.db
        assert f"{source} source" in text, (r.db, text)
        assert f"Assessment {r.job_id}" in text, (r.db, text)
        assert _report_date(r) in text, (r.db, text)
        # The same day the executive report's cover prints.
        from tests.e2e.deliverable_figures import pdf_page_texts

        iso = str(json.loads(r.report_json().read_text())["timestamp"])[:10]
        cover = pdf_page_texts(str(r.deliverable("executive-report.pdf")))[0]
        assert f"Generated {iso}" in " ".join(cover.split()), (r.db, iso)
        assert (
            header.get_by_role(
                "button", name=re.compile(r"^Copy assessment id .*" + re.escape(r.job_id))
            ).count()
            == 1
        )
        crumbs = page.get_by_role("navigation", name=re.compile("Breadcrumb", re.I))
        expect(crumbs.get_by_role("link", name="Assessments")).to_have_attribute(
            "href", "/dashboard"
        )
        assert crumbs.inner_text().split("\n")[-1].strip() == r.db, crumbs.inner_text()
        assert "Home" not in crumbs.inner_text() and "Results" not in crumbs.inner_text()
        help_link = page.locator("header").get_by_role("link", name=re.compile("^Help")).first
        expect(help_link).to_have_attribute(
            "href",
            "https://aws-samples.github.io/sample-aws-genai-db-modernizer/understand-the-results/#the-results-page",
        )
        expect(help_link).to_have_attribute("target", "_blank")
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_an_old_job_without_source_date_or_detailed_report_still_names_itself(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#2314: a job saved before the source engine was on record, whose report has no
    timestamp and whose folder has no detailed report, still names itself in the h1 and
    offers what its folder holds; the parts it has no record of are left out."""
    r = all_runs[0]
    job = re.escape(r.job_id)

    def no_source(route: playwright.sync_api.Route) -> None:
        response = route.fetch()
        body = response.json()
        body["source_database_type"] = None
        body["source_database_label"] = None
        route.fulfill(response=response, json=body)

    def no_timestamp(route: playwright.sync_api.Route) -> None:
        response = route.fetch()
        body = response.json()
        body["synthesis"].pop("timestamp", None)
        route.fulfill(response=response, json=body)

    def no_detailed(route: playwright.sync_api.Route) -> None:
        response = route.fetch()
        body = response.json()
        body["files"] = [
            f for f in body["files"] if f["filename"] != "engineering-report-detailed.md"
        ]
        route.fulfill(response=response, json=body)

    page.route(re.compile(rf"/api/v1/assessments/{job}$"), no_source)
    page.route(re.compile(rf"/api/v1/assessments/{job}/results$"), no_timestamp)
    page.route(re.compile(rf"/api/v1/assessments/{job}/deliverables$"), no_detailed)
    ev = _watch(page)
    page.goto(f"{ui}/analysis/results/{r.job_id}")
    _loaded(page, r)
    h1 = page.get_by_role("heading", level=1)
    expect(h1).to_have_text(f"{r.db} assessment")
    header = page.locator("#results-header")
    text = " ".join(header.inner_text().split())
    assert f"Assessment {r.job_id}" in text, text
    assert " source" not in text and _report_date(r) not in text, text
    rows = _download_menu(page).locator("[data-deliverable]")
    assert [x.get_attribute("data-deliverable") for x in rows.all()] == [
        "executive-report.pdf",
        "engineering-report.md",
        "assessment-data.json",
    ]
    assert ev == {"console": [], "pageerror": [], "failed": []}, ev


@pytest.mark.parametrize("width", [1440, 390])
def test_results_page_header_and_download_menu_have_no_accessibility_violations(
    page: Page, ui: str, all_runs: list[PipelineResult], width: int
) -> None:
    """#2314: with the Download menu open, axe finds nothing blocking on the results page
    at a desktop and a phone width, and the header never makes the page scroll sideways."""
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    r = all_runs[0]
    page.goto(f"{ui}/analysis/results/{r.job_id}")
    _loaded(page, r)
    assert _page_fits_viewport(page), (width, "the header scrolls the page sideways")
    _download_menu(page)
    page.evaluate("Promise.all(document.getAnimations().map((a) => a.finished))")
    blocking, where, _ = _axe_blocking(page)
    assert blocking == [], (width, blocking, where)
    assert _page_fits_viewport(page), (width, "the open menu scrolls the page sideways")


# #1028, #2313: the deliverables sit above the tabs; each tab lists its sections in
# TAB_SECTIONS' order. These are on every job's page; the others are banners a report
# without fallback-engine queries, close calls, routines or merge decisions leaves out,
# the explorer (a job with no access patterns), the Table design (a
# job with no schema design), the assumptions panel (no assumption to change), the step
# gate (no step 1 evidence) and the two Plan notes.
REQUIRED_SECTIONS = {
    "executive-summary-section",
    "cost-breakdown-section",
    "query-flow-section",
    "migration-roadmap-section",
    "query-routing-section",
    "table-owners-section",
    "risk-assessment-section",
}


def test_results_page_states_the_price_date(
    loaded_results: OpenResults, all_runs: list[PipelineResult]
) -> None:
    """#1196: the projected-cost tile states the same price date report.json's
    facts.price_list holds (the line the PDF and engineering reports print)."""
    from src.report.formatting import price_list_text

    for r in all_runs:
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        assert (report.get("facts") or {}).get("price_list"), r.db
        page, ev = loaded_results(r)
        _show_tab(page, "summary")
        # The figure itself, not network silence (#1593): the page is loaded once
        # the figure renders, whatever else is still in flight.
        shown = page.locator("[data-figure=price-list]")
        shown.wait_for(timeout=READY_MS)
        assert shown.inner_text().strip() == f"List prices: {price_list_text(report['facts'])}"
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_results_page_states_what_each_engine_was_sized_for(
    loaded_results: OpenResults, all_runs: list[PipelineResult]
) -> None:
    """#1195: each cost row states what its figure was sized for, as report.json's
    facts.engines[].cost_sizing holds it (the reports' "Sized for" column): its query
    count and call rate in their columns, and the basis in the last."""
    from src.report.formatting import fmt_num

    for r in all_runs:
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        sizing = {
            e["engine"]: e["cost_sizing"]
            for e in report["facts"]["engines"]
            if e.get("cost_sizing") and e.get("monthly_cost_usd") is not None
        }
        assert sizing, r.db
        page, ev = loaded_results(r)
        _show_tab(page, "summary")
        page.locator("[data-figure=cost-sizing]").first.wait_for(timeout=READY_MS)
        for engine, basis in sizing.items():
            # #2316: the row names the basis; the queries and the call rate have columns.
            row = page.locator(f"#cost-breakdown-section tr:has([data-engine-badge={engine}])")
            cells = [c.strip() for c in row.locator("td").all_inner_texts()]
            assert cells[2] == fmt_num(basis.get("sized_query_count") or 0), (r.db, engine, cells)
            assert cells[3] == fmt_num(basis.get("calls_per_second") or 0), (r.db, engine, cells)
            assert cells[4] and not cells[4].startswith("Sized for"), (r.db, engine, cells)
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_results_page_shows_the_headline_first(
    loaded_results: OpenResults, all_runs: list[PipelineResult]
) -> None:
    """#1028, #2313: the results page opens on its Summary tab, under the header and its
    Download menu (#2314). Each tab holds its own sections, in the order of the decision (Summary: the
    executive summary, the cost breakdown, the query flow, the assumptions; Plan: the
    roadmap, then the fallback-engine warning and close-call banners when the report has
    them, ...; Queries: the open query table, then the explorer; Data model: the
    table owners, collapsed, then the Table design; Risks and
    decisions), and no other tab's. The close-call banner names its first 3 queries by a
    5-character id, then "+N more"."""
    in_order = (
        "ids => ids.map(id => document.getElementById(id))"
        "  .every((el, i, all) => i === 0 ||"
        "    (all[i - 1].compareDocumentPosition(el) & Node.DOCUMENT_POSITION_FOLLOWING))"
    )
    every_section = [i for ids in TAB_SECTIONS.values() for i in ids]
    for r in all_runs:
        page, ev = loaded_results(r)
        _show_tab(page, "summary")
        # The Download menu first, above the tabs.
        assert page.evaluate(
            "() => !!(document.getElementById('deliverables-section').compareDocumentPosition("
            "document.querySelector('[role=tablist][aria-label=\"Assessment results\"]'))"
            " & Node.DOCUMENT_POSITION_FOLLOWING)"
        ), r.db
        tabs = page.get_by_role("tablist", name="Assessment results").get_by_role("tab")
        facts = json.loads((r.artifact_root / r.report["report"]).read_text())["facts"]
        counts = facts.get("results_tab_counts") or {}
        # #2363: each label carries the count the facts state for it, and no other.
        expected = [
            f"{label} ({counts[tab]:,})" if tab in counts else label
            for tab, label in RESULTS_TABS.items()
        ]
        assert tabs.all_inner_texts() == expected, r.db
        assert {"queries", "risks"} <= set(counts), r.db
        present_anywhere: set[str] = set()
        for tab in _each_tab(page):
            present, _ = _settled_sections(page, tab)
            assert page.evaluate(in_order, present), (r.db, tab, present)
            for section_id in present:
                expect(page.locator(f"#{section_id}")).to_be_visible()
            # A section of another tab is never shown here (a tab opened before stays
            # mounted, hidden, so it may be in the DOM but not visible).
            for section_id in every_section:
                if section_id not in TAB_SECTIONS[tab]:
                    assert not page.locator(f"#{section_id}").is_visible(), (r.db, tab, section_id)
            present_anywhere |= set(present)
        assert REQUIRED_SECTIONS <= present_anywhere, (r.db, REQUIRED_SECTIONS - present_anywhere)
        if not r.external and r.db == "wordpress":
            assert "close-calls-section" in present_anywhere, (
                r.db,
                "the wordpress sample has close calls",
            )
        _show_tab(page, "queries")
        routing = page.locator("#query-routing-section")
        # #2319: the query table is open, with nothing to expand first.
        assert routing.locator('button[aria-expanded="false"]').count() == 0, r.db
        routing.get_by_role("table", name="Query routing").wait_for(timeout=READY_MS)
        assert routing.get_by_role("table", name="Query routing").is_visible(), r.db
        _show_tab(page, "data-model")
        owners = page.locator("#table-owners-section")
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        # Closed: the section's toggle, and the Info link when a table has a replica.
        info = bool(
            ((report.get("facts") or {}).get("table_sources") or {}).get("replicated_tables")
        )
        assert owners.locator('[aria-expanded="false"]').count() == 1 + info, r.db
        assert owners.locator('[aria-expanded="true"]').count() == 0, r.db
        # #2321: its replicas are columns of that table; no second section lists them,
        # and an old link to that section lands inside Table owners.
        assert page.locator("#sources-of-truth-section").count() == 1, r.db
        assert owners.locator("#sources-of-truth-section").count() == 1, r.db
        close_calls = (report.get("facts") or {}).get("close_calls") or {}
        pairs = close_calls.get("engine_pairs") or []
        if pairs and close_calls.get("close_call_query_count"):
            _show_tab(page, "plan")
            _assert_close_calls_table(page, report, pairs, r.db)
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def _assert_close_calls_table(page: Page, report: dict, pairs: list[dict], db: str) -> None:
    """#2318: the close calls are one table of their own, a row per listed query: its id
    as the routing table shows it (eight characters, never a negative-looking number), the
    assigned engine, the runner-up, the report's gap and what settled it. An id opens the
    query's decision trace."""
    from src.shared.engine_names import display_engine

    section = page.locator("#close-calls-section")
    table = section.get_by_role("table", name="Close calls")
    expect(table).to_be_visible(timeout=READY_MS)
    digest = report["query_traces"]["close_calls"]
    ordered = sorted(
        digest, key=lambda c: (bool(c.get("settled_tie")), c["gap"], str(c["query_id"]))
    )
    rows = table.locator("tbody tr")
    assert rows.count() == min(25, len(ordered)), db
    first = ordered[0]
    cells = [c.strip() for c in rows.first.locator("td").all_inner_texts()]
    assert cells[0] == first["query_id"][:8], (db, cells)
    assert cells[2] == display_engine(first["engine"]), (db, cells)
    assert cells[3] == display_engine(first["alternative"]), (db, cells)
    assert cells[4] == f"{first['gap']:.1f}", (db, cells)
    settled = (
        "Tie rule"
        if first.get("settled_tie")
        else "Group score" if first.get("basis") == "group_mean" else "Score"
    )
    assert cells[5] == settled, (db, cells)
    # No explanation paragraphs, no "Closest:" lists and no "+N more" toggles around it.
    assert "Closest:" not in section.inner_text(), db
    assert section.get_by_role("button", name=re.compile(r"^\+\d+ more")).count() == 0, db
    opener = section.get_by_role(
        "button", name=f"Open the decision trace for query {first['query_id']}"
    )
    assert opener.count() == 1, db
    assert section.get_by_role("link", name="Open the assignment review").count() == 1, db
    heading = section.get_by_role("heading", name=re.compile("^Close calls"))
    assert heading.count() == 1, db
    # One count, of the table's rows: the close calls and the ties it lists.
    assert f"({len(digest):,})" in heading.inner_text(), db


def test_results_page_on_this_page_lists_the_open_tabs_sections(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#2313: at 1440 px the tools panel is open on "On this page", listing the open tab's
    sections in the tab's order, and only those; following an anchor scrolls to its
    section, focuses it and names it in the URL. A tab with one section or none has no
    such panel."""
    page.set_viewport_size({"width": 1440, "height": 900})
    listed_any = 0
    for r in all_runs:
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results/{r.job_id}")
        _loaded(page, r)
        for tab in _each_tab(page):
            _, listed = _settled_sections(page, tab)
            nav = page.get_by_role("navigation", name="On this page")
            if len(listed) < 2:
                expect(nav).to_have_count(0)
                continue
            expect(nav).to_be_visible(timeout=READY_MS)
            expect(nav.get_by_role("link")).to_have_count(len(listed))
            hrefs = [a.get_attribute("href") for a in nav.get_by_role("link").all()]
            assert hrefs == [f"#{i}" for i in listed], (r.db, tab, hrefs)
            last = listed[-1]
            nav.get_by_role("link").last.click()
            expect(page).to_have_url(re.compile(rf"tab={tab}#{last}$"))
            # The page scrolls and focuses on the next frame, so the check waits for it.
            expect(page.locator(f"#{last}")).to_be_focused()
            expect(_results_tab(page, tab)).to_have_attribute("aria-selected", "true")
            listed_any += 1
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)
    assert listed_any, "no tab of any job lists two sections"


def test_settled_sections_wait_for_a_slow_assumptions_request(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#2394: the assumptions panel loads its own data, so on a loaded runner it joins the
    Summary tab long after the page's other sections. The settle helper must wait for that
    request, not for a quiet period: with the request held for 3 s, the panel is still in
    the settled snapshot and in the On this page links."""
    page.set_viewport_size({"width": 1440, "height": 900})

    def slow(route: playwright.sync_api.Route) -> None:
        page.wait_for_timeout(3000)
        route.continue_()

    page.route("**/assumptions", slow)
    r = all_runs[0]
    page.goto(f"{ui}/analysis/results/{r.job_id}")
    _loaded(page, r)
    present, listed = _settled_sections(page, "summary")
    assert "assumptions-panel" in present, present
    assert "assumptions-panel" in listed, listed


def test_an_old_results_anchor_opens_its_sections_tab(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#2313: a link made before the tabs, to a section by its id, opens the section's
    tab, names the tab in the URL, scrolls to the section and focuses it."""
    r = all_runs[0]
    for tab, ids in TAB_SECTIONS.items():
        target = TAB_READY[tab][1:]
        assert target in ids
        ev = _watch(page)
        page.goto(f"{ui}/analysis/results/{r.job_id}#{target}")
        expect(page.locator(f"#{target}")).to_be_visible(timeout=READY_MS)
        expect(_results_tab(page, tab)).to_have_attribute("aria-selected", "true")
        expect(page).to_have_url(re.compile(rf"/analysis/results/{r.job_id}\?tab={tab}#{target}$"))
        expect(page.locator(f"#{target}")).to_be_focused(timeout=READY_MS)
        assert ev == {"console": [], "pageerror": [], "failed": []}, (tab, ev)


def test_results_page_states_the_resolved_risks_by_severity(
    loaded_results: OpenResults, all_runs: list[PipelineResult]
) -> None:
    """#1020: the risk section's resolved-risks header reads the stored
    facts.resolved_risks_by_severity, as the executive and engineering reports do
    ("6 risks resolved by the assignment: 2 high, 4 medium")."""
    checked = 0
    for r in all_runs:
        facts = json.loads((r.artifact_root / r.report["report"]).read_text())["facts"]
        by_severity = facts.get("resolved_risks_by_severity") or {}
        if not by_severity:
            continue
        n = facts["resolved_risk_count"]
        severities = ", ".join(f"{k} {s.lower()}" for s, k in by_severity.items())
        noun = "risk" if n == 1 else "risks"
        expected = f"{n} {noun} resolved by the assignment: {severities}"
        md = r.deliverable("engineering-report.md").read_text(encoding="utf-8")
        assert f"({severities})" in md, r.db
        page, ev = loaded_results(r)
        _show_tab(page, "risks")
        # The risk section itself, not file order: this test may run first (-k).
        page.get_by_role("heading", name="Risk assessment").wait_for(timeout=READY_MS)
        header = page.get_by_text(expected, exact=True)
        assert header.count() == 1, (r.db, expected)
        checked += 1
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)
    assert checked, "no sample resolved a risk"


def test_results_page_states_the_routine_outcomes(
    loaded_results: OpenResults, all_runs: list[PipelineResult]
) -> None:
    """#1954: the results page states each sample's routine headline and to-do as
    report.json's ``facts.routines`` holds them (the lines the engineering report
    prints); a report with no routines has no Routines section."""
    checked = 0
    for r in all_runs:
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        routines = (report.get("facts") or {}).get("routines")
        if r.job_id in ("ui-word", "ui-disc"):
            assert routines and routines["migrate_yourself_line"], (r.db, "the sample's routines")
        page, ev = loaded_results(r)
        _show_tab(page, "plan")
        section = page.locator("#routines-section")
        if not routines:
            assert section.count() == 0, r.db
            continue
        # #2318: the routines are in wave 1 of the roadmap, not a section of their own.
        first_wave = page.locator("#migration-roadmap-section [data-wave]").first
        assert first_wave.locator("#routines-section").count() == 1, r.db
        line = section.locator("[data-figure=routines-line]")
        assert line.inner_text().strip() == routines["line"], r.db
        to_do = routines.get("migrate_yourself_line")
        shown = section.locator("[data-figure=routines-to-do-line]")
        if to_do:
            assert shown.inner_text().strip() == f"{to_do[0].upper()}{to_do[1:]}.", r.db
        else:
            assert shown.count() == 0, r.db
        checked += 1
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)
    assert checked or any(r.external for r in all_runs), "no sample has routines"


def test_results_page_states_the_distinct_query_patterns(
    loaded_results: OpenResults, all_runs: list[PipelineResult]
) -> None:
    """#1146: the results page prints "N queries in M distinct query patterns" from
    facts.query_pattern_counts, the same figures the engineering report prints. #2315: on
    the Queries tab, above the query table, not in the executive summary."""
    figure = re.compile(r"([\d,]+) quer(?:y|ies) in ([\d,]+) distinct query patterns?")
    for r in all_runs:
        facts = json.loads((r.artifact_root / r.report["report"]).read_text())["facts"]
        counts = facts["query_pattern_counts"]
        expected = (counts["query_count"], counts["pattern_count"])
        md = r.deliverable("engineering-report.md").read_text(encoding="utf-8")
        found = figure.search(md)
        assert found and tuple(int(g.replace(",", "")) for g in found.groups()) == expected, r.db
        page, ev = loaded_results(r)
        _show_tab(page, "summary")
        page.locator('[data-figure="decision-headline"]').wait_for(timeout=READY_MS)
        assert (
            page.locator('#executive-summary-section [data-figure="query-patterns"]').count() == 0
        )
        _show_tab(page, "queries")
        line = page.locator('#query-routing-section [data-figure="query-patterns"]')
        line.wait_for(timeout=READY_MS)
        shown = figure.fullmatch(line.inner_text().strip())
        assert shown, (r.db, line.inner_text())
        assert tuple(int(g.replace(",", "")) for g in shown.groups()) == expected, r.db
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_results_page_summary_leads_with_the_decision_and_five_figures(
    loaded_results: OpenResults, all_runs: list[PipelineResult]
) -> None:
    """#2315: the executive summary's header is the decision line the facts word
    (facts.executive_brief.decision_headline), the one the chat summary opens with, then one
    row of five figures, each the fact formatted, then the summary's paragraphs under Why."""
    from src.report.facts import report_facts
    from src.report.printed_summary import prose, render_final_summary

    for r in all_runs:
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        facts = report_facts(report)
        headline = facts["executive_brief"]["decision_headline"]
        assert headline, r.db
        printed = render_final_summary(
            report, database_name=r.db, job_id=r.job_id, deliverables_dir="d/", deliverables=[]
        )
        assert printed.splitlines()[2] == f"**{prose(headline)}**", r.db
        page, ev = loaded_results(r)
        _show_tab(page, "summary")
        section = page.locator("#executive-summary-section")
        shown = section.locator('[data-figure="decision-headline"]')
        shown.wait_for(timeout=READY_MS)
        assert shown.inner_text().strip() == headline, r.db
        assert section.get_by_role("heading", level=2).first.inner_text().strip() == headline
        tiles = section.locator("[data-tile]").evaluate_all(
            "els => els.map(e => e.getAttribute('data-tile'))"
        )
        assert tiles == ["projected-cost", "overall-risk", "waves", "queries", "target-engines"]

        def figure(name: str, section: Locator = section) -> str:
            return str(section.locator(f'[data-figure="{name}"]').inner_text()).strip()

        assert figure("projected-cost") == f"${facts['projected_monthly_cost_usd']:,.2f}/mo", r.db
        assert figure("wave-count") == str(facts["migration_wave_count"]), r.db
        assert figure("query-count") == f"{facts['query_total_count']:,}", r.db
        level = facts["executive_brief"]["overall_risk_level"]
        assert figure("overall-risk").lower() == level.lower(), r.db
        assert figure("open-risks").startswith(f"{facts['open_risk_count']} open risk"), r.db
        for name in ("access-patterns", "query-patterns", "database-name"):
            assert section.locator(f'[data-figure="{name}"]').count() == 0, (r.db, name)
        why = section.locator('[data-section="executive-summary-why"]')
        assert why.is_visible(), r.db
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


@pytest.mark.parametrize("width", [1440, 390])
def test_results_page_summary_figures_share_one_row_on_desktop(
    page: Page, ui: str, all_runs: list[PipelineResult], width: int
) -> None:
    """#2315 (#2458 review): at 1440 px the five figures share one row, the same top edge,
    left to right in order; at 390 px they stack, one per row. axe finds nothing blocking
    on the Summary tab at either width, the tab list's scroll buttons and the tables' scroll
    regions included."""
    page.set_viewport_size({"width": width, "height": 900})
    for r in all_runs:
        ev = _open_results(page, ui, r)
        _show_tab(page, "summary")
        section = page.locator("#executive-summary-section")
        section.locator('[data-tile="target-engines"]').wait_for(timeout=READY_MS)
        boxes = section.locator("[data-tile]").evaluate_all(
            "els => els.map(e => { const b = e.getBoundingClientRect();"
            " return [Math.round(b.top), Math.round(b.left)]; })"
        )
        assert len(boxes) == 5, (r.db, boxes)
        tops = [top for top, _ in boxes]
        lefts = [left for _, left in boxes]
        if width == 1440:
            assert len(set(tops)) == 1, (r.db, boxes)
            assert lefts == sorted(lefts) and len(set(lefts)) == 5, (r.db, boxes)
        else:
            assert tops == sorted(tops) and len(set(tops)) == 5, (r.db, boxes)
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        blocking, where, _ = _axe_blocking(page)
        assert blocking == [], (r.db, width, blocking, where)
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_results_page_states_the_cost_basis_the_deliverables_state(
    loaded_results: OpenResults, all_runs: list[PipelineResult]
) -> None:
    """#1197: the read I/O basis DocumentDB's estimate rests on (the source's hit ratio,
    its memory scaling and the billed-miss floor) is one report fact,
    ``facts.cost_basis_notes``; the results page, the engineering report and the PDF
    print the same sentences."""
    from tests.e2e.deliverable_figures import md_plain, pdf_page_texts

    checked = 0
    for r in all_runs:
        notes = json.loads((r.artifact_root / r.report["report"]).read_text())["facts"].get(
            "cost_basis_notes"
        )
        if not notes:
            # No note is the right answer for a source with no cost basis to state (a
            # wordpress model run has no DocumentDB estimate): then nothing is printed.
            md = r.deliverable("engineering-report.md").read_text(encoding="utf-8")
            assert "- Cost basis:" not in md, r.db
            page, ev = loaded_results(r)
            _show_tab(page, "summary")
            assert page.locator('[data-figure="cost-basis"]').count() == 0, r.db
            assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)
            continue
        md = md_plain(r.deliverable("engineering-report.md").read_text(encoding="utf-8"))
        pdf = " ".join(" ".join(pdf_page_texts(str(r.deliverable("executive-report.pdf")))).split())
        page, ev = loaded_results(r)
        _show_tab(page, "summary")
        # #2316: the cost notes sit in one expandable that starts closed.
        page.get_by_text("How these costs are estimated").click()
        lines = page.locator('[data-figure="cost-basis"]')
        lines.first.wait_for(timeout=READY_MS)
        assert [t.strip() for t in lines.all_inner_texts()] == notes, r.db
        for note in notes:
            assert f"- Cost basis: {note}" in md, (r.db, note)
            assert note in pdf, (r.db, note)
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)
        checked += 1
    # The deterministic samples include one that states a basis; an external (model-run)
    # job is a single source, which may state none.
    assert checked or any(r.external for r in all_runs), "no sample states a cost basis"


def test_results_page_states_the_no_open_risks_caveat_only_when_no_risk_is_open(
    loaded_results: OpenResults, all_runs: list[PipelineResult]
) -> None:
    """#996: the sentence the engineering report prints under "no open risks" is on the
    results page too, in the risk section, and only for a report with no open risk."""
    from src.report.risks import NO_OPEN_RISKS_CAVEAT

    for r in all_runs:
        facts = json.loads((r.artifact_root / r.report["report"]).read_text())["facts"]
        no_open = facts["open_risk_count"] == 0
        md = r.deliverable("engineering-report.md").read_text(encoding="utf-8")
        assert (NO_OPEN_RISKS_CAVEAT in md) == no_open, r.db
        page, ev = loaded_results(r)
        _show_tab(page, "risks")
        # The risk section itself: a count of 0 must not come from a page not drawn yet.
        page.get_by_role("heading", name="Risk assessment").wait_for(timeout=READY_MS)
        note = page.locator("#no-open-risks-note")
        assert note.count() == (1 if no_open else 0), r.db
        if no_open:
            assert note.inner_text().strip() == NO_OPEN_RISKS_CAVEAT, r.db
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_results_page_states_the_caveat_even_with_no_other_risk_content(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#996: a report with no risks, no resolved risks and no overall level still gets a
    risk section holding the no-open-risks sentence, as the engineering report prints it."""
    from src.report.risks import NO_OPEN_RISKS_CAVEAT

    r = all_runs[0]

    def strip_risks(route: object) -> None:
        response = route.fetch()  # type: ignore[attr-defined]
        body = response.json()
        body["synthesis"]["risk_assessment"] = {}
        body["synthesis"]["facts"].update(open_risk_count=0, resolved_risk_count=0)
        route.fulfill(response=response, json=body)  # type: ignore[attr-defined]

    page.route(re.compile(rf"/assessments/{r.job_id}/results$"), strip_risks)
    ev = _watch(page)
    _goto_results(page, ui, r, "risks")
    note = page.locator("#no-open-risks-note")
    assert note.count() == 1
    assert note.inner_text().strip() == NO_OPEN_RISKS_CAVEAT
    assert page.get_by_role("heading", name="Risk assessment").count() == 1
    assert ev == {"console": [], "pageerror": [], "failed": []}, ev


def test_results_page_names_a_resolved_risks_tables(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#950: a resolved risk keeps naming its tables on the results page, as the
    engineering report's "Affects" line does."""
    checked = 0
    for r in all_runs:
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        facts = report["facts"]
        resolved = [
            x
            for x in (report.get("risk_assessment") or {}).get("resolved_risks") or []
            if x.get("affected_tables")
        ]
        if not resolved or not facts.get("resolved_risks_by_severity"):
            continue
        n = facts["resolved_risk_count"]
        severities = ", ".join(
            f"{k} {s.lower()}" for s, k in facts["resolved_risks_by_severity"].items()
        )
        header = f"{n} {'risk' if n == 1 else 'risks'} resolved by the assignment: {severities}"
        ev = _watch(page)
        _goto_results(page, ui, r, "risks")
        page.get_by_text(header, exact=True).click()
        line = f"Affects: {', '.join(resolved[0]['affected_tables'])}"
        assert page.get_by_text(line, exact=True).count() >= 1, (r.db, line)
        checked += 1
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)
    assert checked, "no sample resolved a risk with tables"


def test_results_page_shows_the_same_risks_as_report_json(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#950 (ADR-032 Decision 5): the results page's severity tabs count what
    facts.open_risks_by_severity counts, and each tab lists its risks by id, the
    migration risks included."""
    for r in all_runs:
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        by_severity = report["facts"].get("open_risks_by_severity") or {}
        risks = report["risk_assessment"]["risks"]
        refs = shared_mitigation_refs(backfill_report_risks(report))
        ev = _watch(page)
        _goto_results(page, ui, r, "risks")
        for severity in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
            n = int(by_severity.get(severity) or 0)
            tab = page.get_by_role("tab", name=f"{severity.capitalize()} ({n})", exact=True)
            assert tab.count() == 1, (r.db, severity, n)
            if not n:
                continue
            tab.click()
            for risk in risks:
                if risk.get("severity") == severity:
                    shown = page.get_by_text(str(risk["risk_id"]), exact=True)
                    assert shown.count() >= 1, (r.db, risk["risk_id"])
                    # #1510: a mitigation an earlier risk states is "as for" it, as in
                    # both reports (one computation, ``shared_mitigation_refs``).
                    if risk["risk_id"] in refs:
                        # #2322: the sentence is in the card's body, so open the cards
                        # and check the reader can see it, not that a hidden node has it.
                        _expand_every_section(page)
                        same = page.get_by_text(f"as for {refs[risk['risk_id']]}.")
                        expect(same.first).to_be_visible(timeout=READY_MS)
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_results_page_states_the_sync_risks_cdc_copy_of_unwritten_tables(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#2238: a sync risk whose tables include ones no captured query writes says CDC
    keeps their copies current, as report.json and both reports do, and the results page
    shows that sentence and never "a one-time copy"."""
    checked = 0
    for r in all_runs:
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        sync = [
            x
            for x in report["risk_assessment"]["risks"]
            if x.get("rule") == "sync.wave" and "written by no captured query" in x["description"]
        ]
        if not sync:
            continue
        _goto_results(page, ui, r, "risks")
        for risk in sync:
            page.get_by_role(
                "tab", name=re.compile(rf"^{risk['severity'].capitalize()} \(")
            ).click()
            sentence = re.search(
                r"CDC keeps (?:its copy|their copies) current", risk["description"]
            )
            assert sentence, (r.db, risk["description"])
            # #2322: the lines sit in a card's nested Design notes; open them, then the
            # reader must be able to see the sentence.
            _expand_every_section(page)
            expect(page.get_by_text(sentence.group(0)).first).to_be_visible(timeout=READY_MS)
            assert page.get_by_text("one-time copy").count() == 0, (r.db, risk["risk_id"])
            checked += 1
    assert checked, "no sample's sync risk names an unwritten table"


def test_ui_shows_only_the_product_name(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#957: no page names "ATX", "AWS Transform" or the standalone word "transform"
    (``tests/branding.py``, #2185); the header says "Database Modernizer Assessment Tool"."""
    r = all_runs[0]
    for path in (
        "/dashboard",
        "/analysis/create",
        f"/analysis/monitor/summary/{r.job_id}",
        f"/analysis/results/{r.job_id}",
    ):
        page.goto(f"{ui}{path}")
        # Content only the loaded page shows, so the text scan below never reads a spinner.
        if path == "/dashboard":
            # #2305: a finished job is named by its database and opens on its results.
            expect(page.locator(f'a[href="/analysis/results/{r.job_id}"]').first).to_be_visible(
                timeout=READY_MS
            )
        elif path == "/analysis/create":
            expect(page.get_by_role("heading", name="New assessment")).to_be_visible(
                timeout=READY_MS
            )
        elif path.startswith("/analysis/monitor/summary/"):
            page.locator("[data-sankey-node]").first.wait_for(timeout=READY_MS)
        else:
            _loaded(page, r)
        if path.startswith("/analysis/results/"):
            # #2313: the results page's text is the text of all its tabs.
            text = "\n".join(page.inner_text("body") for _ in _each_tab(page))
        else:
            text = page.inner_text("body")
        # #2185: the one rule (tests/branding.py): "transformation" in model prose passes.
        assert product_name_hits(text) == [], path
        assert "Database Modernizer Assessment Tool" in text, path
        assert page.title() == "Database Modernizer Assessment Tool", path


# ---------------------------------------------------------------------------
# The results page's browser checks (#987): the retired HTML reports were
# rendered in a browser for leaked placeholders, the customer text guard,
# horizontal overflow and accessibility. The results page is the deliverable
# that replaces them, so it gets the same checks.
# ---------------------------------------------------------------------------

# Placeholder values ("null" as a cell, not in a sentence) are checked by tests/placeholders.py;
# "[object Object]" and "{{" are leaks anywhere.
FORBIDDEN_WORDS = ("undefined", "NaN", "null")
FORBIDDEN_SUBSTRINGS = ("[object Object]", "{{")

# axe impacts that fail the scan; "minor" findings are printed for visibility only.
BLOCKING_IMPACTS = ("critical", "serious", "moderate")


def _leaked_forbidden_text(text: str) -> list[str]:
    leaked = placeholder_leaks(text, FORBIDDEN_WORDS)
    leaked += [s for s in FORBIDDEN_SUBSTRINGS if s in text]
    return leaked


def _open_results(page: Page, ui: str, r: PipelineResult) -> dict[str, list[str]]:
    ev = _watch(page)
    page.goto(f"{ui}/analysis/results/{r.job_id}")
    _loaded(page, r)
    return ev


def test_results_page_text_is_clean(
    loaded_results: OpenResults, all_runs: list[PipelineResult]
) -> None:
    from src.tools.validation.customer_text_guard import scan_text

    for r in all_runs:
        page, ev = loaded_results(r)
        # #2313: the page's text is every tab's.
        for tab in _each_tab(page):
            text = page.inner_text("body")
            assert _leaked_forbidden_text(text) == [], (r.db, tab)
            findings = scan_text(text, f"results-page:{r.db}:{tab}")
            assert findings == [], (r.db, tab, [(f.kind, f.text, f.excerpt) for f in findings])
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_results_page_risk_text_is_clean(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#2322: a risk card's description, mitigation and design notes sit in collapsed
    sections. The shared page above reads them closed, so this test opens its own page,
    opens the Risks tab's sections and scans what a reader can open."""
    from src.tools.validation.customer_text_guard import scan_text

    for r in all_runs:
        ev = _watch(page)
        _goto_results(page, ui, r, "risks")
        _expand_every_section(page)
        text = page.inner_text("body")
        assert _leaked_forbidden_text(text) == [], r.db
        findings = scan_text(text, f"results-page:{r.db}:risks")
        assert findings == [], (r.db, [(f.kind, f.text, f.excerpt) for f in findings])
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


@pytest.mark.parametrize("width", [1280, 1920])
def test_results_page_has_no_horizontal_overflow(
    page: Page, ui: str, all_runs: list[PipelineResult], width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    for r in all_runs:
        _open_results(page, ui, r)
        for tab in _each_tab(page):
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"), (
                r.db,
                tab,
            )


def _expand_every_section(page: Page) -> None:
    """Open every collapsed section of the page's content (the Table design's
    tables and query groups, the explorer's groups, ...), so the scan sees what
    a reader can open, not only what shows on load.

    Each level's visible section headers are clicked in one ``evaluate`` (#1593): a
    large job has about 700 of them, and a Playwright click per header (an actionability
    wait and a round trip each) took over two minutes. The headers and the click order
    are the same. Visible means what Playwright's ``is_visible()`` checks: rendered
    (``checkVisibility`` with ``visibilityProperty``, so ``visibility: hidden`` is out)
    and a non-empty bounding box. ``click()`` fires the same click event the page's
    handlers open a section on. The check after the loop fails when a visible header
    is still closed, so a click that stopped opening sections cannot pass."""
    for _ in range(4):  # nested sections open one level at a time
        # Section headers only (they control their content); never a menu, a
        # dropdown or a popover trigger.
        clicked = page.evaluate("""() => {
              const isVisible = (el) => {
                const box = el.getBoundingClientRect();
                return el.checkVisibility({visibilityProperty: true})
                  && box.width > 0 && box.height > 0;
              };
              const closed = [...document.querySelectorAll(
                'main [aria-expanded="false"][aria-controls]:not([aria-haspopup])')];
              const visible = closed.filter(isVisible);
              visible.reverse().forEach((el) => el.click());
              return visible.length;
            }""")
        if not clicked:
            break
        page.wait_for_timeout(200)
    still_closed = page.locator(
        'main [aria-expanded="false"][aria-controls]:not([aria-haspopup])'
    ).evaluate_all(
        "els => els.filter((el) => { const b = el.getBoundingClientRect();"
        " return el.checkVisibility({visibilityProperty: true})"
        " && b.width > 0 && b.height > 0; }).length"
    )
    assert still_closed == 0, f"{still_closed} visible sections did not open"


def test_results_page_has_no_moderate_or_worse_accessibility_violations(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    from axe_playwright_python.sync_playwright import Axe

    for r in all_runs:
        _open_results(page, ui, r)
        # #2313: every tab, each with all its sections open.
        for tab in _each_tab(page):
            _expand_every_section(page)
            violations = Axe().run(page).response["violations"]
            blocking = [v["id"] for v in violations if v.get("impact") in BLOCKING_IMPACTS]
            minor = [v["id"] for v in violations if v.get("impact") not in BLOCKING_IMPACTS]
            if minor:
                print(f"\naxe minor findings on the results page for {r.db} ({tab}): {minor}")
            # Name the elements, so a failure on a headless run's job says what to fix.
            where = {
                v["id"]: [n.get("target") for n in v.get("nodes", [])][:5]
                for v in violations
                if v.get("impact") in BLOCKING_IMPACTS
            }
            assert blocking == [], (r.job_id, r.db, tab, blocking, where)


def _axe_blocking(page: Page) -> tuple[list[str], dict[str, list[tuple[str, str]]], list[str]]:
    """Axe's blocking violation ids, the first targets of each, and the minor ids."""
    from axe_playwright_python.sync_playwright import Axe

    violations = Axe().run(page).response["violations"]
    blocking = [v["id"] for v in violations if v.get("impact") in BLOCKING_IMPACTS]
    minor = [v["id"] for v in violations if v.get("impact") not in BLOCKING_IMPACTS]
    where = {
        v["id"]: [
            (n.get("html", "")[:160], ((n.get("any") or [{}])[0]).get("message", ""))
            for n in v.get("nodes", [])
        ][:5]
        for v in violations
        if v.get("impact") in BLOCKING_IMPACTS
    }
    return blocking, where, minor


@pytest.mark.parametrize("width", [1440, 390])
def test_job_pages_have_no_accessibility_violations(
    page: Page, ui: str, all_runs: list[PipelineResult], width: int
) -> None:
    """#2312: the dashboard, the new-assessment form, the progress page and the per-query
    assignment review (with its first row expanded) have named navigation toggles, a
    labelled selection column, readable signal chips and no duplicate landmarks, at a
    desktop and a phone width. The deterministic jobs' triage signals name no engine, so
    the chips axe sees here are the neutral grey ones; the engine-coloured pairs are
    checked on the rendered chips in src/ui/src/components/__tests__/SignalChips.test.js."""
    page.set_viewport_size({"width": width, "height": 900})
    # An alert fades in; axe must not read its text mid-fade.
    page.emulate_media(reduced_motion="reduce")
    r = all_runs[0]
    page.goto(f"{ui}/dashboard")
    expect(page.get_by_text(r.job_id).or_(page.get_by_text(r.db)).first).to_be_visible(
        timeout=READY_MS
    )
    scans = {"dashboard": _axe_blocking(page)}

    page.goto(f"{ui}/analysis/create")
    expect(page.get_by_role("heading", name=re.compile("Create|New", re.I)).first).to_be_visible(
        timeout=READY_MS
    )
    scans["create"] = _axe_blocking(page)

    page.goto(f"{ui}/analysis/monitor/summary/{r.job_id}")
    page.wait_for_load_state("networkidle")
    scans["progress"] = _axe_blocking(page)

    page.goto(f"{ui}/analysis/assignments/{r.job_id}")
    expect(page.get_by_text(f"Your {r.db} workload").first).to_be_visible(timeout=READY_MS)
    _open_per_query_view(page)
    rows = page.locator(".query-list-row__summary")
    expect(rows.first).to_be_visible(timeout=READY_MS)
    # Every row on the page, so each kind of detail (patterns, signals, optimization
    # notes) is scanned, not only the first row's.
    for i in range(rows.count()):
        rows.nth(i).click()
    expect(page.locator(".query-list-row__detail").first).to_be_visible(timeout=READY_MS)
    # A row's panel fades in; axe reads a blended text colour mid-fade and reports a
    # contrast failure that is not there once it settles. Wait for every animation.
    page.evaluate("Promise.all(document.getAnimations().map((a) => a.finished))")
    scans["assignments"] = _axe_blocking(page)
    print(f"\naxe blocking at {width}px: " + str({k: v[0] for k, v in scans.items()}))
    failures = {k: (v[0], v[1]) for k, v in scans.items() if v[0]}
    assert failures == {}, (width, failures)
    if width == 390:
        # The list scrolls inside its own region; the page is never wider than the screen.
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")
        region = page.locator(".query-list-scroll")
        assert region.evaluate("el => el.scrollWidth > el.clientWidth")

    page.goto(f"{ui}/dashboard")
    expect(page.locator("table").first).to_be_visible(timeout=READY_MS)
    assert "empty-table-header" not in scans["dashboard"][2]
    # Each row's radio says what it selects, and sort labels use the column's header.
    radios = page.locator("table input[type=radio]")
    expect(radios.first).to_be_attached(timeout=READY_MS)
    for i in range(radios.count()):
        assert (radios.nth(i).get_attribute("aria-label") or "").startswith("Select ")
    sort_labels = page.locator("table th [aria-label$='sorted.']").evaluate_all(
        "els => els.map(e => e.getAttribute('aria-label'))"
    )
    # The phone list drops the Job ID column (#2345), so it names the database column.
    expected_label = "Job ID, not sorted." if width == 1440 else "Database, not sorted."
    assert expected_label in sort_labels, sort_labels
    raw_ids = (
        "jobId",
        "databaseName",
        "sourceEngine",
        "sourceLabel",
        "projectedCost",
        "targetEngines",
    )
    assert not [lbl for lbl in sort_labels if lbl.startswith(raw_ids)]
    if width == 390:
        # #2345: the phone list's cost column is sortable and named by its header.
        assert "Projected cost, not sorted." in sort_labels, sort_labels


def _page_fits_viewport(page: Page) -> bool:
    return bool(page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1"))


@pytest.mark.parametrize("width", [1440, 390])
def test_progress_page_of_a_finished_job_has_a_summary_and_a_readable_flow_chart(
    page: Page, ui: str, all_runs: list[PipelineResult], width: int
) -> None:
    """#2317: a finished job's progress page summarizes the database, source, target engines,
    monthly cost and overall risk from the report facts, with "View results" in the header and
    no phase tracker. The query flow fills its container (no fixed 900 px), is one named image
    that states the split, and "Show as a table" lists the same numbers. The page never scrolls
    sideways at a phone width."""
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    for r in all_runs:
        facts = json.loads((r.artifact_root / r.report["report"]).read_text())["facts"]
        ev = _watch(page)
        page.goto(f"{ui}/analysis/monitor/summary/{r.job_id}")
        expect(page.get_by_role("heading", level=2, name="Summary")).to_be_visible(timeout=READY_MS)
        cost = facts.get("projected_monthly_cost_usd")
        expected_cost = f"${cost:,.2f}/mo" if isinstance(cost, (int, float)) else "—"
        body = page.inner_text("body")
        where = (r.db, width)
        assert r.db in body, where
        assert expected_cost in body, (where, expected_cost)
        for engine in facts.get("target_engines") or []:
            assert ENGINE_LABELS.get(engine, engine) in body, (where, engine)
        assert "Overall risk" in body and "Assessment id" in body, where
        assert page.get_by_role("link", name="View results").count() == 1, where
        assert page.get_by_role("list", name="Assessment phases").count() == 0, where
        assert "Analyse" not in body and "patterns (" not in body, where

        chart = page.locator("svg[role=img]")
        expect(chart).to_have_count(1)
        label = chart.get_attribute("aria-label") or ""
        assert re.match(r"Query flow of [\d,]+ quer(y|ies), split as ", label), (where, label)
        placed = facts.get("in_scope_query_count")
        if isinstance(placed, int):
            assert label.startswith(f"Query flow of {placed:,} quer"), (where, placed, label)
        box = chart.bounding_box()
        assert box is not None and box["x"] >= 0 and box["x"] + box["width"] <= width + 1, (
            where,
            box,
        )
        if width == 1440:
            assert box["width"] > 950, (where, box)
        assert _page_fits_viewport(page), (where, "the page scrolls sideways")

        page.get_by_role("button", name="Show as a table").click()
        rows = page.locator("table tbody tr")
        expect(rows.first).to_be_visible()
        engines = [
            tuple(c.inner_text() for c in rows.nth(i).locator("td").all())
            for i in range(rows.count())
        ]
        owners = [(e, n) for e, n in engines if "(cache layer)" not in e]
        assert owners, (where, engines)
        # The table is the report's own final engine counts, the ones the results page shows.
        counts = {
            ENGINE_LABELS.get(e, e): n for e, n in (facts.get("engine_query_counts") or {}).items()
        }
        for engine_name, queries in owners:
            if engine_name in counts:
                assert queries == f"{counts[engine_name]:,}", (where, engine_name, queries, counts)
            assert f"{engine_name} {queries}" in label, (where, engine_name, queries, label)
        assert _page_fits_viewport(page), (where, "the page scrolls sideways with the table")
        assert ev == {"console": [], "pageerror": [], "failed": []}, (where, ev)


@pytest.mark.parametrize("width", [1440, 390])
def test_progress_page_of_a_running_job_lists_its_phases_without_overlap(
    page: Page, ui: str, all_runs: list[PipelineResult], width: int
) -> None:
    """#2317: a running job's phases are Cloudscape Steps, one per row, "Analyze" spelled that
    way, the current one marked in progress; no label runs into another at a phone width.
    The job's own detail response is cut to its first two stages for this check."""
    r = all_runs[0]

    def running(route: playwright.sync_api.Route) -> None:
        response = route.fetch()
        body = response.json()
        stages = body["progress"]["stages"][:2]
        stages[-1]["status"] = "in-progress"
        body["progress"]["stages"] = stages
        route.fulfill(response=response, json=body)

    page.route(re.compile(rf"/api/v1/assessments/{re.escape(r.job_id)}$"), running)
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    page.goto(f"{ui}/analysis/monitor/summary/{r.job_id}")
    steps = page.get_by_role("list", name="Assessment phases").get_by_role("listitem")
    expect(steps).to_have_count(5, timeout=READY_MS)
    names = [steps.nth(i).inner_text() for i in range(5)]
    assert [n.split("\n")[0] for n in names] == ["Collect", "Analyze", "Select", "Design", "Review"]
    assert steps.nth(1).get_by_role("img", name="In progress").count() == 1
    assert steps.nth(0).get_by_role("img", name="Done").count() == 1
    boxes = [steps.nth(i).bounding_box() for i in range(5)]
    for above, below in zip(boxes, boxes[1:], strict=False):
        assert above["y"] + above["height"] <= below["y"] + 1, (width, above, below)
    assert page.get_by_role("link", name="View results").count() == 0
    assert "Monthly cost" not in page.inner_text("body")
    assert _page_fits_viewport(page), (width, "the page scrolls sideways")
    if width == 390:
        for box in boxes:
            assert box["x"] + box["width"] <= width + 1, box


def test_results_page_has_no_accessibility_violations_with_an_earlier_path_picked(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """Review of #2249: with an earlier adoption path picked, the roadmap marks the waves
    and stages it leaves out without fading their text below WCAG AA contrast. Each job
    that offers paths is scanned with its first earlier summary path picked and every
    section open (the waves' stage lists and their units included)."""
    from axe_playwright_python.sync_playwright import Axe

    scanned = 0
    for r in all_runs:
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        adoption = (report.get("facts") or {}).get("adoption") or {}
        earlier = [p for p in adoption.get("summary_path_ids") or [] if p != "full_target"]
        if not earlier:
            continue
        _open_results(page, ui, r)
        _show_tab(page, "plan")
        panel = page.locator("#migration-roadmap-section [data-figure=adoption-paths]")
        _choose_path(panel, earlier[0], adoption)
        expect(panel.locator("[data-figure=selected-path]")).to_have_attribute(
            "data-path", earlier[0]
        )
        # #2313: every tab, the Plan tab (the roadmap) first; the path stays picked.
        for tab in ["plan", *(t for t in RESULTS_TABS if t != "plan")]:
            _show_tab(page, tab)
            _expand_every_section(page)
            if tab == "plan":
                section = page.locator("#migration-roadmap-section")
                # The stage lists are open, and the earlier path leaves something out.
                assert section.locator("[data-stage]").count() > 0, r.db
                assert section.locator("[data-left-out]").count() > 0, r.db
            violations = Axe().run(page).response["violations"]
            blocking = [v["id"] for v in violations if v.get("impact") in BLOCKING_IMPACTS]
            where = {
                v["id"]: [n.get("target") for n in v.get("nodes", [])][:5]
                for v in violations
                if v.get("impact") in BLOCKING_IMPACTS
            }
            assert blocking == [], (r.job_id, r.db, earlier[0], tab, blocking, where)
        scanned += 1
    assert scanned or any(r.external for r in all_runs), "no job offers an earlier path"


def _designed_runs(all_runs: list[PipelineResult]) -> list[PipelineResult]:
    """The jobs with at least one schema design: ``ui-desi`` in the deterministic
    suite (``tests/e2e/designed_run.py``), the headless run's own job on e2e-llm."""
    return [r for r in all_runs if real_designs(r.job_dir())]


def test_the_designed_job_renders_the_schema_design_panels(
    loaded_results: OpenResults, all_runs: list[PipelineResult]
) -> None:
    """#987: the suite always has a job with schema designs, so the checks above
    also cover the panels only a schema-designed run shows: the source tables
    (#2446, the explorer's by-source-table view), the design trade-offs and the
    Table design when an Aurora engine was designed. Fails when no such job
    renders them."""
    designed = _designed_runs(all_runs)
    assert designed, "no job with a schema design in this session"
    if not any(r.external for r in all_runs):
        assert [r.job_id for r in designed] == ["ui-desi"]
    for r in designed:
        page, ev = loaded_results(r)
        # #2313: the panels are on the Queries, Data model and Risks and decisions tabs.
        text = "\n".join(page.inner_text("body") for _ in _each_tab(page))
        headings = ["Source tables", "Trade-offs and design decisions"]
        aurora = any(
            p.parent.parent.name.startswith("schema-aurora_") for p in real_designs(r.job_dir())
        )
        if aurora:
            headings.append("Table design")
        for heading in headings:
            assert heading in text, (r.job_id, heading)
        assert "Aurora design" not in text, r.job_id
        if aurora:
            # #1232: every section of the Table design opens collapsed.
            _show_tab(page, "data-model")
            design = page.locator("#table-design-section")
            assert design.locator('[aria-expanded="false"]').count() >= 1, r.job_id
            assert design.locator('[aria-expanded="true"]').count() == 0, r.job_id
        _show_tab(page, "queries")
        assert page.get_by_role("radio").count() > 0, r.job_id
        # #2446: the explorer folded into the query table: one list on the Queries tab.
        assert "Access pattern explorer" not in text, r.job_id
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.job_id, ev)


def _a_designed_query(r: PipelineResult) -> tuple[str, str] | None:
    """A query a NoSQL design's access pattern serves, and that design's engine, from the
    job's schema_output.json files (#2446)."""
    for path in real_designs(r.job_dir()):
        engine = path.parent.parent.name.removeprefix("schema-")
        if engine.startswith("aurora_"):
            continue
        design = json.loads(path.read_text())
        for ap in design.get("access_patterns") or []:
            ids = ap.get("source_query_ids") or ap.get("query_ids") or []
            if ids:
                return ids[0], engine
    return None


@pytest.mark.parametrize("width", [1440, 390])
def test_a_querys_access_patterns_open_in_its_panel_and_source_tables_on_data_model(
    page: Page, ui: str, all_runs: list[PipelineResult], width: int
) -> None:
    """#2446: the Queries tab lists queries once: the explorer's pattern and cache tables
    are gone, and a query's access patterns open in its detail panel's Target design tab.
    The explorer's by-source-table view is the Data model tab's Source tables section,
    whose rows open their destination designs in a dialog. Axe finds nothing blocking
    with the panel or the dialog open, and the page does not scroll sideways. #2480
    review: the Target design tab and the dialog work from the keyboard, and a query
    opened from the dialog moves focus into its panel."""
    page.set_viewport_size({"width": width, "height": 900})
    page.emulate_media(reduced_motion="reduce")
    checked = 0
    for r in _designed_runs(all_runs):
        picked = _a_designed_query(r)
        if not picked:
            continue
        qid, engine = picked
        ev = _watch(page)
        _goto_results(page, ui, r, "queries")
        section = page.locator("#query-routing-section")
        _wait_for_query_routing_rows(section, r, ev)
        assert page.locator("#access-patterns-section").count() == 0
        assert page.get_by_role("heading", name="Access pattern explorer").count() == 0
        _filter_queries(section, qid)
        opener = section.get_by_role("button", name=f"{OPEN_TRACE}{qid}")
        expect(opener).to_be_visible(timeout=READY_MS)
        opener.click()
        panel = _query_panel(page, qid)
        panel.get_by_role("tab", name="General").focus()
        page.keyboard.press("ArrowRight")
        page.keyboard.press("Enter")
        expect(panel.get_by_role("tab", name="Target design")).to_have_attribute(
            "aria-selected", "true", timeout=READY_MS
        )
        heading = re.compile(
            rf"^{re.escape(ENGINE_LABELS[engine])}( \(cache layer\))? access pattern "
        )
        expect(panel.get_by_role("heading", name=heading).first).to_be_visible(timeout=READY_MS)
        expect(panel).to_contain_text("Destination Table")
        blocking, where, _ = _axe_blocking(page)
        assert blocking == [], (r.db, width, "query panel", blocking, where)
        page.get_by_role("button", name="Close the query details").click()
        expect(panel).to_be_hidden(timeout=READY_MS)

        _show_tab(page, "data-model")
        tables = page.locator("#source-tables-section")
        expect(tables).to_be_visible(timeout=READY_MS)
        expect(tables.get_by_role("heading", name=re.compile(r"^Source tables"))).to_be_visible()
        first = tables.get_by_role("button", name=re.compile(r"^Open the target design of ")).first
        first.focus()
        page.keyboard.press("Enter")
        dialog = page.get_by_role("dialog")
        expect(dialog).to_be_visible(timeout=READY_MS)
        expect(dialog).to_contain_text("Source table ")
        blocking, where, _ = _axe_blocking(page)
        assert blocking == [], (r.db, width, "source table dialog", blocking, where)
        page.keyboard.press("Escape")
        expect(dialog).to_be_hidden(timeout=READY_MS)
        expect(first).to_be_focused(timeout=READY_MS)
        page.keyboard.press("Enter")
        expect(dialog).to_be_visible(timeout=READY_MS)
        query_link = dialog.get_by_role("button", name=re.compile(f"^{re.escape(OPEN_TRACE)}"))
        expect(query_link.first).to_be_visible(timeout=READY_MS)
        linked = (query_link.first.get_attribute("aria-label") or "").removeprefix(OPEN_TRACE)
        query_link.first.focus()
        page.keyboard.press("Enter")
        expect(dialog).to_be_hidden(timeout=READY_MS)
        panel = _query_panel(page, linked)
        expect(panel.locator("[data-query-detail-focus]")).to_be_focused(timeout=READY_MS)
        page.get_by_role("button", name="Close the query details").click()
        expect(panel).to_be_hidden(timeout=READY_MS)
        assert _page_fits_viewport(page), (width, "the page scrolls sideways")
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.job_id, ev)
        checked += 1
    assert checked or any(r.external for r in all_runs), "no job with a NoSQL design"


def _design_filter_cases(r: PipelineResult) -> list[tuple[str, str, str]]:
    """Property tokens the query table's target design properties take (#2483), each
    with a query an access pattern of the job's NoSQL designs serves for it: its target
    operation, its pattern group, and Uses GSI = Yes when a pattern reads a GSI."""
    cases: dict[str, tuple[str, str, str]] = {}
    for path in real_designs(r.job_dir()):
        if path.parent.parent.name.startswith("schema-aurora_"):
            continue
        for ap in json.loads(path.read_text()).get("access_patterns") or []:
            ids = ap.get("source_query_ids") or ap.get("query_ids") or []
            if not ids:
                continue
            if ap.get("operation"):
                cases.setdefault("op", ("Target operation", ap["operation"], ids[0]))
            if ap.get("pattern_group"):
                cases.setdefault("group", ("Pattern group", ap["pattern_group"], ids[0]))
            if ap.get("gsi_name"):
                cases.setdefault("gsi", ("Uses GSI", "Yes", ids[0]))
    return list(cases.values())


def test_the_query_table_filters_by_the_target_design(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#2483: the explorer's target operation, pattern group and Uses GSI filters are
    properties of the query table, grouped under Target design. Each token keeps a query
    the design's own pattern serves, reads as the value picked, and axe finds nothing
    blocking with a token applied."""
    page.set_viewport_size({"width": 1440, "height": 900})
    checked = 0
    for r in _designed_runs(all_runs):
        cases = _design_filter_cases(r)
        if not cases:
            continue
        ev = _watch(page)
        _goto_results(page, ui, r, "queries")
        section = page.locator("#query-routing-section")
        _wait_for_query_routing_rows(section, r, ev)
        box = section.get_by_label("Filter queries")
        box.focus()
        expect(
            page.get_by_text(re.compile(r"^Target design \(a query matches when any"))
        ).to_be_visible(timeout=READY_MS)
        for prop, value, qid in cases:
            _filter_queries(section, f"{prop} = {value}")
            expect(section).to_contain_text(
                re.compile(rf"{re.escape(prop)}\s*=\s*{re.escape(value)}"), timeout=READY_MS
            )
            _filter_queries(section, qid)
            opener = section.get_by_role("button", name=f"{OPEN_TRACE}{qid}")
            expect(opener).to_be_visible(timeout=READY_MS)
            page.keyboard.press("Escape")
            blocking, where, _ = _axe_blocking(page)
            assert blocking == [], (r.db, prop, blocking, where)
            section.get_by_role("button", name="Clear filters").click()
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.job_id, ev)
        checked += 1
    assert checked or any(r.external for r in all_runs), "no job with a NoSQL design"


def _aurora_design_tables(r: PipelineResult, engine: str | None = None) -> list[str]:
    """The table names of the job's Aurora design (of one engine when given), as its
    schema_output.json lists them."""
    names: list[str] = []
    for path in real_designs(r.job_dir()):
        folder = path.parent.parent.name
        if folder.startswith("schema-aurora_") and engine in (None, folder.removeprefix("schema-")):
            design = json.loads(path.read_text())
            names += [t["table_name"] for t in design.get("table_definitions") or []]
    return names


def _another_aurora_engine(r: PipelineResult) -> str:
    """An Aurora engine the job has no design for (the other relational engine)."""
    designed = _designed_aurora_engines(r)
    return "aurora_mysql" if "aurora_mysql" not in designed else "aurora_postgresql"


def _serve_extra_aurora_design(page: Page, engine: str, drop_patterns: bool = False) -> None:
    """Add one more Aurora design, for ``engine``, to the schema designs the page loads.
    ``drop_patterns`` also removes the access patterns from the Aurora designs served
    already: a link to an engine whose design lists them lands on its queries (a model run
    designs them), and a link to one that lists none lands on its Table design tab."""

    def add_design(route: playwright.sync_api.Route) -> None:
        response = route.fetch()
        body = response.json()
        if drop_patterns:
            for design in body["schema_designs"]:
                if str(design.get("target_type", "")).startswith("aurora_"):
                    design["content"].pop("access_patterns", None)
        body["schema_designs"].append(
            {
                "target_type": engine,
                "content": {"table_definitions": [{"table_name": "second_design", "columns": []}]},
            }
        )
        route.fulfill(response=response, json=body)

    page.route("**/schema-designs", add_design)


def test_the_aurora_design_is_a_table_with_a_filter_and_a_detail_panel(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#2325: the Table design lists the design's tables in one table with a text filter,
    fixed column widths and no nested toggle per table. Picking a row opens its columns,
    keys, queries and DDL in the split panel beside the list, as text; axe finds nothing
    blocking with the panel open, and closing it leaves the list as it was."""
    checked = 0
    for r in _designed_runs(all_runs):
        names = _aurora_design_tables(r)
        if not names:
            continue
        ev = _watch(page)
        _goto_results(page, ui, r, "data-model")
        design = page.locator("#table-design-section")
        expect(design).to_be_visible(timeout=READY_MS)
        tables = design.locator("#aurora-design-tables")
        expect(tables).to_be_visible(timeout=READY_MS)
        shown = tables.locator("tbody tr")
        expect(shown).to_have_count(min(len(names), 25), timeout=READY_MS)
        headers = [h.strip() for h in tables.locator("thead th").all_inner_texts()]
        assert headers[-6:] == [
            "Table",
            "Columns",
            "Primary key",
            "Indexes",
            "Foreign keys",
            "Queries",
        ], headers
        # One list, not a toggle per table: no nested section carries a table's name.
        assert design.get_by_role("button", name=names[0], exact=True).count() == 0

        design.get_by_role("searchbox", name="Find a table by name").fill(names[0])
        expect(shown).to_have_count(1, timeout=READY_MS)
        design.get_by_role("button", name=f"Open {names[0]} in the detail panel").click()
        panel = page.get_by_role("region", name=f"Table {names[0]}").first
        expect(panel).to_be_visible(timeout=READY_MS)
        assert page.get_by_role("dialog").count() == 0
        expect(panel).to_contain_text("CREATE TABLE", timeout=READY_MS)
        expect(panel).to_contain_text("Primary key")
        blocking, where, _ = _axe_blocking(page)
        assert blocking == [], (r.db, blocking, where)

        page.get_by_role("button", name="Close the table details").click()
        expect(panel).to_be_hidden(timeout=READY_MS)
        expect(tables).to_be_visible()

        # The keyboard opens it too: focus the name button, press Enter, then Tab reaches
        # the panel's close button and Enter closes it.
        opener = design.get_by_role("button", name=f"Open {names[0]} in the detail panel")
        opener.focus()
        page.keyboard.press("Enter")
        panel = page.get_by_role("region", name=f"Table {names[0]}").first
        expect(panel).to_be_visible(timeout=READY_MS)
        label = "Close the table details"
        for _ in range(120):
            if page.evaluate("document.activeElement?.getAttribute('aria-label')") == label:
                break
            page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement?.getAttribute('aria-label')") == label
        page.keyboard.press("Enter")
        expect(panel).to_be_hidden(timeout=READY_MS)
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.job_id, ev)
        checked += 1
    assert checked or any(r.external for r in all_runs), "no job with an Aurora design"


def test_the_aurora_table_panel_closes_when_the_design_tab_changes(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#2325: the panel shows one engine's table, so switching to another engine's design
    tab closes it. When the job designed one Aurora engine, the other engine's design is
    added to the served schema designs; the tabs are the job's own (#2488), so the test
    holds on a MySQL source (wordpress) and a PostgreSQL one (discourse, trailgear)."""
    checked = 0
    for r in _designed_runs(all_runs):
        engines = _designed_aurora_engines(r)
        if not engines:
            continue
        first = engines[0]
        names = _aurora_design_tables(r, first)
        if not names:
            continue
        second = engines[1] if len(engines) > 1 else _another_aurora_engine(r)
        if len(engines) == 1:
            _serve_extra_aurora_design(page, second)
        try:
            _goto_results(page, ui, r, "data-model")
            design = page.locator("#table-design-section")
            design.get_by_role("tab", name=re.compile(re.escape(ENGINE_LABELS[first]))).click()
            design.get_by_role("button", name=f"Open {names[0]} in the detail panel").click()
            panel = page.get_by_role("region", name=f"Table {names[0]}").first
            expect(panel).to_be_visible(timeout=READY_MS)
            design.get_by_role("tab", name=re.compile(re.escape(ENGINE_LABELS[second]))).click()
            expect(panel).to_be_hidden(timeout=READY_MS)
        finally:
            page.unroute("**/schema-designs")
        checked += 1
    assert checked or any(r.external for r in all_runs), "no job with an Aurora design"


def test_the_trade_offs_tabs_list_each_topic_with_the_facts_label_and_count(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#1221: each engine tab of "Trade-offs and design decisions" lists its design
    trade-offs under the topics report.json's facts.trade_off_topics groups them in,
    with the label and count the engineering report prints; an engine whose
    trade-offs are all "other" shows no topic heading."""
    from src.shared.engine_names import display_engine

    checked = 0
    for r in _designed_runs(all_runs):
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        groups = (report.get("facts") or {}).get("trade_off_topics") or []
        by_engine: dict[str, list[dict]] = {}
        for g in groups:
            if g["topic"] != "review_note":
                by_engine.setdefault(g["engine"], []).append(g)
        ev = _open_results(page, ui, r)
        _show_tab(page, "risks")
        for engine, topics in by_engine.items():
            tab = page.get_by_role(
                "tab", name=re.compile(rf"^{re.escape(display_engine(engine))} \(")
            )
            if tab.count() == 0:
                continue
            tab.first.click()
            for g in topics:
                section = page.locator(f'[data-trade-off-topic="{g["topic"]}"]')
                expect(section.first).to_be_visible(timeout=READY_MS)
                heading = f"{g['label']} ({g['trade_off_count']})"
                if [t["topic"] for t in topics] == ["other"]:
                    assert heading not in section.first.inner_text(), (r.job_id, engine)
                else:
                    assert heading in section.first.inner_text(), (r.job_id, engine, heading)
                    checked += 1
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.job_id, ev)
    if not any(r.external for r in all_runs):
        assert checked > 0, "no titled trade-off topic group in the designed job"


def test_results_page_states_the_pending_engine_choices(
    loaded_results: OpenResults, all_runs: list[PipelineResult]
) -> None:
    """#1003: the results page states the pending engine choices with the counts
    report.json's facts hold (the ones the engineering report and the PDF state),
    the choices --auto decided the same way (#1973), and nothing when there are
    neither."""
    for r in all_runs:
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        facts = report.get("facts") or {}
        n, q = facts.get("pending_choice_count") or 0, facts.get("pending_choice_query_count") or 0
        d = facts.get("auto_decided_choice_count") or 0
        dq = facts.get("auto_decided_query_count") or 0
        page, ev = loaded_results(r)
        _show_tab(page, "plan")
        note = page.locator("#engine-choices-note")
        # #2318: in wave 1 of the roadmap, not a section of its own.
        if note.count():
            assert (
                page.locator("#migration-roadmap-section [data-wave]")
                .first.locator("#engine-choices-note")
                .count()
                == 1
            ), r.db
        if n:
            tables = "table still ties" if n == 1 else "tables still tie"
            queries = "query" if q == 1 else "queries"
            assert f"{n} {tables} between engines ({q} {queries})" in note.inner_text(), r.db
        if d:
            choices = "engine choice" if d == 1 else "engine choices"
            queries = "query" if dq == 1 else "queries"
            decided = "was decided" if d == 1 else "were decided"
            assert f"{d} {choices} ({dq} {queries}) {decided} automatically" in (
                note.inner_text()
            ), r.db
        else:
            assert "decided automatically" not in (note.inner_text() if note.count() else ""), r.db
        if not n and not d:
            assert note.count() == 0, r.db
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.db, ev)


def test_query_routing_table_fits_its_card_at_1440(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    """#1244: at a 1440 px viewport the Query routing table was about 2083 px wide in a
    card about 1238 px wide, with Engine and Routing off-screen until the reader scrolled
    right. The table now fits its card, and Engine and Routing are on the first screen."""
    page.set_viewport_size({"width": 1440, "height": 900})
    for r in all_runs:
        ev = _watch(page)
        _goto_results(page, ui, r, "queries")
        section = page.locator("#query-routing-section")
        expect(section).to_be_visible(timeout=READY_MS)
        _wait_for_query_routing_rows(section, r, ev)
        m = page.evaluate("""() => {
              const table = document.querySelector('#query-routing-section table');
              const scroller = table.parentElement;
              const box = scroller.getBoundingClientRect();
              const head = (name) => [...table.querySelectorAll('thead th')]
                .find((th) => th.innerText.trim() === name).getBoundingClientRect();
              return {tableWidth: table.scrollWidth, visibleWidth: scroller.clientWidth,
                      visibleRight: box.right, engineRight: head('Engine').right,
                      routingRight: head('Routing').right};
            }""")
        assert m["tableWidth"] <= m["visibleWidth"] + 1, (r.db, m)
        assert m["engineRight"] <= m["visibleRight"] + 1, (r.db, m)
        assert m["routingRight"] <= m["visibleRight"] + 1, (r.db, m)


# #1246: one colour per engine on every view. Measured in the browser on the results
# page: every engine badge (cost cards, roadmap, explorer,
# risks), every Sankey node and every trade-off Mapping target chip is drawn in the
# engine's catalog colour (colors.ui), and source-table chips in a neutral grey.
_ENGINE_COLOURS_JS = """() => {
  const rgb = (el, prop) => getComputedStyle(el)[prop];
  const badges = [...document.querySelectorAll('[data-engine-badge]')].map((el) => ({
    engine: el.dataset.engineBadge, colour: rgb(el.firstElementChild, 'backgroundColor')}));
  const nodes = [...document.querySelectorAll('svg g[data-sankey-node]')].map((g) => ({
    engine: g.dataset.sankeyNode, colour: rgb(g.querySelector('rect'), 'fill')}));
  const chips = [...document.querySelectorAll('[data-mapping]')].map((el) => ({
    kind: el.dataset.mapping, engine: el.dataset.engine || null,
    colour: rgb(el.querySelector('[class*="awsui_badge_"]'), 'backgroundColor')}));
  return {badges, nodes, chips};
}"""


def _rgb(hex_colour: str) -> str:
    h = hex_colour.lstrip("#")
    return f"rgb({int(h[0:2], 16)}, {int(h[2:4], 16)}, {int(h[4:6], 16)})"


def test_each_engine_has_one_colour_across_the_ui(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    from src.shared import catalog

    colour = {e.id: _rgb(e.colors.ui) for e in catalog.engines()}
    neutral = _rgb("#5f6b7a")
    page.set_viewport_size({"width": 1440, "height": 900})
    seen: dict[str, int] = {"badges": 0, "nodes": 0, "chips": 0}
    for r in all_runs:
        _open_results(page, ui, r)
        # #2313: every tab's badges, nodes and chips; a tab opened before stays in the
        # DOM, so each is measured on the tab that shows it.
        for tab in _each_tab(page):
            _expand_every_section(page)
            found = page.evaluate(_ENGINE_COLOURS_JS)
            for b in found["badges"]:
                assert b["colour"] == colour.get(b["engine"], neutral), (r.db, tab, b)
            for n in found["nodes"]:
                if n["engine"] in colour:
                    assert n["colour"] == colour[n["engine"]], (r.db, tab, n)
                    seen["nodes"] += 1
            for c in found["chips"]:
                expected = neutral if c["kind"] == "source" else colour.get(c["engine"], neutral)
                assert c["colour"] == expected, (r.db, tab, c)
            seen["badges"] += len(found["badges"])
            seen["chips"] += len(found["chips"])
            if tab == "plan":
                # The roadmap's wave badges are engine badges too: every wave engine is drawn.
                waves = page.locator("[data-wave] [data-engine] [data-engine-badge]")
                assert waves.count() > 0, r.db
    assert seen["badges"] > 0 and seen["nodes"] > 0, seen


# #1245: the cache layer's label ("ElastiCache (N)") and its "cache layer" sub-label
# used to be drawn over the small dashed cache node and over each other. Measured in
# the browser: no label box crosses its node, the two labels do not overlap, and both
# fit inside the diagram.
_SANKEY_CACHE_LABEL_BOXES = """() => {
  const box = (el) => { const r = el.getBoundingClientRect();
    return {left: r.left, right: r.right, top: r.top, bottom: r.bottom}; };
  return [...document.querySelectorAll('svg')].flatMap((svg) =>
    [...svg.querySelectorAll('g')].filter((g) =>
      [...g.querySelectorAll('text')].some((t) => t.textContent === 'cache layer'))
    .map((g) => {
      const [name, sub] = g.querySelectorAll('text');
      return {label: name.textContent, svg: box(svg), node: box(g.querySelector('rect')),
              name: box(name), sub: box(sub)};
    }));
}"""


def _overlap(a: dict[str, float], b: dict[str, float]) -> bool:
    return (
        a["left"] < b["right"]
        and b["left"] < a["right"]
        and a["top"] < b["bottom"]
        and b["top"] < a["bottom"]
    )


def test_sankey_cache_label_clears_its_node_and_sub_label(
    page: Page, ui: str, all_runs: list[PipelineResult]
) -> None:
    page.set_viewport_size({"width": 1440, "height": 900})
    checked = []
    expected = 0
    for r in all_runs:
        report = json.loads((r.artifact_root / r.report["report"]).read_text())
        if report.get("cache_overlay"):
            expected += 2  # the Job summary and the results page both draw it
        for url in (
            f"{ui}/analysis/monitor/summary/{r.job_id}",
            f"{ui}/analysis/results/{r.job_id}",
        ):
            page.goto(url)
            # The Sankey itself must be drawn before its labels are measured, whether or
            # not this run has a cache layer: an empty scan of a page that has not
            # rendered would pass vacuously.
            page.locator("[data-sankey-node]").first.wait_for(timeout=READY_MS)
            labels = page.evaluate(_SANKEY_CACHE_LABEL_BOXES)
            if report.get("cache_overlay"):
                assert labels, (
                    r.db,
                    url,
                    "the report keeps a cache layer but the Sankey draws none",
                )
            else:
                assert not labels, (r.db, url, "the Sankey draws a cache layer the report lacks")
            for g in labels:
                where = (r.db, url, g["label"])
                assert not _overlap(g["name"], g["node"]), where
                assert not _overlap(g["sub"], g["node"]), where
                assert not _overlap(g["name"], g["sub"]), where
                for label in (g["name"], g["sub"]):
                    assert g["svg"]["left"] <= label["left"], where
                    assert label["right"] <= g["svg"]["right"], where
                checked.append(where)
    # Every run whose report keeps a cache layer was measured on both pages (the
    # wordpress sample does, so the deterministic job is never vacuous).
    assert expected > 0 or all(not r.db.startswith("wordpress") for r in all_runs)
    assert len(checked) == expected, checked


def test_results_page_lists_the_dynamodb_merge_review_decisions(
    loaded_results: OpenResults, all_runs: list[PipelineResult]
) -> None:
    """#1780: the results page shows ``facts.dynamodb_merge_reviews`` as both reports
    print it: the title, the two sentences and each decision's line, and nothing when the
    fact is absent. The deterministic suite builds a job whose DynamoDB design comes from
    the group merge with an overlap left for review
    (``designed_run.run_merge_review_wordpress``) into the UI's artifact root; an external
    job is checked as it is, whichever case it is (e2e-llm runs this file without
    deselecting deterministic tests, so it never skips, #1794)."""
    if any(r.external for r in all_runs):
        jobs = list(all_runs)
    else:
        from tests.e2e.designed_run import MERGE_REVIEW_COPY, run_merge_review_wordpress

        jobs = [run_merge_review_wordpress(all_runs[0].artifact_root, "ui-merg")]
        first = json.loads(jobs[0].report_json().read_text())["facts"]["dynamodb_merge_reviews"]
        assert first["decisions"][0]["authoritative_home"] == "Posts", first
        assert f"{MERGE_REVIEW_COPY} is a derived copy" in first["decisions"][0]["line"]
    for r in jobs:
        fact = json.loads(r.report_json().read_text())["facts"].get("dynamodb_merge_reviews")
        page, ev = loaded_results(r)
        _show_tab(page, "risks")
        callout = page.locator("#dynamodb-merge-reviews")
        if not fact:
            assert callout.count() == 0, r.job_id
            continue
        expect(callout).to_be_visible(timeout=READY_MS)

        def figures(name: str, callout: Locator = callout) -> list[str]:
            return [t.strip() for t in callout.locator(f'[data-figure="{name}"]').all_inner_texts()]

        assert "DynamoDB merge-review decisions" in callout.inner_text(), r.job_id
        assert figures("merge-review-summary") == [fact["summary_line"]], r.job_id
        assert figures("merge-review-review") == [fact["review_line"]], r.job_id
        assert figures("merge-review-decision") == [d["line"] for d in fact["decisions"]]
        assert ev == {"console": [], "pageerror": [], "failed": []}, (r.job_id, ev)
