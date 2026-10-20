"""Every local UI link a Claude Code command prints opens a real UI route (#439).

`/assign` once pointed users at ``http://localhost:3000/assignments``, which no
route matches, so the catch-all sent them to the dashboard instead of the
assignment approval gate.

Since #1233 the commands quote the `ui_url` that `start_local_ui.py --page`
prints and show only its shape (`http://localhost:<port>/...`), so both the
script's job pages and those documented shapes must open a route.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMANDS_DIR = REPO_ROOT / ".claude" / "commands"
APP_ROUTES = REPO_ROOT / "src" / "ui" / "src" / "AppRoutes.js"

UI_LINK = re.compile(r"https?://localhost:(?:\d+|<port>)(/(?:<[^>\s]+>|[^\s)<>\"'`])*)?")
ROUTE_PATH = re.compile(r'<Route\s+path="([^"]+)"')
# A placeholder segment in a command (`<jobId>`, `{job_id}`) stands for a route parameter.
PLACEHOLDER = re.compile(r"^(<[^>]+>|\{[^}]+\})$")


def _routes() -> list[list[str]]:
    paths = ROUTE_PATH.findall(APP_ROUTES.read_text(encoding="utf-8"))
    return [p.strip("/").split("/") for p in paths if p != "*"]


def _matches(link_path: str, route: list[str]) -> bool:
    segments = link_path.strip("/").split("/")
    if len(segments) != len(route):
        return False
    return all(
        (r.startswith(":") and bool(PLACEHOLDER.match(s))) or s == r
        for s, r in zip(segments, route, strict=True)
    )


def _command_links() -> list[tuple[str, str]]:
    links = []
    for path in sorted(COMMANDS_DIR.glob("*.md")):
        for match in UI_LINK.finditer(path.read_text(encoding="utf-8")):
            links.append((path.name, match.group(1) or "/"))
    return links


def test_route_matcher_rejects_unknown_paths() -> None:
    routes = _routes()
    assert any(_matches("/analysis/assignments/{job_id}", r) for r in routes)
    assert any(_matches("/analysis/assignments/<jobId>", r) for r in routes)
    assert not any(_matches("/assignments", r) for r in routes)
    # A literal segment never stands in for a parameter.
    assert not any(_matches("/analysis/assignments/assignments", r) for r in routes)


@pytest.mark.passes_on_main  # guards the scan pattern itself, not #1233's change
def test_commands_print_ui_links() -> None:
    assert _command_links(), "no localhost link found; the scan pattern is stale"


def test_every_page_start_local_ui_prints_opens_a_route() -> None:
    from scripts.start_local_ui import JOB_PAGES

    routes = _routes()
    broken = [
        f"{page}: {template}"
        for page, template in JOB_PAGES.items()
        if not any(_matches(template, r) for r in routes)
    ]
    assert not broken, f"--page URLs that match no route in AppRoutes.js: {broken}"


def test_every_command_ui_link_opens_a_route() -> None:
    routes = _routes()
    broken = [
        f"{name}: {link}"
        for name, link in _command_links()
        if link != "/" and not any(_matches(link, r) for r in routes)
    ]
    assert not broken, f"UI links that match no route in AppRoutes.js: {broken}"
