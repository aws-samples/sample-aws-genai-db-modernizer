# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The executive report and the engineering report open with the decision (#2460).

The results page's summary header and the chat summary open with
``facts.executive_brief.decision_headline`` (#2315). Both reports open with the same
line, right under their title, with the readiness of that wave as the results page
shows it. The renderers only format the stored line (ADR-032): a report saved before it
gets it from the facts reader's backfill, and a report with no waves keeps its opening.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.report import escaping, executive_report
from src.report.facts import report_facts
from src.report.renderers import render_engineering_report_md
from tests.executive_pdf import outline, outside_the_page, page_texts, printed

FIXTURES = Path(__file__).parent / "fixtures"
TITLE = "# Database Modernization: Engineering Report"


def _fixture(name: str) -> dict[str, Any]:
    """The fixture with its facts block as the reader returns it, stored on the report."""
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    data["facts"] = report_facts(data)
    return data


def _saved(name: str) -> dict[str, Any]:
    """The fixture as it was saved, before the decision line (#2315)."""
    data: dict[str, Any] = json.loads((FIXTURES / name).read_text())
    return data


def _with_readiness(report: dict[str, Any], readiness: str, warnings: int = 0) -> dict[str, Any]:
    out = copy.deepcopy(report)
    for row in out["facts"]["waves"]:
        row.update(
            readiness=readiness,
            readiness_line=f"{readiness.capitalize()}: the wave's line.",
            open_warning_count=warnings,
        )
    return out


def _headline(report: dict[str, Any]) -> str:
    headline = report_facts(report)["executive_brief"]["decision_headline"]
    assert isinstance(headline, str) and headline
    return headline


def _md_lines(report: dict[str, Any]) -> list[str]:
    return render_engineering_report_md(report).splitlines()


# -- the engineering report ------------------------------------------------------------


def test_the_engineering_report_opens_with_the_stored_headline() -> None:
    report = _with_readiness(_fixture("wordpress_report.json"), "suggested", 2)
    report["facts"]["executive_brief"]["decision_headline"] = "Approve wave 1: as stored"
    lines = _md_lines(report)
    assert lines[:6] == [
        TITLE,
        "",
        "**Approve wave 1: as stored**",
        "",
        "Wave 1: Suggested, 2 warnings to accept",
        "",
    ]
    assert lines[6].startswith("Source database: ")
    assert lines.index("## Executive summary") > 6


def test_the_engineering_report_escapes_the_headline_as_prose() -> None:
    report = _fixture("wordpress_report.json")
    report["facts"]["executive_brief"]["decision_headline"] = "Approve wave 1: move word_press*"
    assert _md_lines(report)[2] == f"**{escaping.md_text('Approve wave 1: move word_press*')}**"
    assert _md_lines(report)[2] == r"**Approve wave 1: move word\_press\***"


def test_an_old_report_s_engineering_report_reads_the_backfilled_headline() -> None:
    report = _saved("wordpress_pre_2170_report.json")
    assert not report["facts"]["executive_brief"].get("decision_headline")
    lines = _md_lines(report)
    assert lines[2] == f"**{escaping.md_text(_headline(report))}**"
    assert (
        lines[2]
        == "**Move wordpress to Aurora MySQL and DynamoDB, and add ElastiCache, in 3 waves**"
    )
    # With no decision wave the readiness is the first wave's, as the results page shows it.
    assert lines[4] == "Wave 1: Not designed"


def test_a_report_with_no_readiness_check_prints_the_headline_alone() -> None:
    from src.shared.decision_headline import decision_readiness_label

    report = _fixture("wordpress_report.json")
    assert decision_readiness_label(report_facts(report)) is None
    lines = _md_lines(report)
    assert lines[2] == "**Approve wave 1: move wordpress to Aurora MySQL**"
    assert lines[3] == ""
    assert lines[4].startswith("Source database: ")


@pytest.mark.passes_on_main  # main's opening; the guard is that #2460 keeps it
def test_a_report_with_no_headline_keeps_its_opening() -> None:
    report = _fixture("wordpress_report.json")
    report["facts"]["executive_brief"]["waves"] = []
    report["facts"]["executive_brief"]["decision_headline"] = None
    lines = _md_lines(report)
    assert lines[:2] == [TITLE, ""]
    assert lines[2].startswith("Source database: ")
    assert not any(line.startswith("Wave 1: ") for line in lines[:8])


def test_the_engineering_report_states_the_headline_once() -> None:
    report = _fixture("wordpress_report.json")
    text = render_engineering_report_md(report)
    assert text.count("Approve wave 1: move wordpress to Aurora MySQL") == 1


# -- the executive report (PDF) ----------------------------------------------------------


def _cover(report: dict[str, Any]) -> str:
    return page_texts(executive_report.render_executive_report_pdf(report))[0]


def test_the_cover_states_the_headline_and_its_wave_s_readiness() -> None:
    report = _with_readiness(_fixture("wordpress_report.json"), "held")
    cover = _cover(report)
    db_line = f"Source database {report['database_name']}"
    headline = printed(_headline(report))
    assert f"{db_line} {headline} Wave 1: Not suggested yet" in cover


def test_the_cover_of_an_old_report_reads_the_backfilled_headline() -> None:
    report = _saved("wordpress_pre_2170_report.json")
    cover = _cover(report)
    assert printed(_headline(report)) in cover
    assert "Wave 1: Not designed" in cover


def test_the_cover_draws_the_headline_as_text_not_markup() -> None:
    report = _fixture("wordpress_report.json")
    report["facts"]["executive_brief"]["decision_headline"] = "Approve wave 1: <b>A & B</b>"
    assert "Approve wave 1: <b>A & B</b>" in _cover(report)


@pytest.mark.passes_on_main  # main's opening; the guard is that #2460 keeps it
def test_a_cover_with_no_headline_keeps_its_opening() -> None:
    report = _fixture("wordpress_report.json")
    report["facts"]["executive_brief"]["waves"] = []
    report["facts"]["executive_brief"]["decision_headline"] = None
    cover = _cover(report)
    assert "Approve wave" not in cover
    assert "Wave 1:" not in cover


@pytest.mark.parametrize(
    "headline",
    [
        "Approve wave 1: move wordpress to Aurora MySQL",
        "Move a_database_with_a_very_long_name_from_the_collector to Aurora PostgreSQL, "
        "DynamoDB, DocumentDB and Amazon OpenSearch Service, and add ElastiCache, in 7 waves",
    ],
)
def test_the_headline_adds_no_page_and_stays_on_the_cover(headline: str) -> None:
    report = _with_readiness(_fixture("wordpress_report.json"), "suggested", 12)
    without = copy.deepcopy(report)
    without["facts"]["executive_brief"].update(waves=[], decision_headline=None)
    data = executive_report.render_executive_report_pdf(without)
    assert "Approve wave" not in page_texts(data)[0]
    report["facts"]["executive_brief"]["decision_headline"] = headline
    with_headline = executive_report.render_executive_report_pdf(report)
    pages = page_texts(with_headline)
    assert len(pages) == len(page_texts(data))
    assert printed(headline) in pages[0]
    assert outline(with_headline) == executive_report.section_headings(report)
    assert outside_the_page(with_headline) == []
