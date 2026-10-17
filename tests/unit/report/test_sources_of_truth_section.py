"""Sources of truth and replicas, in every deliverable (#992, ADR-031 Amendment 9).

``facts.table_sources`` states, for each written table read on a replica, its
source of truth, the replica engines (the CDC direction runs from the source of
truth to them) and the eventual-consistency risk naming the reads a replica
serves. The engineering report and the PDF report (in its wave plan) format it;
the UI results page does too (``SourcesOfTruthCallout``). They only format the
facts (ADR-032).
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from src.report.escaping import md_text
from src.report.renderers import render_engineering_report_md
from src.report.sources_of_truth import SOURCES_OF_TRUTH_INTRO
from src.shared.report_facts import TABLE_SOURCES_SHOWN, compute_report_facts
from tests.unit.report.pdf_text import pdf_text

MD_HEADING = "## Sources of truth and replicas"


def _row(table: str, source: str, replicas: dict[str, list[str]]) -> dict[str, Any]:
    return {
        "table": table,
        "source_of_truth": source,
        "write_engines": [source],
        "write_query_ids": [f"{table}-w1", f"{table}-w2"],
        "replicas": [{"engine": e, "read_query_ids": q} for e, q in replicas.items()],
    }


def _report(rows: list[dict[str, Any]] | None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "database_name": "trailgear",
        "job_id": "job-1",
        "timestamp": "2026-10-17T00:00:00Z",
        "ranking": [
            {"target": "dynamodb", "confidence_score": 70, "workload_percent": 60.0},
            {"target": "aurora_postgresql", "confidence_score": 80, "workload_percent": 40.0},
        ],
        "recommended_architecture": {"databases": [{"service": "dynamodb", "table_count": 1}]},
        "schema_designs": {},
        "tied_queries": [],
    }
    if rows is not None:
        report["table_sources"] = rows
    report["facts"] = compute_report_facts(report)
    return report


@pytest.fixture
def report() -> dict[str, Any]:
    return _report(
        [
            _row("trailgear.Shipment", "dynamodb", {"aurora_postgresql": ["r1", "r2"]}),
            _row("trailgear.Customer", "aurora_postgresql", {}),
        ]
    )


def _section_md(md: str) -> str:
    start = md.index(MD_HEADING)
    end = md.find("\n## ", start + 1)
    return md[start:] if end == -1 else md[start:end]


RISK = (
    "Eventually consistent: 2 reads on Aurora PostgreSQL (r1, r2) read a copy kept in sync "
    "from DynamoDB via CDC, so a read can miss a write until it replicates."
)


def test_the_engineering_report_states_each_tables_source_replicas_cdc_and_risk(report) -> None:
    section = _section_md(render_engineering_report_md(report))
    assert section.startswith("## Sources of truth and replicas (2 written tables)")
    assert SOURCES_OF_TRUTH_INTRO in section
    row = next(line for line in section.splitlines() if line.startswith("| `trailgear.Shipment`"))
    assert row == "| `trailgear.Shipment` | DynamoDB | DynamoDB → Aurora PostgreSQL | 2 |"
    assert f"- `trailgear.Shipment`: {md_text(RISK)}" in section.splitlines()
    assert (
        "1 written table runs every query on the source of truth, with no replica: "
        "trailgear.Customer." in section
    )


def test_the_pdf_states_the_same_in_its_wave_plan(report) -> None:
    text = pdf_text(report)
    assert "Sources of truth and replicas (2 written tables)" in text
    # The PDF font has no arrow glyph: it prints "to" (pdf_markdown glyph fallback).
    assert "DynamoDB to Aurora PostgreSQL" in text
    assert " ".join(RISK.split()) in text


def test_more_than_the_cap_says_how_many_more() -> None:
    report = _report(
        [
            _row(f"db.t{i:03d}", "dynamodb", {"aurora_postgresql": ["r"]})
            for i in range(TABLE_SOURCES_SHOWN + 2)
        ]
    )
    section = _section_md(render_engineering_report_md(report))
    assert "2 more replicated tables are not shown here." in section
    assert "`table_sources`" in section


def test_split_writes_are_named_not_hidden() -> None:
    row = _row("db.legacy", "dynamodb", {})
    row.update(source_of_truth=None, write_engines=["aurora_postgresql", "dynamodb"])
    section = _section_md(render_engineering_report_md(_report([row])))
    assert "1 table has writes on more than one engine and no single source of truth" in section


@pytest.mark.parametrize("rows", [None, []])
def test_no_section_without_any(rows) -> None:
    report = _report(rows)
    assert MD_HEADING not in render_engineering_report_md(report)
    assert "Sources of truth and replicas" not in pdf_text(report)


def test_the_renderers_print_the_stored_facts(report) -> None:
    doctored = copy.deepcopy(report)
    doctored["facts"]["table_sources"]["written_table_count"] = 9
    doctored["facts"]["table_sources"]["replicated_tables"][0]["risk"] = "Stored risk."
    md = _section_md(render_engineering_report_md(doctored))
    text = pdf_text(doctored)
    assert "(9 written tables)" in md and "(9 written tables)" in text
    assert "Stored risk." in md and "Stored risk." in text


def test_table_names_and_risk_text_are_escaped() -> None:
    row = _row("db.t<img src=x>|`", 'x"><b>', {"aurora_postgresql": ["<script>|"]})
    md = _section_md(render_engineering_report_md(_report([row])))
    assert "<script>" not in md and "<img" not in md
    line = next(line for line in md.splitlines() if line.startswith("| `db.t"))
    assert line.count(" | ") == 3  # no injected cell
