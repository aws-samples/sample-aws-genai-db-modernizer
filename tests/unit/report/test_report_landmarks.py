"""Both HTML deliverables expose landmarks and a sequential heading outline (#184).

A screen-reader user moves through a page by its landmarks and headings. axe reported
three moderate findings on the reports: no ``<main>`` landmark (``landmark-one-main``),
content outside every landmark (``region``), and a heading level that skipped from h1
to h3 (``heading-order``, decision report). The browser-driven axe scan in
``tests/e2e/test_report_html.py`` is the real check; these are the structural checks
the unit tier can run without a browser.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from src.report import analysis_report as ar
from src.report import renderers

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_report.json"

_BODY = re.compile(r"<body\b[^>]*>(.*)</body>", re.S)
_SCRIPT = re.compile(r"<script\b.*?</script>", re.S)
_HEADING = re.compile(r"<h([1-6])\b")


def _body_without_scripts(html: str) -> str:
    body = _BODY.search(html)
    assert body, "no <body>"
    return _SCRIPT.sub("", body.group(1))


def _assert_sequential_headings(html: str) -> None:
    levels = [int(m.group(1)) for m in _HEADING.finditer(_body_without_scripts(html))]
    assert levels[:1] == [1], levels
    for prev, cur in zip(levels, levels[1:], strict=False):
        assert cur <= prev + 1, f"heading level skips from h{prev} to h{cur}: {levels}"


def _decision_report_html() -> str:
    report = json.loads(FIXTURE.read_text(encoding="utf-8"))
    prov = renderers.provenance(report, "decision-report", "html", job_id="job-1")
    return renderers.render_decision_report_html(report, prov=prov)


def _analysis_report_html() -> str:
    return ar.render_analysis_report_html(
        {
            "jobId": "job-1",
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
            "schemaDesigns": [],
            "queryJourneys": {"total": 0, "items": []},
        }
    )


class TestDecisionReport:
    def test_content_sits_in_banner_main_and_contentinfo(self) -> None:
        body = _body_without_scripts(_decision_report_html())
        assert body.count("<main>") == 1 and body.count("</main>") == 1
        header, rest = body.split("</header>", 1)
        assert header.lstrip().startswith("<header class=hero>") and "<h1>" in header
        main, tail = rest.split("</main>", 1)
        assert main.lstrip().startswith("<div class=wrap><main>")
        # The footer is a sibling of <main>, not a child, so it is still the page's
        # contentinfo landmark, and the provenance note sits inside it.
        assert re.fullmatch(r"\s*<footer>.*</footer>\s*</div>\s*", tail, re.S), tail
        assert "job-1" in tail

    def test_headings_are_sequential(self) -> None:
        _assert_sequential_headings(_decision_report_html())

    def test_tile_figures_are_not_headings(self) -> None:
        html = _decision_report_html()
        tiles = html.split("<div class=tiles>", 1)[1].split("\n</div>", 1)[0]
        assert "<h" not in tiles, tiles
        assert tiles.count("<p class=val>") == 6, tiles


class TestAnalysisReport:
    def test_content_sits_in_one_main(self) -> None:
        body = _body_without_scripts(_analysis_report_html())
        assert body.count("<main") == 1
        assert re.fullmatch(r'\s*<main class="container">.*</main>\s*', body, re.S), body[:200]

    def test_headings_are_sequential(self) -> None:
        _assert_sequential_headings(_analysis_report_html())
