"""The built help site (#325): home page, search, the sample report, and nav links.

Run via `ci/e2e.sh`, which builds the site first (`mkdocs build --strict`, then
`scripts/build_docs_sample.py`) -- this file only serves and checks the result,
the same split `test_ui.py` uses for the local UI (built by `ci/lib.sh`'s
`build_ui`, served and checked here).

Chromium only (`only_browser`, like `test_ui.py`): the site itself is checked
for broken links by `mkdocs build --strict`, which already runs cross-platform
in CI; this suite is about the rendered page, not browser-engine differences in
HTML/CSS, so one browser is enough.
"""

from __future__ import annotations

import http.server
import re
import socket
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Page

from tests.placeholders import placeholder_leaks

REPO = Path(__file__).resolve().parents[2]
SITE_DIR = REPO / "site"

pytestmark = pytest.mark.only_browser("chromium")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port: int = s.getsockname()[1]
        return port


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
        pass


@pytest.fixture(scope="session")
def docs_site() -> Iterator[str]:
    if not (SITE_DIR / "index.html").exists():
        pytest.fail(
            f"{SITE_DIR} not built. Run: uv run mkdocs build --strict && "
            "uv run python scripts/build_docs_sample.py --site-dir site"
        )
    if not (SITE_DIR / "sample" / "index.html").exists():
        pytest.fail(
            f"{SITE_DIR / 'sample'} missing. Run: "
            "uv run python scripts/build_docs_sample.py --site-dir site"
        )

    port = _free_port()
    handler = lambda *a, **kw: _QuietHandler(*a, directory=str(SITE_DIR), **kw)  # noqa: E731
    server = http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _sample_run_release() -> str:
    """The release the recorded sample run's README names (``| Release | vX |``)."""
    readme = REPO / "docs" / "examples" / "wordpress" / "sample-run" / "README.md"
    for line in readme.read_text(encoding="utf-8").splitlines():
        if line.startswith("| Release |"):
            return line.split("|")[2].strip()
    raise AssertionError(f"{readme} names no release")


def _nav_links(page: Page, origin: str) -> list[str]:
    # `.href` (not `getAttribute("href")`): with `navigation.instant` enabled,
    # Material's client-side router rewrites every link's `href` *attribute* to
    # an absolute URL on load, so reading the attribute directly would make
    # same-site links indistinguishable from external ones. The IDL `.href`
    # property is always the resolved absolute URL regardless, in both cases.
    hrefs = page.eval_on_selector_all("nav a[href], .md-nav a[href]", "els => els.map(e => e.href)")
    return [h for h in hrefs if h.startswith(origin) and not h.startswith("mailto:")]


def test_home_shows_claude_code_cta(page: Page, docs_site: str) -> None:
    page.goto(docs_site)
    page.wait_for_load_state("networkidle")
    content = page.content()
    assert "/modernize" in content
    assert "Claude Code" in content
    cta = page.get_by_role("link", name=re.compile("Get started with Claude Code", re.I))
    assert cta.count() > 0


def test_search_returns_results(page: Page, docs_site: str) -> None:
    page.goto(docs_site)
    page.wait_for_load_state("networkidle")
    page.keyboard.press("/")  # Material's search shortcut
    search_input = page.locator("input[data-md-component='search-query']")
    search_input.wait_for(state="visible", timeout=5000)
    # Material's search listens for keystrokes, not a synthetic "input" event --
    # click + type (not .fill()) so the real keyup handler fires.
    search_input.click()
    search_input.type("schema", delay=50)
    results = page.locator("[data-md-component='search-result'] a")
    results.first.wait_for(state="visible", timeout=5000)
    assert results.count() > 0


def test_sample_report_stays_in_theme(page: Page, docs_site: str) -> None:
    # #364: the sample page is a normal themed MkDocs page, not a standalone
    # one build_docs_sample.py generates by overwriting index.html -- so the
    # site header, search and nav sidebar are all still present.
    page.goto(f"{docs_site}/sample/")
    page.wait_for_load_state("networkidle")
    assert page.locator(".md-header").count() > 0
    assert page.locator(".md-nav--primary").count() > 0
    content = page.content()
    assert "Sample report" in content
    # Explains what the reader is looking at: one example, and how it was
    # produced -- a real model run at a named release (#1260), with schemas.
    assert "wordpress" in content.lower()
    assert "real model run" in content
    assert _sample_run_release() in content
    assert "schema designs" in content


