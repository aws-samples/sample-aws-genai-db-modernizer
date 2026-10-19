"""Close calls in the engineering report and the PDF report (#730).

``facts.close_calls`` groups the queries an engine won by less than the
close-call margin over another engine the plan keeps; both reports state
each group, its margin and alternative, and how the customer moves them
(the assignment review). They only format the facts (ADR-032).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from src.report.renderers import render_engineering_report_md
from src.shared.report_facts import CLOSE_CALL_IDS_SHOWN, compute_report_facts
from tests.executive_pdf import engineering_text, executive_text

MD_HEADING = "## Close calls"


def _row(
    qid: str, engine: str, alt: str, gap: float, basis: str = "query", tie: bool = False
) -> dict[str, Any]:
    return {
        "settled_tie": tie,
        "query_id": qid,
        "engine": engine,
        "alternative": alt,
        "score": 80.0,
        "alternative_score": 80.0 - gap,
        "gap": gap,
        "basis": basis,
    }


def _report(rows: list[dict[str, Any]] | None) -> dict[str, Any]:
    digest: dict[str, Any] = {"has_ledger": True, "query_count": 20, "queries": []}
    if rows is not None:
        digest["close_calls"] = rows
    report: dict[str, Any] = {
        "database_name": "wordpress",
        "job_id": "job-1",
        "timestamp": "2026-10-15T00:00:00Z",
        "ranking": [
            {"target": "dynamodb", "confidence_score": 70, "workload_percent": 60.0},
            {"target": "aurora_mysql", "confidence_score": 80, "workload_percent": 40.0},
        ],
        "recommended_architecture": {"databases": [{"service": "dynamodb", "table_count": 1}]},
        "schema_designs": {},
        "tied_queries": [],
        "query_traces": digest,
    }
    report["facts"] = compute_report_facts(report)
    return report


ROWS = [
    _row("q-a", "dynamodb", "aurora_mysql", 0.0, tie=True),
    _row("q-b", "dynamodb", "aurora_mysql", 1.0, basis="group_mean"),
    _row("q-c", "dynamodb", "aurora_mysql", 4.0),
    _row("q-z", "aurora_mysql", "dynamodb", 2.0),
    _row("q-t", "aurora_mysql", "documentdb", 0.0, tie=True),
]


@pytest.fixture
def report() -> dict[str, Any]:
    return _report(ROWS)


def _section_md(md: str) -> str:
    start = md.index(MD_HEADING)
    end = md.find("\n## ", start + 1)
    return md[start:] if end == -1 else md[start:end]


def test_the_engineering_report_states_each_pair_and_how_to_move_them(report) -> None:
    text = engineering_text(report)
    # The headline is the real close calls; the settled ties are stated apart.
    assert "Close calls (3 queries), plus 2 exact ties a rule settled" in text
    assert "less than 5 points" in text
    assert "assignment review" in text
    assert "Why this engine" in text


def test_the_executive_report_leaves_the_close_calls_to_the_engineering_report(report) -> None:
    """#1020: the briefing shows totals and decisions needed; a close call is not a
    pending choice (the assessment decided it), so it stays in the engineering report."""
    assert "Close calls" not in executive_text(report)


def test_the_engineering_report_tables_the_same_pairs(report) -> None:
    section = _section_md(render_engineering_report_md(report))
    assert section.startswith("## Close calls (3 queries), plus 2 exact ties a rule settled")
    assert "| DynamoDB | Aurora MySQL | 2 | 1 to 4 | 1 | 1 | `q-b`, `q-c`, `q-a` |" in section
    assert "| Aurora MySQL | DynamoDB | 1 | 2 | 0 | 0 | `q-z` |" in section
    assert "| Aurora MySQL | DocumentDB | 0 | - | 1 | 0 | `q-t` |" in section
    assert "`query_traces.close_calls`" in section
    assert "assignment review" in section


def test_the_reports_agree_on_every_pair(report) -> None:
    text = engineering_text(report)
    md = _section_md(render_engineering_report_md(report))
    for pair in report["facts"]["close_calls"]["engine_pairs"]:
        assert f"| {pair['pair_query_count']} |" in md
        for qid in pair["query_ids"]:
            assert f"`{qid}`" in md
            assert qid in text


def test_more_than_the_cap_names_the_closest_and_counts_the_rest() -> None:
    n = CLOSE_CALL_IDS_SHOWN + 2
    rows = [_row(f"q{i:02d}", "documentdb", "aurora_postgresql", 1.0) for i in range(n)]
    report = _report(rows)
    md = _section_md(render_engineering_report_md(report))
    assert "(+2 more)" in md
    assert "`q00`" in md
    assert f"q{n - 1:02d}" not in md


@pytest.mark.parametrize("rows", [None, []])
def test_no_section_without_close_calls(rows) -> None:
    report = _report(rows)
    assert "Close calls (" not in engineering_text(report)
    assert MD_HEADING not in render_engineering_report_md(report)


def test_the_renderers_print_the_stored_facts_not_their_own_count(report) -> None:
    doctored = copy.deepcopy(report)
    doctored["facts"]["close_calls"]["close_call_query_count"] = 99
    doctored["facts"]["close_calls"]["settled_tie_count"] = 0
    doctored["facts"]["close_calls"]["engine_pairs"][0]["pair_query_count"] = 42
    text = engineering_text(doctored)
    md = _section_md(render_engineering_report_md(doctored))
    assert "Close calls (99 queries)" in text and md.startswith("## Close calls (99 queries)")
    assert "| DynamoDB | Aurora MySQL | 42 |" in md


def test_query_ids_are_escaped() -> None:
    hostile = _report([_row("q<script>|`x", "dynamodb", "aurora_mysql", 1.0)])
    md = _section_md(render_engineering_report_md(hostile))
    assert "<script>" not in md
    row = next(line for line in md.splitlines() if line.startswith("| DynamoDB"))
    # Six columns (no pair is compared on a group mean, #951): the id's pipe does
    # not open a cell.
    assert row.count(" | ") == 5


def test_an_unknown_engine_id_is_escaped_in_the_engineering_report() -> None:
    hostile = _report([_row("q1", 'x"><script>|', "aurora_mysql", 1.0)])
    md = _section_md(render_engineering_report_md(hostile))
    assert "<script>" not in md
    row = next(line for line in md.splitlines() if "q1" in line)
    # Six columns (no group-mean column, #951): the engine id's pipe does not open a cell.
    assert row.count(" | ") == 5
