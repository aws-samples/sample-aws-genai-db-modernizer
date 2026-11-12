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
import json
import logging
from pathlib import Path
from typing import Any

import pytest

from src.contracts.synthesis_output import ReportFacts, SynthesisOutputContract
from src.report.engineering_report import render_engineering_report_md
from src.report.facts import report_facts
from src.shared.report_facts import compute_report_facts, engine_served_split
from tests.executive_pdf import executive_text

# A report saved before #2162 (contract 1.97 shape, built from _report() below): its
# merged wave's engines both read db.t4, so its stored DocumentDB row counts it too.
OLD_OVERLAP_REPORT = Path(__file__).parent / "fixtures" / "report_pre_2162_overlap.json"


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


def test_every_scope_row_adds_up_and_the_rows_add_up_to_the_wave() -> None:
    """Each row's parts are its served tables, and the rows' kept-current counts are the
    wave's: on main the rows say 2 + 1 = 3 against the wave's 2."""
    facts = _with_facts(_report())["facts"]
    rows = _targets(facts)
    for row in rows.values():
        assert row["engine_source_tables"] == (
            row["tables_migrate"]
            + sum(row["moves_to_tables"].values())
            + row["kept_in_sync_tables"]
            + sum((row.get("kept_in_sync_counted_under") or {}).values())
        )
    wave = next(w for w in facts["waves"] if w["wave"] == 2)
    assert sum(r["kept_in_sync_tables"] for r in rows.values()) == wave["kept_in_sync_tables"]


@pytest.mark.passes_on_main(reason="the in-wave moves are unchanged by #2162; guards them")
def test_the_moves_between_wave_engines_are_the_waves_in_wave_entries() -> None:
    facts = _with_facts(_report())["facts"]
    wave = next(w for w in facts["waves"] if w["wave"] == 2)
    moved = sum(sum(r["moves_to_tables"].values()) for r in _targets(facts).values())
    assert moved == len(wave["shared_in_wave"]) == 1


@pytest.mark.passes_on_main(reason="with no shared read nothing is re-attributed, as on main")
def test_no_overlap_keeps_each_engines_own_count() -> None:
    """When no outside-owned table is read by both engines, nothing is re-attributed."""
    report = _report()
    wave = report["migration_waves"][1]
    wave["table_counts_by_engine"] = {"dynamodb": 3, "documentdb": 3}  # t1, t2, t5 | t2, t3, t4
    split = engine_served_split(report["migration_waves"])
    assert split["dynamodb"]["kept_in_sync"] == 1
    assert split["documentdb"]["kept_in_sync"] == 1
    assert not split["documentdb"].get("counted_under")


@pytest.mark.passes_on_main(reason="a one-engine wave keeps its own count, as on main")
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
    assert split["dynamodb"]["kept_in_sync"] == 2
    assert split["dynamodb"]["moves_to"] == {}
    assert not split["dynamodb"].get("counted_under")


def test_a_wave_with_more_than_two_engines_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """No builder makes one (``test_no_migrating_wave_has_more_than_two_engines``); if one
    ever lands, its split cannot be attributed, and the facts layer says so."""
    report = _report()
    report["migration_waves"][1]["engines"].append("opensearch")
    report["migration_waves"][1]["table_counts_by_engine"]["opensearch"] = 0
    report["migration_waves"][1]["owned_counts_by_engine"]["opensearch"] = 0
    with caplog.at_level(logging.WARNING, logger="src.shared.report_facts"):
        engine_served_split(report["migration_waves"])
    assert "has 3 migrating engines" in caplog.text