def test_sample_report_deliverables_resolve(page: Page, docs_site: str) -> None:
    page.goto(f"{docs_site}/sample/")
    page.wait_for_load_state("networkidle")
    links = page.locator("#sample-deliverables a")
    assert links.count() > 0
    # Each listed deliverable actually resolves (no 404), rather than just
    # existing as a link.
    for i in range(links.count()):
        href = links.nth(i).get_attribute("href")
        if not href or href.startswith(("http://", "https://", "..")):
            continue
        resp = page.request.get(f"{docs_site}/sample/{href}")
        assert resp.status < 400, f"{href} returned {resp.status}"


def test_no_nav_link_returns_404(page: Page, docs_site: str) -> None:
    # Not the home page: its front matter hides the nav sidebar for a clean
    # hero layout (docs/index.md's `hide: [navigation]`), so it has no nav
    # links of its own to check.
    page.goto(f"{docs_site}/get-started/")
    page.wait_for_load_state("networkidle")
    hrefs = _nav_links(page, docs_site)
    assert hrefs, "expected at least one nav link on the get-started page"

    checked: set[str] = set()
    for url in hrefs:
        if url in checked:
            continue
        checked.add(url)
        resp = page.request.get(url)
        assert resp.status < 400, f"{url} returned {resp.status}"


# ---------------------------------------------------------------------------
# The sample engineering report page (#987): the retired HTML reports were
# rendered in a browser, offline, for leaked placeholders, horizontal overflow
# and accessibility. The help site's rendering of the engineering report is the
# HTML that remains, so it gets the same checks.
# ---------------------------------------------------------------------------

ENGINEERING_PAGE = "sample/engineering-report.html"
BLOCKING_IMPACTS = ("critical", "serious", "moderate")


def _open_offline(page: Page, url: str, origin: str) -> dict[str, list[str]]:
    """Load ``url`` with every request off the site blocked; collect errors."""
    events: dict[str, list[str]] = {"console": [], "pageerror": [], "blocked": []}
    page.on("console", lambda m: events["console"].append(m.text) if m.type == "error" else None)
    page.on("pageerror", lambda e: events["pageerror"].append(str(e)))

    def guard(route) -> None:  # type: ignore[no-untyped-def]
        if route.request.url.startswith(origin):
            route.continue_()
        else:
            events["blocked"].append(route.request.url)
            route.abort()

    page.route("**/*", guard)
    page.goto(url)
    page.wait_for_load_state("networkidle")
    return events


def test_sample_engineering_report_renders_cleanly_offline(page: Page, docs_site: str) -> None:
    from src.tools.validation.customer_text_guard import scan_text

    events = _open_offline(page, f"{docs_site}/{ENGINEERING_PAGE}", docs_site)
    assert events == {"console": [], "pageerror": [], "blocked": []}
    text = page.inner_text("body")
    for heading in ("Engineering Report", "Target engines", "Migration roadmap"):
        assert heading in text, heading
    leaked = placeholder_leaks(text, ("undefined", "NaN", "null"))
    leaked += [s for s in ("[object Object]", "{{") if s in text]
    assert leaked == []
    findings = scan_text(text, "sample-engineering-report:body")
    assert findings == [], [(f.kind, f.text, f.excerpt) for f in findings]


@pytest.mark.parametrize("width", [1280, 1920])
def test_sample_engineering_report_has_no_horizontal_overflow(
    page: Page, docs_site: str, width: int
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    _open_offline(page, f"{docs_site}/{ENGINEERING_PAGE}", docs_site)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth + 1")


def test_sample_engineering_report_has_no_moderate_or_worse_accessibility_violations(
    page: Page, docs_site: str
) -> None:
    from axe_playwright_python.sync_playwright import Axe

    _open_offline(page, f"{docs_site}/{ENGINEERING_PAGE}", docs_site)
    violations = Axe().run(page).response["violations"]
    blocking = [v["id"] for v in violations if v.get("impact") in BLOCKING_IMPACTS]
    assert blocking == [], blocking
    # One <main>, every piece of content in a landmark, no skipped heading level.
    found = {v["id"] for v in violations} & {"landmark-one-main", "region", "heading-order"}
    assert found == set(), sorted(found)
