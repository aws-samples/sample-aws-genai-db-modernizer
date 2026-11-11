# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A known query id in model-written design text prints as a code span (#2107).

The discourse e2e-llm run of #2111: a redesign round's DynamoDB group wrote "The redesign
request names a query (2150880814507944816) that is not in this group's input" in a
trade-off, and the engineering report printed the id bare
(``tests/report_concision.bare_ids``). The output gate rewrites only an explicit
citation ("query 4503599627"), so the renderer is the guard: every model-written field in
"Target schemas by engine" and "Migration trade-offs" goes through
``escaping.md_text_with_ids``, as the executive summary and the migration notes already
did (#1078, #2084). Text with no known id renders exactly as before.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.report.formatting import report_query_ids
from src.report.renderers import render_engineering_report_md
from tests.report_concision import bare_ids, known_query_ids, section

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_report.json"


def _report() -> dict[str, Any]:
    report: dict[str, Any] = json.loads(FIXTURE.read_text())
    return report


def _known(report: dict[str, Any]) -> str:
    """A numeric-looking known id, as a PostgreSQL queryid reads, added to the report."""
    qid = "2150880814507944816"
    report["schema_designs"]["dynamodb"]["unsupported_patterns"][0]["query_ids"].append(qid)
    assert qid in report_query_ids(report)
    return qid


def _with(report: dict[str, Any], where: str, text: str) -> None:
    if where == "trade-off impact":
        report["trade_offs"][0]["impact"] = text
    elif where == "trade-off description":
        report["trade_offs"][0]["description"] = text
    elif where == "unsupported pattern":
        report["schema_designs"]["dynamodb"]["unsupported_patterns"][0]["recommendation"] = text
    elif where == "app-layer note":
        report["schema_designs"]["aurora_mysql"]["app_layer_notes"][0]["recommendation"] = text
    else:
        raise AssertionError(where)


WHERE = ["trade-off impact", "trade-off description", "unsupported pattern", "app-layer note"]


@pytest.mark.parametrize("where", WHERE)
def test_a_known_id_in_model_design_text_is_a_code_span(where: str) -> None:
    report = _report()
    qid = _known(report)
    _with(report, where, f"The redesign request names a query ({qid}) that is not in its input.")
    md = render_engineering_report_md(report)
    assert bare_ids(md, known_query_ids(report)) == []
    assert f"\\(`{qid}`\\)" in md


@pytest.mark.parametrize("where", WHERE)
def test_an_8_character_prefix_is_quoted_too(where: str) -> None:
    report = _report()
    qid = _known(report)
    _with(report, where, f"Query {qid[:8]} reads one item.")
    md = render_engineering_report_md(report)
    assert bare_ids(md, known_query_ids(report)) == []
    assert f"`{qid[:8]}`" in md


@pytest.mark.passes_on_main
def test_text_with_no_known_id_renders_as_before() -> None:
    """A number that is no query of the job, and an id already in a code span, stay as
    written: the report changes only where a model named a known id bare."""
    report = _report()
    qid = _known(report)
    _with(report, "trade-off impact", f"Costs 4503599627 bytes; see query `{qid}`.")
    md = render_engineering_report_md(report)
    trade_offs = section(md, "Migration trade-offs")
    assert "4503599627 bytes" in trade_offs
    assert f"`{qid}`" in trade_offs and f"``{qid}``" not in trade_offs
