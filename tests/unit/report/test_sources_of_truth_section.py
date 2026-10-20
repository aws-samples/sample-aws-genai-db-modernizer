"""Sources of truth and replicas, in every deliverable (#992, ADR-031 Amendment 9).

``facts.table_sources`` states, for each written table read on a replica, its
source of truth, the replica engines (the CDC direction runs from the source of
truth to them) and the eventual-consistency risk naming the reads a replica
serves. The engineering report formats it, after its wave plan, and the UI
results page does too (``SourcesOfTruthCallout``). They only format the facts
(ADR-032). The executive report names no table (#1020), so it leaves them out.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.report.escaping import md_text
from src.report.facts import report_facts
from src.report.renderers import render_engineering_report_md
from src.report.sources_of_truth import SOURCES_OF_TRUTH_INTRO, sources_of_truth_md
from src.shared.report_facts import (
    TABLE_SOURCES_SHOWN,
    compute_report_facts,
    shared_replica_risk_text,
)
from tests.executive_pdf import executive_text

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


# #951: the reads are counted, not listed (their ids are replica_read_query_ids),
# and the risk states its bound and what to do.
RISK = (
    "Eventually consistent: 2 reads on Aurora PostgreSQL read a copy kept in sync from "
    "DynamoDB via CDC, so each sees a write only after CDC applies it, behind by the "
    "replication lag; a read that must see its own write should read from DynamoDB."
)


def test_the_engineering_report_states_each_tables_source_replicas_cdc_and_risk(report) -> None:
    section = _section_md(render_engineering_report_md(report))
    assert section.startswith("## Sources of truth and replicas (2 written tables)")
    assert SOURCES_OF_TRUTH_INTRO in section
    row = next(line for line in section.splitlines() if line.startswith("| `trailgear.Shipment`"))
    assert row == "| `trailgear.Shipment` | DynamoDB | DynamoDB → Aurora PostgreSQL | 2 |"
    # #1170: one source of truth, so the risk is stated once for every table.
    assert (
        "Every table above has this risk. " + md_text(shared_replica_risk_text(["dynamodb"]))
        in section.splitlines()
    )
    assert report["facts"]["table_sources"]["replicated_tables"][0]["risk"] == RISK
    assert (
        "1 written table runs every query on the source of truth, with no replica: "
        "trailgear.Customer." in section
    )


def test_the_executive_report_leaves_the_table_sources_to_the_engineering_report(
    report,
) -> None:
    """#1020: the executive report names no table, so the per-table sources of
    truth and replicas stay in the engineering report."""
    text = executive_text(report)
    assert "Sources of truth and replicas" not in text
    assert "trailgear.Shipment" not in text and "Eventually consistent" not in text


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


def test_the_renderers_print_the_stored_facts(report) -> None:
    doctored = copy.deepcopy(report)
    doctored["facts"]["table_sources"]["written_table_count"] = 9
    row = doctored["facts"]["table_sources"]["replicated_tables"][0]
    row["risk"] = "Stored risk."
    # #1232: the facts say whether a row shares the one risk sentence; the
    # renderer never decides it from the prose.
    md = _section_md(render_engineering_report_md(doctored))
    assert "(9 written tables)" in md
    assert "Stored risk." not in md and "Every table above has this risk." in md
    row["shares_replica_risk"] = False
    md = _section_md(render_engineering_report_md(doctored))
    assert "Stored risk." in md and "Every table above has this risk." not in md


OLD_WORDING = (
    Path(__file__).resolve().parents[3]
    / "src/ui/src/components/__tests__/fixtures/oldWordingTableSources.json"
)


def _old_wording() -> dict[str, Any]:
    data: dict[str, Any] = json.loads(OLD_WORDING.read_text(encoding="utf-8"))
    return data


def test_an_older_reports_rows_get_shares_replica_risk_from_the_facts_layer() -> None:
    """#1232: a facts block stored between a5c40e70 and ca763a01 has the read
    counts but the older risk wording and no ``shares_replica_risk``; the facts
    layer fills it in (False for the older wording) and serves the fixture's block."""
    data = _old_wording()
    stored = copy.deepcopy(data["stored"])
    assert all("shares_replica_risk" not in r for r in stored["replicated_tables"])
    served = report_facts({"facts": {"engines": [], "table_sources": stored}})["table_sources"]
    assert served == data["served"]
    assert [r["shares_replica_risk"] for r in served["replicated_tables"]] == [True, False]
    assert stored == data["stored"]  # the stored block itself is not modified


