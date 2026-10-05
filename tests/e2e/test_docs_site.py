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


def test_sample_report_opens(page: Page, docs_site: str) -> None:
    page.goto(f"{docs_site}/sample/")
    page.wait_for_load_state("networkidle")
    assert "Sample report" in page.content()
    links = page.locator("ul li a")
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
