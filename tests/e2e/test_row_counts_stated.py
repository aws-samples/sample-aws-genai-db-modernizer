"""The deliverables state the tables the source has no row count for (#1636).

Before #1636 a table with no statistics reached every figure as an empty one: 265 of
discourse's 311 tables. wordpress's 31 InnoDB zeros are one-page empty tables and stay
0, so its deliverables claim no unknown count. The collector output keeps an unknown
count as ``row_count`` None,
synthesis counts them once (``facts.row_counts``) and the engineering report and the
executive PDF print the one sentence with the sizing assumption that stands in for
them. A report written before the fact (a replayed one) states nothing.
"""

from __future__ import annotations

import json

from src.report.facts import report_facts
from tests.e2e.deliverable_figures import md_plain, pdf_page_texts
from tests.e2e.pipeline import PipelineResult

UNKNOWN = {"wordpress": (0, 50), "discourse": (265, 311), "trailgear": (0, 30)}


def _collected_tables(run: PipelineResult) -> list[dict]:
    path = run.job_dir() / "collector" / "output.json"
    tables: list[dict] = json.loads(path.read_text())["database_schema"]["tables"]
    return tables


def test_the_facts_count_the_collections_unknown_row_counts(run: PipelineResult) -> None:
    facts = report_facts(json.loads(run.report_json().read_text())).get("row_counts")
    if facts is None:  # a report written before #1636 (the replayed run)
        return
    tables = _collected_tables(run)
    assert facts["table_count"] == len(tables)
    assert facts["unknown_count"] == sum(1 for t in tables if t["row_count"] is None)
    if run.db in UNKNOWN and not run.external:
        assert (facts["unknown_count"], facts["table_count"]) == UNKNOWN[run.db]


def test_every_deliverable_states_the_sizing_assumption(run: PipelineResult) -> None:
    facts = report_facts(json.loads(run.report_json().read_text())).get("row_counts") or {}
    statement = facts.get("statement")
    md = md_plain(run.deliverable("engineering-report.md").read_text(encoding="utf-8"))
    pdf = " ".join(" ".join(pdf_page_texts(str(run.deliverable("executive-report.pdf")))).split())
    if not statement:
        assert "Row counts:" not in md
        assert "Row counts:" not in pdf
        return
    assert f"Row counts: {statement}." in md
    assert f"Row counts: {statement}." in pdf
    assert statement.endswith("not as 0") or "stand-in" in statement
