"""Every deliverable states the date of the list prices behind its cost figures (#1196).

Each estimate records the price table it was priced from: the analysis estimates and,
since #1195, the TCO's own estimate of each kept engine for its assigned queries.
Synthesis states the dates of the estimates the TCO sums once in ``facts.price_list``
(ADR-032), never the table it happens to load,
and the engineering report and the PDF report only format it. A report written before
the fact existed states no date.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.agents.referee.synthesis_handler import (
    _write_synthesis_report,
    run_synthesis_deterministic,
)
from src.contracts.synthesis_output import ReportFacts, SynthesisOutputContract
from src.report.formatting import price_list_text
from src.report.renderers import render_engineering_report_md
from src.shared.catalog import prices as prices_module
from src.shared.catalog.prices import estimates_price_basis, price_basis
from src.shared.report_facts import compute_report_facts
from src.tools.analysis import (
    aurora_common_analysis_tools,
    documentdb_analysis_tools,
    dynamodb_analysis_tools,
    opensearch_analysis_tools,
    redis_analysis_tools,
)
from tests.executive_pdf import executive_text
from tests.unit.agents.referee.test_synthesis_unservable_capabilities import DB, JOB
from tests.unit.agents.referee.test_synthesis_unservable_capabilities import store as seeded_store


def _basis(day: str) -> dict[str, str]:
    return {
        "source": "AWS Price List",
        "region": "us-east-1",
        "pricing_model": "on-demand",
        "retrieved_on": day,
    }


ESTIMATOR_MODULES = (
    aurora_common_analysis_tools,
    documentdb_analysis_tools,
    dynamodb_analysis_tools,
    opensearch_analysis_tools,
    redis_analysis_tools,
)
BASIS = estimates_price_basis({"aurora_mysql": _basis("2026-10-19")})
LINE = "List prices: AWS Price List, us-east-1 on-demand, retrieved 2026-10-19"


def _report(price_list: dict[str, Any] | None, *, stored: bool = True) -> dict[str, Any]:
    report: dict[str, Any] = {
        "database_name": "shop",
        "recommended_architecture": {"architecture_type": "SINGLE_DATABASE", "databases": []},
        "ranking": [{"target": "aurora_mysql", "workload_percent": 100.0, "confidence_score": 80}],
        "risk_assessment": {"overall_risk_level": "LOW", "risks": [], "resolved_risks": []},
        "tco_analysis": {
            "current_monthly_cost": 0.0,
            "current_cost_known": False,
            "projected_monthly_cost": 121.06,
            "savings_percent": 0,
            "cost_breakdown": [{"database": "aurora_mysql", "monthly_cost_usd": 121.06}],
        },
        "trade_offs": [],
        "schema_designs": {},
        "migration_waves": None,
        "table_mappings": [],
        "query_groups": [],
        "cache_overlay": None,
    }
    if stored:
        report["facts"] = compute_report_facts(report, waves=[], price_list=price_list)
    return report


class TestTheBasisOfTheEstimates:
    def test_one_date_when_every_estimate_agrees(self) -> None:
        got = estimates_price_basis(
            {"dynamodb": _basis("2026-10-19"), "aurora_mysql": _basis("2026-10-19")}
        )
        assert got == {
            **_basis("2026-10-19"),
            "engine_dates": {"aurora_mysql": "2026-10-19", "dynamodb": "2026-10-19"},
            "undated_engines": [],
        }
        ReportFacts.model_validate(
            {**compute_report_facts(_report(None, stored=False), waves=[]), "price_list": got}
        )

    def test_each_engines_date_when_they_differ(self) -> None:
        got = estimates_price_basis(
            {"dynamodb": _basis("2026-07-01"), "aurora_mysql": _basis("2026-10-19")}
        )
        assert got is not None and got["retrieved_on"] is None
        assert got["engine_dates"] == {"aurora_mysql": "2026-10-19", "dynamodb": "2026-07-01"}

    def test_an_estimate_written_before_the_date_is_named_undated(self) -> None:
        got = estimates_price_basis({"dynamodb": None, "aurora_mysql": _basis("2026-10-19")})
        assert got is not None
        assert (got["retrieved_on"], got["undated_engines"]) == ("2026-10-19", ["dynamodb"])

    def test_no_date_at_all_is_no_fact(self) -> None:
        assert estimates_price_basis({"dynamodb": None, "aurora_mysql": {}}) is None
        assert estimates_price_basis({}) is None

    def test_a_report_rebuilt_without_it_has_none(self) -> None:
        facts = compute_report_facts(_report(None, stored=False), waves=[])
        assert facts["price_list"] is None
        assert ReportFacts.model_validate(facts).price_list is None


class TestTheDeliverablesStateIt:
    def test_formatting(self) -> None:
        assert price_list_text({"price_list": BASIS}) == LINE.removeprefix("List prices: ")
        assert price_list_text({"price_list": None}) == ""
        assert price_list_text({}) == ""

    def test_formatting_each_engines_date(self) -> None:
        basis = estimates_price_basis(
            {
                "dynamodb": _basis("2026-07-01"),
                "aurora_mysql": _basis("2026-10-19"),
                "elasticache": None,
            }
        )
        assert price_list_text({"price_list": basis}) == (
            "AWS Price List, us-east-1 on-demand, retrieved 2026-10-19 (Aurora MySQL), "
            "2026-07-01 (DynamoDB); not recorded for ElastiCache"
        )

    def test_engineering_report_cost_section(self) -> None:
        md = render_engineering_report_md(_report(BASIS))
        cost = md.split("## Cost", 1)[1].split("\n## ", 1)[0]
        assert f"- {LINE}" in cost

    def test_pdf_report_cost_tile(self) -> None:
        assert LINE in executive_text(_report(BASIS))

    def test_a_report_without_a_price_date_states_none(self) -> None:
        for report in (_report(None), _report(None, stored=False)):
            assert "List prices:" not in render_engineering_report_md(report)
            assert "List prices:" not in executive_text(report)


def _synthesize(tmp_path, estimate_dates: dict[str, str | None]) -> dict[str, Any]:
    """The seeded job, with each engine's estimate dated as given, synthesized."""
    store = seeded_store.__wrapped__(tmp_path)  # type: ignore[attr-defined]
    for engine, day in estimate_dates.items():
        key = f"{DB}/{JOB}/analysis-{engine}/analysis.json"
        analysis = store.read_json(key)
        components = analysis.setdefault("cost_estimate", {}).setdefault("cost_components", {})
        components.pop("price_list", None)
        if day is not None:
            components["price_list"] = _basis(day)
        store.write_json(key, analysis)
    result = run_synthesis_deterministic(JOB, DB, store, assignment_version=1)
    _write_synthesis_report(store, result, assignment_version=1)
    report: dict[str, Any] = store.read_json(f"{DB}/{JOB}/synthesis/v1/report.json")
    SynthesisOutputContract.model_validate(report)
    return report


