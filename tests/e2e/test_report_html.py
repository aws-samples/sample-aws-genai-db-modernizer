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
