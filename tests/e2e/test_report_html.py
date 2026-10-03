"""The HTML deliverables render cleanly, offline, in Chromium and WebKit."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page

from tests.e2e.pipeline import PipelineResult

# "undefined"/"NaN"/"null" are checked as whole WORDS (\b-bounded): matched as
# plain substrings they false-positive on prose like "the null hypothesis" or
# identifiers containing them. "[object Object]" and "{{" can't appear as
# legitimate words in rendered report text at all, so they stay plain
# substring checks.
FORBIDDEN_WORDS = ("undefined", "NaN", "null")
FORBIDDEN_SUBSTRINGS = ("[object Object]", "{{")


def _leaked_forbidden_text(text: str) -> list[str]:
    leaked = [w for w in FORBIDDEN_WORDS if re.search(rf"\b{re.escape(w)}\b", text)]
    leaked += [s for s in FORBIDDEN_SUBSTRINGS if s in text]
    return leaked


REPORTS = {
    "analysis": (
        "analysis-report",
        [
            "Database Modernization Analysis Report",
            "Executive Summary",
            "Cost Breakdown",
            "Query Flow",
            "Access Pattern Explorer",
            "Trade-offs and Design Decisions",
            "Principal Engineer Notes",
        ],
    ),
    "decision": (
        "decision-report",
        [
            "Database Modernization — Decision Report",
            "Executive summary",
            "Recommended architecture",
        ],
    ),
}


def _open_offline(page: Page, path) -> dict[str, list[str]]:
    """Load ``path`` with every non-file request blocked; collect console/page errors."""
    events: dict[str, list[str]] = {"console": [], "pageerror": [], "blocked": []}

    def on_console(message) -> None:
        if message.type == "error":
            events["console"].append(message.text)

    page.on("console", on_console)
    page.on("pageerror", lambda e: events["pageerror"].append(str(e)))

    def guard(route):
        if route.request.url.startswith("file://"):
            route.continue_()
        else:
            events["blocked"].append(route.request.url)
            route.abort()

    page.route("**/*", guard)
    page.goto(path.as_uri())
    page.wait_for_load_state("networkidle")
    return events


@pytest.mark.parametrize("kind", list(REPORTS))
def test_report_renders_cleanly_offline(page: Page, run: PipelineResult, kind: str) -> None:
    marker, headings = REPORTS[kind]
    events = _open_offline(page, run.html(marker))

    assert events == {"console": [], "pageerror": [], "blocked": []}
    text = page.inner_text("body")
    for h in headings:
        assert h in text, f"missing {h!r} in {kind} report"
    leaked = _leaked_forbidden_text(text)
    assert leaked == [], f"{kind} report shows {leaked}"


@pytest.mark.parametrize("width", [1280, 1920])
@pytest.mark.parametrize("kind", list(REPORTS))
def test_no_horizontal_overflow(page: Page, run: PipelineResult, kind: str, width: int) -> None:
    marker, _ = REPORTS[kind]
    page.set_viewport_size({"width": width, "height": 900})
    _open_offline(page, run.html(marker))
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


def test_analysis_report_embeds_every_journey_and_draws_charts(
    page: Page, run: PipelineResult
) -> None:
    _open_offline(page, run.html("analysis-report"))
    assert page.evaluate("DATA.queryJourneys.total") == run.report["journeys"]
    for canvas in ("#engineChart", "#operationChart"):
        box = page.locator(canvas).bounding_box()
        assert box and box["width"] > 0 and box["height"] > 0, canvas


@pytest.mark.parametrize("kind", list(REPORTS))
def test_no_serious_accessibility_violations(page: Page, run: PipelineResult, kind: str) -> None:
    from axe_playwright_python.sync_playwright import Axe

    marker, _ = REPORTS[kind]
    _open_offline(page, run.html(marker))
    violations = Axe().run(page).response["violations"]
    serious = [v["id"] for v in violations if v.get("impact") in ("serious", "critical")]
    non_serious = [v["id"] for v in violations if v.get("impact") not in ("serious", "critical")]
    if non_serious:
        print(f"\naxe non-serious findings for {kind} report: {non_serious}")
    assert serious == [], serious


def _export_data_with_patterns() -> dict:
    """Minimal export data with schema designs.

    The deterministic pipeline run produces no schema designs (that phase needs an
    LLM), so its explorer table is empty and has no rows to click.
    """
    qid = "q-0001"
    patterns = [
        {
            "pattern_id": f"DDB-AP-{i}",
            "operation": "Query" if i % 2 else "PutItem",
            "source_tables": [f"wp.wp_{t}"],
            "table_name": f"{t}_table",
            "description": f"Access pattern {i}",
            "query_ids": [qid],
        }
        for i, t in enumerate(["posts", "posts", "users"], start=1)
    ]
    return {
        "jobId": "e2e-modal-check",
        "exportDate": "2026-10-03T12:00:00+00:00",
        "results": {
            "synthesis": {
                "database_name": "wordpress",
                "summary": "Summary.",
                "reality_check": {"after_distribution": {"dynamodb": 100.0}},
                "tco_analysis": {
                    "cost_breakdown": [{"database": "dynamodb", "monthly_cost_usd": 1.0}]
                },
            }
        },
        "schemaDesigns": [
            {
                "target_type": "dynamodb",
                "content": {
                    "access_patterns": patterns,
                    "trade_offs": [{"description": "Trade-off", "query_ids": [qid]}],
                },
            }
        ],
        "queryJourneys": {"total": 1, "items": [{"query_id": qid, "source": {}, "assignment": {}}]},
    }


def test_analysis_report_detail_modals_open_from_every_view(page: Page, tmp_path) -> None:
    """Row clicks open their modal in every explorer view without a script error (#243)."""
    from src.report.analysis_report import render_analysis_report_html

    path = tmp_path / "analysis-report.html"
    path.write_text(render_analysis_report_html(_export_data_with_patterns()), encoding="utf-8")
    events = _open_offline(page, path)

    page.locator(".toggle-btn", has_text="By access pattern").click()
    page.locator("#access-patterns-container tbody tr").first.click()
    assert page.locator("#pattern-modal").is_visible()
    page.locator("#pattern-modal .modal-close").click()

    page.locator(".toggle-btn", has_text="By source table").click()
    rows = page.locator("#access-patterns-container tbody tr")
    assert rows.count() == 2  # wp_posts, wp_users
    for i in range(rows.count()):
        rows.nth(i).click()
        assert page.locator("#source-table-modal").is_visible()
        assert page.locator("#source-table-modal-body .tab-content").count() > 0
        page.locator("#source-table-modal .modal-close").click()

    page.locator("#tradeoffs-container .link").first.click()
    assert page.locator("#query-journey-modal").is_visible()

    assert events == {"console": [], "pageerror": [], "blocked": []}
