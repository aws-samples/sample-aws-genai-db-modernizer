# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``md_cell`` keeps untrusted text inert in a Markdown table cell (#2349).

A cell that holds collection, DMS or model text must not render a remote image, a link or
emphasis when someone opens the engineering report. The property test renders the cell
with the Markdown parser the repo already uses (python-markdown, as ``build_docs_sample``
does) and reads the HTML back.
"""

from __future__ import annotations

import markdown
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from src.report import escaping

HOSTILE = [
    "![x](http://e/x.png)",
    "[x](http://e/)",
    "<http://e/auto>",
    "<b>bold</b> <img src=x onerror=1>",
    "*em* **strong** _em_ __strong__ ~~gone~~",
    "a | b",
]


def _html(cell: str) -> str:
    return str(markdown.markdown(f"x {cell}"))


@pytest.mark.parametrize("raw", HOSTILE)
def test_a_hostile_cell_renders_as_plain_text(raw: str) -> None:
    html = _html(escaping.md_cell(raw))
    for tag in ("<a", "<img", "<em", "<strong", "<del", "<b>", "<script"):
        assert tag not in html, (raw, html)


@pytest.mark.parametrize(
    ("raw", "cell"),
    [
        ("![x](http://e/x.png)", "!\\[x\\](http://e/x.png)"),
        ("~~gone~~", "\\~\\~gone\\~\\~"),
        ("a | b", "a \\| b"),
        ("<b>", "&lt;b&gt;"),
        ("a\\b", "a\\\\b"),
        ("two\nlines", "two lines"),
    ],
)
def test_md_cell_escapes_each_character_once(raw: str, cell: str) -> None:
    assert escaping.md_cell(raw) == cell


def test_a_code_span_in_a_cell_stays_a_span_with_its_pipe_escaped() -> None:
    assert escaping.md_cell("`a_b|c` *x*") == "`a_b\\|c` \\*x\\*"
    assert (
        escaping.md_cell("wp_posts and _lead and trail_ (+2 more) pg_ cat")
        == "wp_posts and \\_lead and trail_ (+2 more) pg_ cat"
    )


@pytest.mark.passes_on_main
def test_a_code_span_cell_is_not_escaped_a_second_time() -> None:
    cell = escaping.md_cell(f"`{escaping.md_code('wp_posts*')}`")
    assert cell == "`wp_posts*`"


def test_md_code_cell_is_a_span_with_literal_content_and_an_escaped_pipe() -> None:
    assert escaping.md_code_cell("a_b|c*") == "`a_b\\|c*`"
    assert escaping.md_code_cell("a`b") == "`ab`"


_ALPHABET = st.sampled_from(list("ab _*~[]()!<>|\\:/.-\n"))


@settings(max_examples=300, deadline=None)
@given(st.lists(_ALPHABET, max_size=40).map("".join))
def test_md_cell_output_renders_as_plain_text(raw: str) -> None:
    cell = escaping.md_cell(raw)
    assert "\n" not in cell
    html = _html(cell)
    for tag in ("<a ", "<a>", "<img", "<em", "<strong", "<del", "<script", "<b>"):
        assert tag not in html, (raw, cell, html)


# One hostile value through each section the issue lists, read back through the parser.

IMG = "![x](http://e/x.png) *em* [l](http://e/)"


def _plain(md_line: str) -> None:
    html = _html(md_line)
    for tag in ("<a ", "<img", "<em", "<strong"):
        assert tag not in html, (md_line, html)


def test_an_auto_decided_reason_is_plain_text() -> None:
    from src.report.engine_choices import auto_decided_md

    report = {
        "auto_decided_choices": [
            {"table": "t", "engine": "aurora_mysql", "query_ids": ["q1"], "reason": IMG}
        ],
        "facts": {"auto_decided_choice_count": 1},
    }
    row = next(line for line in auto_decided_md(report) if "http://e/x.png" in line)
    assert "![x]" not in row and "[l](" not in row and " *em*" not in row
    _plain(row)


def test_an_unservable_explanation_is_plain_text() -> None:
    from src.report.renderers import render_engineering_report_md
    from tests.unit.report.test_unservable_sections import _report, _row, _section_md

    md = _section_md(render_engineering_report_md(_report([_row("q1", explanation=IMG)])))
    row = next(line for line in md.splitlines() if "http://e/x.png" in line)
    assert "![x]" not in row and "[l](" not in row and " *em*" not in row
    _plain(row)