def test_an_older_report_reads_the_same_in_both_renderers() -> None:
    """The engineering report prints the fixture's expected text for that block;
    SourcesOfTruthCallout.test.js checks the results page prints the same."""
    data = _old_wording()
    report = _report(None)
    report["facts"] = {"engines": [], "table_sources": copy.deepcopy(data["stored"])}
    md = "\n".join(sources_of_truth_md(report))
    expected = data["expected"]
    assert md.count(md_text(expected["shared"])) == 1
    own = expected["own_risk"]
    assert f"- `{own['table']}`: {md_text(own['risk'])}" in md
    assert md.count("Eventually consistent: ") == 2


def test_table_names_and_risk_text_are_escaped() -> None:
    row = _row("db.t<img src=x>|`", 'x"><b>', {"aurora_postgresql": ["<script>|"]})
    md = _section_md(render_engineering_report_md(_report([row])))
    assert "<script>" not in md and "<img" not in md
    line = next(line for line in md.splitlines() if line.startswith("| `db.t"))
    assert line.count(" | ") == 3  # no injected cell


def test_writes_moved_after_approval_are_disclosed_with_their_cause() -> None:
    """#992 round 4: a resumed job's saved split, re-routed after approval, is listed
    with the queries moved after approval, its cause in words."""
    report = _report(None)
    report["post_schema_reroutes"] = [
        {
            "query_id": "w2",
            "from_engine": "aurora_postgresql",
            "to_engine": "dynamodb",
            "reason": "Re-routed to one source of truth on DynamoDB",
            "reason_category": "source_of_truth",
            "score_before": 70,
            "score_after": 80,
            "approved_version": 1,
        }
    ]
    report["facts"] = compute_report_facts(report)
    md = render_engineering_report_md(report)
    assert (
        "After you approved the assignment (version 1), 1 write moved so that each "
        "table's writes land on one engine, its source of truth." in md
    )
    assert "its table's writes were moved to one engine, its source of truth" in md
    assert "schema design review found" not in md


# #1232: the results page states the risk once too, in its own strings. They
# must read exactly as the engineering report's shared sentence, and its intro.
def _ui_strings() -> dict[str, str]:
    en = Path(__file__).resolve().parents[3] / "src/ui/src/locales/en.json"
    strings: dict[str, str] = json.loads(en.read_text(encoding="utf-8"))
    return strings


def test_the_results_page_states_the_shared_risk_in_the_engineering_reports_words() -> None:
    from src.shared.engine_names import display_engine

    en = _ui_strings()
    prefix = "analysis-results.sources-of-truth."
    one = en[prefix + "risk-one-source"].replace("{{source}}", display_engine("dynamodb"))
    assert one == shared_replica_risk_text(["dynamodb"])
    sources = ["dynamodb", "documentdb", "aurora_postgresql"]
    many = (
        en[prefix + "risk-many-sources"]
        .replace("{{sources}}", ", ".join(display_engine(s) for s in sources[:-1]))
        .replace("{{last}}", display_engine(sources[-1]))
    )
    assert many == shared_replica_risk_text(sources)
    assert en[prefix + "intro"] == SOURCES_OF_TRUTH_INTRO
    # The leads the engineering report puts before that sentence.
    lead = _section_md(render_engineering_report_md(_report([_shipment()])))
    assert f"{en[prefix + 'risk-lead']} {shared_replica_risk_text(['dynamodb'])}" in lead
    mixed = _report([_shipment(), _row("trailgear.Order", "dynamodb", {"documentdb": ["r9"]})])
    mixed["facts"]["table_sources"]["replicated_tables"][1]["shares_replica_risk"] = False
    other = _section_md(render_engineering_report_md(mixed))
    assert f"{en[prefix + 'risk-lead-other']} {shared_replica_risk_text(['dynamodb'])}" in other


def _shipment() -> dict[str, Any]:
    return _row("trailgear.Shipment", "dynamodb", {"aurora_postgresql": ["r1", "r2"]})
