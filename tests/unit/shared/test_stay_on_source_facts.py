"""The stay-on-source baseline, as report facts (#949, ADR-032).

The deliverables had no cost to compare the recommended plan against when the
collection carries no source billing ("source cost not provided"): the one
comparison the assessment can always make is the plan against keeping every
query on the source-compatible engine. Synthesis records that engine's own
analysed cost (sized for the whole workload) in
``tco_analysis.stay_on_source``; the facts layer states it once, with the
plan's difference, in ``facts.stay_on_source``, and every deliverable prints
that.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.contracts.synthesis_output import ReportFacts, TCOAnalysis
from src.report.facts import report_facts
from src.shared.report_facts import compute_report_facts, stay_on_source_facts

FIXTURES = Path(__file__).resolve().parents[1] / "report" / "fixtures"


def _report(baseline: Any, projected: float = 582.76, engines: int = 3) -> dict[str, Any]:
    ranking = [
        {"target": e, "workload_percent": 100.0 / engines}
        for e in ("aurora_mysql", "dynamodb", "elasticache")[:engines]
    ]
    tco: dict[str, Any] = {"projected_monthly_cost": projected}
    if baseline is not None:
        tco["stay_on_source"] = baseline
    return {"ranking": ranking, "tco_analysis": tco}


class TestStayOnSourceFacts:
    def test_the_plan_costs_more(self) -> None:
        report = _report({"database": "aurora_mysql", "monthly_cost_usd": 318.8})
        assert stay_on_source_facts(report) == {
            "engine": "aurora_mysql",
            "monthly_cost_usd": 318.8,
            "plan_difference_usd": 263.96,
        }

    def test_the_plan_costs_less(self) -> None:
        report = _report({"database": "aurora_postgresql", "monthly_cost_usd": 900.0}, 620.93)
        assert stay_on_source_facts(report)["plan_difference_usd"] == -279.07

    def test_the_same_cost(self) -> None:
        report = _report({"database": "aurora_mysql", "monthly_cost_usd": 100.0}, 100.0)
        assert stay_on_source_facts(report)["plan_difference_usd"] == 0.0

    def test_no_baseline_recorded(self) -> None:
        assert stay_on_source_facts(_report(None)) is None
        assert stay_on_source_facts({}) is None

    @pytest.mark.parametrize(
        "baseline",
        [
            {"database": "aurora_mysql"},
            {"database": "aurora_mysql", "monthly_cost_usd": "318.80"},
            {"database": "aurora_mysql", "monthly_cost_usd": True},
            {"database": "", "monthly_cost_usd": 318.8},
            {"monthly_cost_usd": 318.8},
            "aurora_mysql",
        ],
    )
    def test_a_malformed_baseline_is_none(self, baseline: Any) -> None:
        assert stay_on_source_facts(_report(baseline)) is None

    def test_a_plan_of_only_that_engine_has_nothing_to_compare(self) -> None:
        """An Aurora-only plan *is* the baseline: no comparison line."""
        report = _report({"database": "aurora_mysql", "monthly_cost_usd": 318.8}, 318.8, 1)
        assert stay_on_source_facts(report) is None


class TestInTheFactsBlock:
    def test_compute_report_facts_carries_it_and_the_contract_accepts_it(self) -> None:
        report = json.loads((FIXTURES / "wordpress_report.json").read_text())
        report.pop("facts", None)
        report["tco_analysis"]["stay_on_source"] = {
            "database": "aurora_mysql",
            "monthly_cost_usd": 100.0,
        }
        facts = compute_report_facts(report)
        assert facts["stay_on_source"] == stay_on_source_facts(report)
        assert facts["stay_on_source"]["engine"] == "aurora_mysql"
        ReportFacts.model_validate(facts)
        TCOAnalysis.model_validate(report["tco_analysis"])

    def test_a_block_written_before_it_gains_it_and_keeps_the_rest(self) -> None:
        report = json.loads((FIXTURES / "wordpress_report.json").read_text())
        report["tco_analysis"]["stay_on_source"] = {
            "database": "aurora_mysql",
            "monthly_cost_usd": 100.0,
        }
        stored = compute_report_facts(report)
        expected = stored.pop("stay_on_source")
        stored["engine_count"] = 99  # a stored figure is never recomputed
        report["facts"] = stored
        facts = report_facts(report)
        assert facts["stay_on_source"] == expected
        assert facts["engine_count"] == 99

    def test_an_old_report_with_no_baseline_reads_none(self) -> None:
        report = json.loads((FIXTURES / "wordpress_report.json").read_text())
        report.pop("facts", None)
        report["tco_analysis"].pop("stay_on_source", None)
        assert report_facts(report)["stay_on_source"] is None
