"""Every deliverable states the tables with no row count and the sizing assumption (#1636).

A table the source has no row count for is sized at its collected size over its
declared row width, never as empty (``src.shared.row_counts``). Synthesis computes the
counts once (``facts.row_counts``, ADR-032) and the engineering report, the executive
PDF and the results page print its one sentence as it is. The TCO states the tables
with no collected size, whose storage is not priced.
"""

from __future__ import annotations

from typing import Any

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import _unknown_size_count, build_tco_analysis
from src.report.renderers import render_engineering_report_md
from src.shared.report_facts import compute_report_facts
from src.shared.row_counts import UNKNOWN_ROW_COUNT_ASSUMPTION, row_count_facts
from tests.e2e.deliverable_figures import md_plain
from tests.executive_pdf import executive_text

STATEMENT = (
    "2 of 3 source tables have no row count in the source's statistics; "
    + UNKNOWN_ROW_COUNT_ASSUMPTION
)


def _tables(*counts: int | None) -> list[dict[str, Any]]:
    return [
        {
            "table_id": f"shop.t{i}",
            "row_count": n,
            "row_count_basis": None if n is None else "estimate",
            "size_mb": 1.0,
        }
        for i, n in enumerate(counts)
    ]


def _report(tables: list[dict[str, Any]] | None) -> dict[str, Any]:
    report: dict[str, Any] = {
        "database_name": "shop",
        "recommended_architecture": {"architecture_type": "MULTI_DATABASE", "databases": []},
        "ranking": [{"target": "aurora_postgresql", "workload_percent": 100.0}],
        "risk_assessment": {"overall_risk_level": "LOW", "risks": [], "resolved_risks": []},
        "tco_analysis": {
            "current_monthly_cost": 0.0,
            "current_cost_known": False,
            "projected_monthly_cost": 300.0,
            "savings_percent": 0,
            "cost_breakdown": [{"database": "aurora_postgresql", "monthly_cost_usd": 300.0}],
        },
        "trade_offs": [],
        "schema_designs": {},
        "migration_waves": None,
        "table_mappings": [],
        "query_groups": [],
        "cache_overlay": None,
    }
    report["facts"] = compute_report_facts(
        report, row_counts=row_count_facts(tables) if tables is not None else None
    )
    return report


def test_the_facts_carry_the_row_counts_once() -> None:
    facts = _report(_tables(None, 4, None))["facts"]["row_counts"]
    assert facts["statement"] == STATEMENT
    assert (facts["table_count"], facts["unknown_count"], facts["estimate_count"]) == (3, 2, 1)


def test_the_engineering_report_states_it() -> None:
    md = md_plain(render_engineering_report_md(_report(_tables(None, 4, None))))
    assert f"Row counts: {STATEMENT}." in md


def test_the_executive_report_states_it() -> None:
    pdf = " ".join(executive_text(_report(_tables(None, 4, None))).split())
    assert f"Row counts: {STATEMENT}." in pdf


def test_nothing_is_said_when_every_count_is_known() -> None:
    report = _report(_tables(0, 4, 9))
    assert report["facts"]["row_counts"]["statement"] is None
    assert "Row counts:" not in render_engineering_report_md(report)
    assert "Row counts:" not in executive_text(report)


def test_a_report_without_the_collection_says_nothing() -> None:
    report = _report(None)
    assert report["facts"]["row_counts"] is None
    assert "Row counts:" not in render_engineering_report_md(report)


def test_the_tco_counts_the_tables_with_no_collected_size() -> None:
    tables = [{"size_mb": None}, {"size_mb": 0.0}, {"size_mb": 3.0}, {"size_mb": None}]
    assert _unknown_size_count({"database_schema": {"tables": tables}}) == 2
    assert _unknown_size_count(None) == 0


def _tco_assumptions(sizes: list[float | None]) -> list[str]:
    tables = [{"table_id": f"db.t{i}", "size_mb": s} for i, s in enumerate(sizes)]
    data = SynthesisData(
        job_id="j",
        database_name="db",
        collector={"metadata": {"source_database": {}}, "database_schema": {"tables": tables}},
        engines={
            "dynamodb": EngineArtifacts(
                "dynamodb", analysis={"cost_estimate": {"monthly_cost_usd": 15.2}}
            ),
        },
    )
    return list(build_tco_analysis(data)["assumptions"])


def test_the_tco_states_the_storage_it_does_not_price() -> None:
    assert (
        "2 source tables have no collected size, so no storage is priced for them"
        in _tco_assumptions([None, 1.0, None])
    )
    assert not [a for a in _tco_assumptions([0.0, 1.0]) if "no collected size" in a]