def _tco_engines(tmp_path) -> list[str]:
    report = _synthesize(tmp_path / "probe", {})
    return [row["database"] for row in report["tco_analysis"]["cost_breakdown"]]


def _refresh_the_table(monkeypatch: pytest.MonkeyPatch, day: str) -> None:
    """The price table as refreshed on ``day``: what every estimate made now records."""
    for module in ESTIMATOR_MODULES:
        monkeypatch.setattr(module, "price_basis", lambda: _basis(day))
    monkeypatch.setattr(prices_module, "price_basis", lambda: _basis(day))


def test_a_job_resynthesized_after_a_refresh_states_the_table_its_tco_was_priced_from(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The analysis estimates were priced on 2026-07-01 and the table was refreshed since.
    Synthesis prices each kept engine for its assigned queries (#1195), on the refreshed
    table, so the report states the refreshed table's date: the one its figures used."""
    engines = _tco_engines(tmp_path)
    assert engines
    _refresh_the_table(monkeypatch, "2027-01-15")
    report = _synthesize(tmp_path / "job", {e: "2026-07-01" for e in engines})
    rows = report["tco_analysis"]["cost_breakdown"]
    assert {r["database"]: r["price_list"]["retrieved_on"] for r in rows} == {
        e: "2027-01-15" for e in engines
    }
    basis = report["facts"]["price_list"]
    assert basis["retrieved_on"] == "2027-01-15"
    assert basis["engine_dates"] == {e: "2027-01-15" for e in sorted(engines)}


def test_an_engine_kept_at_its_analysis_figure_states_the_analysis_date(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no assignment to size from, the TCO keeps the analysis estimate, and the
    report states the date that estimate was priced on, not today's table."""
    engines = _tco_engines(tmp_path)
    _refresh_the_table(monkeypatch, "2027-01-15")
    monkeypatch.setattr(
        "src.agents.referee.synthesis_report.sized_cost_estimate", lambda *a, **k: None
    )
    report = _synthesize(tmp_path / "job", {e: "2026-07-01" for e in engines})
    assert report["facts"]["price_list"]["retrieved_on"] == "2026-07-01"


def test_a_job_whose_estimates_carry_no_date_states_none(tmp_path) -> None:
    engines = _tco_engines(tmp_path)
    report = _synthesize(tmp_path / "job", {e: None for e in engines})
    assert report["facts"]["price_list"] is None


def test_an_estimate_made_now_records_todays_table() -> None:
    from src.tools.analysis.dynamodb_analysis_tools import estimate_dynamodb_costs

    est = estimate_dynamodb_costs(
        {"database_schema": {"tables": []}, "queries": {"query_patterns": []}}
    )
    assert est.cost_components["price_list"] == price_basis()
