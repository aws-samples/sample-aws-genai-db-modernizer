"""Queries on a fallback engine, in every deliverable (#729).

``facts.unservable`` lists each query a fallback rule placed because no
analyzed engine fit it: where it landed and why, the gate that ruled out each
engine and a redesign hint for those engines. The decision report, the
engineering report, the deck and the interactive report state them and point
at the assignment review to re-run. They only format the facts (ADR-032).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from src.report.renderers import render_decision_report_html, render_engineering_report_md
from src.report.unservable import UNSERVABLE_RERUN_TEXT
from src.shared.capability_labels import CAPABILITY_REDESIGN_HINTS
from src.shared.report_facts import UNSERVABLE_SHOWN, compute_report_facts

MD_HEADING = "## Queries on a fallback engine"


def _row(qid: str, **kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "query_id": qid,
        "engine": "aurora_mysql",
        "engine_name": "Aurora MySQL",
        "kind": "fallback",
        "rule": "fallback.no_score",
        "rule_label": "no engine could score it",
        "explanation": "No engine could score this query, so it falls back to Aurora MySQL.",
        "blocking_gates": [
            {
                "engine": "dynamodb",
                "engine_name": "DynamoDB",
                "gate": "capability",
                "gate_text": "missing a capability the query needs",
            }
        ],
        "required_capabilities": ["aggregation"],
    }
    row.update(kw)
    return row


def _report(rows: list[dict[str, Any]] | None) -> dict[str, Any]:
    digest: dict[str, Any] = {"has_ledger": True, "query_count": 5, "queries": []}
    if rows is not None:
        digest["unservable"] = rows
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


@pytest.fixture
def report() -> dict[str, Any]:
    return _report([_row("q-count")])


def _section_html(html: str) -> str:
    start = html.index("<section data-figure=unservable-queries>")
    return html[start : html.index("</section>", start)]


def _section_md(md: str) -> str:
    start = md.index(MD_HEADING)
    end = md.find("\n## ", start + 1)
    return md[start:] if end == -1 else md[start:end]


def test_the_decision_report_flags_each_query_with_gates_redesign_and_rerun(report) -> None:
    section = _section_html(render_decision_report_html(report))
    assert "Queries on a fallback engine (1 query)" in section
    assert "no kept engine" not in section
    assert "<code>q-count</code> landed on <b>Aurora MySQL</b>" in section
    assert "No engine could score this query" in section
    assert "Ruled out: DynamoDB (missing a capability the query needs)." in section
    assert "Keep it on Aurora MySQL, or redesign it. For DynamoDB: " in section
    assert CAPABILITY_REDESIGN_HINTS["aggregation"]["dynamodb"] in section
    assert "does not bring back an engine it removed" in section
    assert UNSERVABLE_RERUN_TEXT in section


def test_the_engineering_report_tables_the_same_queries(report) -> None:
    section = _section_md(render_engineering_report_md(report))
    assert section.startswith("## Queries on a fallback engine (1 query)")
    row = next(line for line in section.splitlines() if line.startswith("| `q-count`"))
    assert "| Aurora MySQL |" in row
    assert "DynamoDB (missing a capability the query needs)" in row
    assert "| aggregation |" in row
    assert "Keep it on Aurora MySQL, or redesign it. For DynamoDB: " in row
    assert CAPABILITY_REDESIGN_HINTS["aggregation"]["dynamodb"] in row
    assert "`query_traces.unservable`" in section
    assert UNSERVABLE_RERUN_TEXT in section


def test_more_than_the_cap_says_how_many_more() -> None:
    report = _report([_row(f"q{i:03d}") for i in range(UNSERVABLE_SHOWN + 1)])
    html = _section_html(render_decision_report_html(report))
    md = _section_md(render_engineering_report_md(report))
    assert "1 more query: see the Engineering Report." in html
    assert "1 more query is not shown here." in md


@pytest.mark.parametrize("rows", [None, []])
def test_no_section_without_any(rows) -> None:
    report = _report(rows)
    assert "data-figure=unservable-queries" not in render_decision_report_html(report)
    assert MD_HEADING not in render_engineering_report_md(report)


def test_the_renderers_print_the_stored_facts(report) -> None:
    doctored = copy.deepcopy(report)
    doctored["facts"]["unservable"]["unservable_query_count"] = 7
    doctored["facts"]["unservable"]["unservable_queries"][0]["redesign_hints"] = ["Stored hint."]
    html = _section_html(render_decision_report_html(doctored))
    md = _section_md(render_engineering_report_md(doctored))
    assert "(7 queries)" in html and "(7 queries)" in md
    assert "Stored hint." in html and "Stored hint." in md


def test_query_ids_and_text_are_escaped() -> None:
    hostile = _report(
        [_row("q<img src=x>|`", explanation="<script>alert(1)</script> | x", engine='x"><b>')]
    )
    html = _section_html(render_decision_report_html(hostile))
    md = _section_md(render_engineering_report_md(hostile))
    assert "<script>" not in html and "<img" not in html and '"><b>' not in html
    assert "<script>" not in md and "<img" not in md
    row = next(line for line in md.splitlines() if line.startswith("| `q"))
    assert row.count(" | ") == 5  # no injected cell


def test_the_resolvers_own_fallback_reaches_both_reports() -> None:
    """The #786 repro end to end, a synthetic state real runs do not reach
    (triage always selects the source's Aurora engine): a MySQL-source
    ``SHOW TABLES`` with only DynamoDB and DocumentDB analyzed falls back to
    Aurora MySQL. Triage's own detector gives its capabilities; the digest,
    the facts and both reports state where it landed, the two gates and the
    redesign for those two engines."""
    from src.agents.referee.assignment_resolver import AssignmentResolver
    from src.agents.referee.triage import detect_query_capabilities
    from src.shared.query_trace import query_trace_digest

    query = {
        "query_id": "q1",
        "query_text": "SHOW TABLES",
        "query_type": "SELECT",
        "tables_accessed": ["db.users"],
        "join_count": 0,
        "has_joins": False,
        "has_aggregations": False,
        "filter_tables": [],
        "calls_per_second": 1.0,
        "rows_returned_avg": 10,
    }
    engines = ["dynamodb", "documentdb"]
    collector = {
        "job_id": "t",
        "metadata": {"source_database": {"engine": "mysql"}},
        "database_schema": {"tables": [{"table_id": "db.users", "table_name": "users"}]},
        "queries": {"query_patterns": [query]},
    }
    capabilities = detect_query_capabilities(collector, [])
    assert capabilities == {"q1": ["sql_admin"]}
    triage = {
        "selected_agents": [{"agent_type": e} for e in engines],
        "signals": [],
        "query_capabilities": capabilities,
    }
    analysis = {
        e: {
            "table_recommendations": [{"table_id": "db.users", "confidence_score": 95}],
            "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
        }
        for e in engines
    }
    result = AssignmentResolver().resolve(triage, analysis, collector)
    assignment = {**result.model_dump(mode="json", by_alias=True), "version": 1}
    digest = query_trace_digest(assignment, {1: assignment}.get, 1, None, capabilities)
    report = _report(digest.model_dump(mode="json", by_alias=True)["unservable"])
    html = _section_html(render_decision_report_html(report))
    md = _section_md(render_engineering_report_md(report))
    assert "<code>q1</code> landed on <b>Aurora MySQL</b>" in html
    assert (
        "Ruled out: DocumentDB (missing a capability the query needs); DynamoDB (missing a "
        "capability the query needs)." in html
    )
    assert "Keep it on Aurora MySQL, or redesign it. For DocumentDB: this is session" in html
    assert "For DynamoDB: this is session" in html
    assert "| `q1` | Aurora MySQL |" in md


class TestDeckAndInteractiveReport:
    """The deck/PDF state the same queries, as #503 and #629 did."""

    def test_the_deck_adds_a_card_with_the_stored_count(self, report) -> None:
        from src.report import pptx_report

        f = pptx_report.derive(report, {})
        card = next(
            d for d in f["decisions"] if d["question"] == "Keep the queries on a fallback engine?"
        )
        assert card["badge"] == "1 query"
        assert "Queries on a fallback engine (1 query)" in card["against"]
        assert "assignment review" in card["action"]

    @pytest.mark.parametrize("rows", [None, []])
    def test_no_deck_card_without_any(self, rows) -> None:
        from src.report import pptx_report

        f = pptx_report.derive(_report(rows), {})
        questions = [d["question"] for d in f["decisions"]]
        assert "Keep the queries on a fallback engine?" not in questions
