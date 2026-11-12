# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Each migration target's Scope row sums to the facts' totals (#2162, ADR-032).

On discourse the merged DynamoDB/DocumentDB wave keeps 36 tables current by CDC, but
the Scope rows said 35 (DynamoDB) and 5 (DocumentDB): both engines read 4 of the same
outside-owned tables, and each row counted them. The facts layer now counts each such
table once, in the first wave engine's ``kept_in_sync_tables``; the other engine's
row states it in ``kept_in_sync_counted_under``, so every row still adds up to its
served tables and the rows add up to the wave's figures.
"""

from __future__ import annotations

import copy
from typing import Any

from src.contracts.synthesis_output import ReportFacts
from src.report.engineering_report import render_engineering_report_md
from src.report.facts import report_facts
from src.shared.report_facts import compute_report_facts, engine_served_split
from tests.executive_pdf import executive_text


def _wave1(tables: list[str]) -> dict[str, Any]:
    return {
        "wave": 1,
        "title": "Move to Aurora MySQL",
        "engines": ["aurora_mysql"],
        "moves_from": ["mysql"],
        "homogeneity": "homogeneous",
        "tables": tables,
        "table_count": len(tables),
        "query_count": 10,
        "workload_share_percent": 33.3,
        "share_basis": "queries",
        "rationale": "The whole source database moves first.",
        "gate": "Parity validated.",
    }


def _report() -> dict[str, Any]:
    """Wave 2 merges DynamoDB and DocumentDB.

    DynamoDB serves t1, t2 (its own), t4 and t5 (Aurora's); DocumentDB serves t3 (its
    own), t2 (DynamoDB's) and t4 (Aurora's). The wave keeps t4 and t5 current by CDC:
    2 tables, while the engines' own reads of outside-owned tables are 2 and 1.
    """
    tables = ["db.t1", "db.t2", "db.t3", "db.t4", "db.t5", "db.t6"]
    return {
        "job_id": "job-1",
        "database_name": "db",
        "timestamp": "2026-11-12T00:00:00Z",
        "ranking": [
            {"target": "dynamodb", "confidence_score": 80, "workload_percent": 40.0},
            {"target": "documentdb", "confidence_score": 70, "workload_percent": 26.7},
            {"target": "aurora_mysql", "confidence_score": 90, "workload_percent": 33.3},
        ],
        "recommended_architecture": {
            "architecture_type": "polyglot",
            "databases": [
                {"service": "dynamodb", "table_count": 0},
                {"service": "documentdb", "table_count": 0},
            ],
        },
        "schema_designs": {"aurora_mysql": {"status": "skipped"}},
        "table_mappings": [],
        "tco_analysis": {
            "current_monthly_cost": 500.0,
            "projected_monthly_cost": 600.0,
            "savings_percent": -20.0,
            "cost_breakdown": [
                {"database": "dynamodb", "monthly_cost_usd": 100.0},
                {"database": "documentdb", "monthly_cost_usd": 200.0},
                {"database": "aurora_mysql", "monthly_cost_usd": 300.0},
            ],
        },
        "risk_assessment": {"overall_risk_level": "LOW", "risks": []},
        "source_table_count": len(tables),
        "migration_waves": [
            _wave1(tables),
            {
                "wave": 2,
                "title": "Move key-value and document-shaped data to DynamoDB and DocumentDB",
                "engines": ["dynamodb", "documentdb"],
                "moves_from": ["aurora_mysql"],
                "tables": ["db.t1", "db.t2", "db.t3", "db.t4", "db.t5"],
                "table_count": 5,
                "table_owners": [
                    {"table": "db.t2", "owner": "dynamodb", "sync": "CDC"},
                    {"table": "db.t4", "owner": "aurora_mysql", "sync": "CDC"},
                    {"table": "db.t5", "owner": "aurora_mysql", "sync": "CDC"},
                ],
                "table_counts_by_engine": {"dynamodb": 4, "documentdb": 3},
                "owned_counts_by_engine": {"dynamodb": 2, "documentdb": 1},
                "query_count": 20,
                "workload_share_percent": 66.7,
                "share_basis": "queries",
                "rationale": "Key-value and document reads.",
                "gate": "Parity confirmed.",
            },
        ],
        "tied_queries": [],
    }


def _with_facts(report: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(report)
    out["facts"] = compute_report_facts(out)
    return out


def _targets(facts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {e["engine"]: e for e in facts["engines"] if e["role"] == "Migration target"}


def test_a_table_both_wave_engines_read_is_kept_current_once() -> None:
    facts = _with_facts(_report())["facts"]
    rows = _targets(facts)
    wave = next(w for w in facts["waves"] if w["wave"] == 2)
    assert wave["kept_in_sync_tables"] == 2
    assert rows["dynamodb"]["kept_in_sync_tables"] == 2
    # t4 is DynamoDB's count already: DocumentDB states it as counted there.
    assert rows["documentdb"]["kept_in_sync_tables"] == 0
    assert rows["documentdb"]["kept_in_sync_counted_under"] == {"dynamodb": 1}
    assert rows["dynamodb"]["kept_in_sync_counted_under"] == {}
    assert sum(r["kept_in_sync_tables"] for r in rows.values()) == wave["kept_in_sync_tables"]
    assert sum(r["tables_migrate"] for r in rows.values()) == wave["moving_tables"]
    ReportFacts.model_validate(facts)


def test_every_scope_row_still_adds_up_to_its_served_tables() -> None:
    for row in _targets(_with_facts(_report())["facts"]).values():
        assert row["engine_source_tables"] == (
            row["tables_migrate"]
            + sum(row["moves_to_tables"].values())
            + row["kept_in_sync_tables"]
            + sum(row["kept_in_sync_counted_under"].values())
        )


def test_the_moves_between_wave_engines_are_the_waves_in_wave_entries() -> None:
    facts = _with_facts(_report())["facts"]
    wave = next(w for w in facts["waves"] if w["wave"] == 2)
    moved = sum(sum(r["moves_to_tables"].values()) for r in _targets(facts).values())
    assert moved == len(wave["shared_in_wave"]) == 1


def test_no_overlap_keeps_each_engines_own_count() -> None:
    """When no outside-owned table is read by both engines, nothing is re-attributed."""
    report = _report()
    wave = report["migration_waves"][1]
    wave["table_counts_by_engine"] = {"dynamodb": 3, "documentdb": 3}  # t1, t2, t5 | t2, t3, t4
    split = engine_served_split(report["migration_waves"])
    assert split["dynamodb"]["kept_in_sync"] == 1
    assert split["documentdb"]["kept_in_sync"] == 1
    assert split["documentdb"]["counted_under"] == {}


def test_a_one_engine_wave_has_nothing_counted_elsewhere() -> None:
    report = _report()
    wave = report["migration_waves"][1]
    wave.update(
        engines=["dynamodb"],
        tables=["db.t1", "db.t2", "db.t4", "db.t5"],
        table_count=4,
        table_counts_by_engine=None,
        owned_counts_by_engine=None,
        table_owners=[
            {"table": "db.t4", "owner": "aurora_mysql", "sync": "CDC"},
            {"table": "db.t5", "owner": "aurora_mysql", "sync": "CDC"},
        ],
    )
    split = engine_served_split(report["migration_waves"])
    assert split["dynamodb"] == {"moves_to": {}, "kept_in_sync": 2, "counted_under": {}}


def test_both_reports_print_the_rows_from_the_facts() -> None:
    report = _with_facts(_report())
    md = render_engineering_report_md(report)
    dynamo = "4 source tables served: 2 migrate, 2 kept current by CDC"
    doc = (
        "3 source tables served: 1 migrate, 1 move to DynamoDB, "
        "1 kept current by CDC in DynamoDB’s count"
    )
    assert f"| {dynamo} |" in md
    assert f"| {doc} |" in md
    assert "counted once, in the first engine’s row" in md
    text = executive_text(report)
    assert dynamo in text
    assert doc in " ".join(text.split())


def test_the_rows_print_what_the_facts_say() -> None:
    """Change the stored figure and the text follows: the renderer computes nothing."""
    report = _with_facts(_report())
    for e in report["facts"]["engines"]:
        if e["engine"] == "documentdb":
            e["kept_in_sync_counted_under"] = {"dynamodb": 7}
    md = render_engineering_report_md(report)
    assert "7 kept current by CDC in DynamoDB’s count" in md


def test_an_old_facts_block_gets_the_split_on_this_definition() -> None:
    """A block stored before #2162 counted t4 in both rows; the reader recounts it from
    the waves, the same function synthesis calls (ADR-032 Decision 4)."""
    report = _with_facts(_report())
    for e in report["facts"]["engines"]:
        e.pop("kept_in_sync_counted_under")
        if e["engine"] == "documentdb":
            e["kept_in_sync_tables"] = 1  # the pre-#2162 figure
    # Re-dumped through the contract, the field reads back as None: still old.
    report["facts"] = ReportFacts.model_validate(report["facts"]).model_dump()
    rows = _targets(report_facts(report))
    assert rows["documentdb"]["kept_in_sync_tables"] == 0
    assert rows["documentdb"]["kept_in_sync_counted_under"] == {"dynamodb": 1}
    md = render_engineering_report_md(report)
    assert "1 kept current by CDC in DynamoDB’s count" in md


def test_a_report_with_no_wave_split_keeps_its_stored_figures() -> None:
    """A wave written before the per-engine counts cannot attribute: nothing changes."""
    report = _report()
    for key in ("table_counts_by_engine", "owned_counts_by_engine"):
        report["migration_waves"][1].pop(key)
    rows = _targets(_with_facts(report)["facts"])
    for row in rows.values():
        assert row["kept_in_sync_counted_under"] == {}
