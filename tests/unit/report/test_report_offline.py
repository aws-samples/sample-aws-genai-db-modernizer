"""The interactive report must be self-contained: no network fetches at view time."""

from __future__ import annotations

import re
from pathlib import Path

from src.report import analysis_report as ar

FIXTURE = Path(__file__).parent / "fixtures" / "e2e09_report.json"


def _minimal_export() -> dict:
    return {
        "jobId": "job-x",
        "results": [],
        "schemaDesigns": [],
        "queryJourneys": {"total": 0, "items": []},
    }


def test_chart_js_is_inlined_not_fetched() -> None:
    html = ar.render_analysis_report_html(_minimal_export(), filename="x.html")
    assert "cdn.jsdelivr.net" not in html
    assert re.search(r"Chart\s*=|Chart\.register|chart\.js", html, re.I)


def test_no_external_script_or_stylesheet_urls() -> None:
    html = ar.render_analysis_report_html(_minimal_export(), filename="x.html")
    assert not re.search(r"<script[^>]+src=\"https?://", html)
    assert not re.search(r"<link[^>]+href=\"https?://", html)