def test_counts_that_do_not_reconcile_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A wave whose per-engine counts claim more shared reads than either engine has."""
    report = _report()
    report["migration_waves"][1]["table_counts_by_engine"] = {"dynamodb": 9, "documentdb": 3}
    with caplog.at_level(logging.WARNING, logger="src.shared.report_facts"):
        engine_served_split(report["migration_waves"])
    assert "do not reconcile" in caplog.text


def test_both_reports_print_the_rows_from_the_facts() -> None:
    report = _with_facts(_report())
    md = render_engineering_report_md(report)
    dynamo = "4 source tables served: 2 migrate, 2 kept current by CDC"
    doc = (
        "3 source tables served: 1 migrate, 1 move to DynamoDB, "
        "1 kept current by CDC, also read by DynamoDB (counted in its row)"
    )
    assert f"| {dynamo} |" in md
    assert f"| {doc} |" in md
    note = "A table kept current by CDC that DynamoDB and DocumentDB both read is counted once, in the DynamoDB row."
    assert note in md
    text = executive_text(report)
    assert dynamo in text
    flat = " ".join(text.split())
    assert doc in flat
    assert note in flat


def test_the_rows_print_what_the_facts_say() -> None:
    """Change the stored figure and the text follows: the renderer computes nothing."""
    report = _with_facts(_report())
    for e in report["facts"]["engines"]:
        if e["engine"] == "documentdb":
            e["kept_in_sync_counted_under"] = {"dynamodb": 7}
    md = render_engineering_report_md(report)
    assert "7 kept current by CDC, also read by DynamoDB (counted in its row)" in md


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
    assert "1 kept current by CDC, also read by DynamoDB (counted in its row)" in md


def test_a_report_with_no_wave_split_keeps_its_stored_figures() -> None:
    """A wave written before the per-engine counts cannot attribute: the stored row's
    figures survive and it gains an empty ``kept_in_sync_counted_under``."""
    report = _with_facts(_report())
    for key in ("table_counts_by_engine", "owned_counts_by_engine"):
        report["migration_waves"][1].pop(key)
    for e in report["facts"]["engines"]:
        e.pop("kept_in_sync_counted_under")
        if e["engine"] == "documentdb":
            e["engine_source_tables"], e["kept_in_sync_tables"] = 120, 99
            e["moves_to_tables"] = {"dynamodb": 5}
    doc = _targets(report_facts(report))["documentdb"]
    assert (doc["engine_source_tables"], doc["kept_in_sync_tables"]) == (120, 99)
    assert doc["moves_to_tables"] == {"dynamodb": 5}
    assert doc["kept_in_sync_counted_under"] == {}


def test_an_old_report_keeps_its_move_counts_and_subtracts_only_the_shared_reads() -> None:
    """The reader computes only the new field: a stored kept-current count loses the
    shared reads, and the stored move counts stay, even when a recount would differ."""
    report = _with_facts(_report())
    for e in report["facts"]["engines"]:
        e.pop("kept_in_sync_counted_under")
        if e["engine"] == "documentdb":
            e["kept_in_sync_tables"], e["moves_to_tables"] = 1, {"dynamodb": 3}
    doc = _targets(report_facts(report))["documentdb"]
    assert doc["kept_in_sync_tables"] == 0
    assert doc["kept_in_sync_counted_under"] == {"dynamodb": 1}
    assert doc["moves_to_tables"] == {"dynamodb": 3}


def test_a_saved_report_with_a_shared_read_renders_rows_that_add_up() -> None:
    """ADR-032 Decision 4 on a saved file: the report validates, its rows add up to its
    wave, and both reports print the split."""
    report = json.loads(OLD_OVERLAP_REPORT.read_text(encoding="utf-8"))
    stored = {e["engine"]: e for e in report["facts"]["engines"]}
    assert all("kept_in_sync_counted_under" not in e for e in stored.values())
    assert stored["documentdb"]["kept_in_sync_tables"] == 1  # counted in both rows
    SynthesisOutputContract.model_validate(report)
    facts = report_facts(report)
    rows = _targets(facts)
    wave = next(w for w in facts["waves"] if w["wave"] == 2)
    assert sum(r["kept_in_sync_tables"] for r in rows.values()) == wave["kept_in_sync_tables"]
    assert rows["documentdb"]["moves_to_tables"] == stored["documentdb"]["moves_to_tables"]
    assert rows["dynamodb"] == {**stored["dynamodb"], "kept_in_sync_counted_under": {}}
    cell = (
        "3 source tables served: 1 migrate, 1 move to DynamoDB, "
        "1 kept current by CDC, also read by DynamoDB (counted in its row)"
    )
    assert cell in render_engineering_report_md(report)
    assert cell in " ".join(executive_text(report).split())
