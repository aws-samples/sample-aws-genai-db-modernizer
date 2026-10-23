"""The executive summary leads with the recommendation, the risk and the decision (#1403).

Synthesis computes the facts before the summary, so the deterministic summary, the model's
request and the checks on its answer all read the same figures; a model summary that
leaves out the cost, the risk level or the decision, claims a saving with no source cost,
or runs over the word budget falls back to the deterministic one with the reason recorded.
"""

from __future__ import annotations

import pytest

from src.agents.referee.synthesis_handler import (
    apply_synthesis_llm_output,
    prepare_synthesis_llm_input,
    run_synthesis_deterministic,
)
from src.report.formatting import fallback_withheld_reason
from src.shared.executive_brief import SUMMARY_WORD_BUDGET, executive_brief_text, usd
from src.shared.report_facts import executive_brief_facts, refresh_risk_facts
from tests.unit.agents.referee.test_synthesis_llm_seam import _mock_store


@pytest.fixture
def det() -> dict:
    result: dict = run_synthesis_deterministic("job-1", "mydb", _mock_store())
    return result


@pytest.fixture
def det_with_wave(det: dict) -> dict:
    """The fixture with a wave 1 to approve (the mock store's assignment has no waves)."""
    brief = det["facts"]["executive_brief"]
    brief["waves"] = [{"wave": 1, "engines": ["dynamodb"], "kind": "migration"}]
    brief["decision_wave"] = 1
    return det


def _brief(det: dict) -> str:
    text = executive_brief_text(det["facts"], "mydb")
    assert text
    return text


class TestTheDeterministicResult:
    def test_facts_are_computed_before_the_summary(self, det) -> None:
        facts = det["facts"]
        assert facts["executive_brief"]["overall_risk_level"] == "MEDIUM"
        assert det["executive_summary"] == _brief(det)
        # No cents in the summary: the whole-dollar figure the facts computed (#1403).
        assert facts["executive_brief"]["projected_monthly_cost_whole_usd"] == 120
        assert "$120 per month" in det["executive_summary"]
        assert usd(facts["projected_monthly_cost_usd"]) not in det["executive_summary"]
        assert "overall risk is medium" in det["executive_summary"]

    def test_the_engineering_note_stays_in_summary(self, det) -> None:
        assert det["summary"].startswith("Analyzed ")


def test_the_facts_the_summary_reads_are_the_ones_the_report_writes() -> None:
    """The deterministic summary, the request and the checks read the facts synthesis
    computes before the summary; the writer computes them again from the same fields, so
    the model is never checked against a figure the report does not state."""
    from src.agents.referee.synthesis_handler import _write_synthesis_report
    from src.contracts.synthesis_output import ReportFacts

    store = _mock_store()
    result = run_synthesis_deterministic("job-1", "mydb", store)
    _write_synthesis_report(store, result, 0)
    (key,) = [k for k in store._written if k.endswith("report.json")]
    written = store._written[key]
    assert ReportFacts.model_validate(result["facts"]).model_dump(mode="json") == written["facts"]
    assert written["summary"] == executive_brief_text(written["facts"], "mydb")


class TestTheModelRequest:
    def test_it_carries_the_facts_the_summary_states(self, det) -> None:
        payload = prepare_synthesis_llm_input(det)
        brief = payload["executive_brief"]
        facts = det["facts"]
        assert brief["projected_monthly_cost_usd"] == facts["projected_monthly_cost_usd"]
        assert brief["price_list"] == facts["price_list"]
        assert brief["overall_risk_level"] == facts["executive_brief"]["overall_risk_level"]
        assert brief["exposures"] == facts["executive_brief"]["exposures"]
        assert brief["decision_wave"] == facts["executive_brief"]["decision_wave"]
        assert brief["saving_counted"] is False
        assert [e["workload_percent"] for e in brief["engines"]] == [
            e["workload_percent"] for e in facts["engines"]
        ]
        assert payload["deterministic_summary"] == _brief(det)


class TestTheChecksOnTheModelSummary:
    def test_a_structured_summary_is_used(self, det) -> None:
        text = _brief(det).replace(" against it.", ".")
        out = apply_synthesis_llm_output(det, {"executive_summary": text})
        assert out["summary_source"] == "llm"
        assert out["executive_summary"] == text

    @pytest.mark.parametrize(
        ("edit", "reason"),
        [
            (
                lambda t: t.replace("$120 per month", "$125 per month"),
                "left out the projected cost, the overall risk level or the decision and "
                "stated a cost that doesn't match the report's figures",
            ),
            (
                lambda t: t.replace("medium", "moderate"),
                "left out the projected cost, the overall risk level or the decision",
            ),
            (
                lambda t: t.split("\n\nDecision:")[0],
                "left out the projected cost, the overall risk level or the decision",
            ),
            (
                lambda t: t + " Each engine costs $40.00 a month.",
                "stated a cost that doesn't match the report's figures",
            ),
            (
                lambda t: t + " The plan saves 30% on the bill.",
                "claimed a saving with no source cost to compare against",
            ),
            (
                lambda t: t + " The team has done this many times." * 30,
                "ran over the summary's word budget",
            ),
        ],
    )
    def test_a_broken_summary_falls_back_with_its_reason(self, det_with_wave, edit, reason) -> None:
        det = det_with_wave
        brief = _brief(det)
        assert "approve wave 1" in brief
        llm = edit(brief)
        out = apply_synthesis_llm_output(det, {"executive_summary": llm})
        assert out["summary_source"] == "deterministic_fallback"
        assert out["executive_summary"] == brief
        assert out["summary_llm"] == llm
        assert any("executive structure" in w for w in out["summary_validation_warnings"])
        assert (
            fallback_withheld_reason(
                {"summary_validation_warnings": out["summary_validation_warnings"]}
            )
            == reason
        )

    def test_the_budget_is_the_one_the_skill_states(self) -> None:
        assert SUMMARY_WORD_BUDGET == 200


class TestTheFacts:
    def _report(self, risks: list[dict], tco: dict | None = None) -> dict:
        return {
            "risk_assessment": {"risks": risks},
            "tco_analysis": tco or {"current_cost_known": False, "current_monthly_cost": 0},
            "engines_in_use": ["mysql"],
        }

    def _waves(self) -> list[dict]:
        return [
            {
                "wave": 1,
                "engines": ["aurora_mysql"],
                "homogeneity": "homogeneous",
                "moves_from": ["mysql"],
            },
            {"wave": 2, "engines": ["elasticache"], "fronts": "aurora_mysql", "moves_from": []},
            {"wave": 3, "engines": ["dynamodb"], "moves_from": ["aurora_mysql"]},
        ]

    def test_the_exposures_are_the_risks_at_the_overall_level(self) -> None:
        risks = [
            {"severity": "MEDIUM", "description": "[dynamodb] a", "risk_id": "RISK-001"},
            {"severity": "HIGH", "description": "[dynamodb] b", "risk_id": "RISK-002"},
            {"severity": "LOW", "description": "[aurora_mysql] c", "risk_id": "RISK-003"},
        ]
        brief = executive_brief_facts(self._report(risks), self._waves(), [])
        assert brief["overall_risk_level"] == "HIGH"
        assert [e["risk_id"] for e in brief["exposures"]] == ["RISK-002"]
        # No wave of its own: the first wave of its engine.
        assert brief["exposures"][0]["wave"] == 3

    def test_at_most_two_exposures_in_register_order(self) -> None:
        risks = [
            {
                "severity": "MEDIUM",
                "description": f"[dynamodb] r{i}",
                "risk_id": f"R{i}",
                "wave": 3,
                "rule": "sync.wave",
            }
            for i in range(4)
        ]
        brief = executive_brief_facts(self._report(risks), self._waves(), [])
        assert [e["risk_id"] for e in brief["exposures"]] == ["R0", "R1"]

    def test_waves_and_decision(self) -> None:
        brief = executive_brief_facts(self._report([]), self._waves(), [])
        assert [w["kind"] for w in brief["waves"]] == ["homogeneous", "cache", "migration"]
        assert brief["decision_wave"] == 1
        assert brief["source_engine"] == "mysql"
        assert brief["overall_risk_level"] == "LOW" and brief["exposures"] == []

    def test_no_source_cost_counts_no_saving(self) -> None:
        brief = executive_brief_facts(self._report([]), self._waves(), [])
        assert brief["source_cost_known"] is False
        assert brief["saving_counted"] is False
        assert brief["source_monthly_cost_usd"] is None and brief["savings_percent"] is None

    def test_a_known_source_cost_or_a_retired_engine_counts_a_saving(self) -> None:
        tco = {"current_cost_known": True, "current_monthly_cost": 800.0, "savings_percent": 25.0}
        brief = executive_brief_facts(self._report([], tco), self._waves(), [])
        assert brief["saving_counted"] is True and brief["savings_percent"] == 25.0
        retired = [{"engine": "documentdb", "monthly_cost_usd": 367.39}]
        assert executive_brief_facts(self._report([]), self._waves(), retired)["saving_counted"]

    def test_a_backfilled_risk_refreshes_the_level(self) -> None:
        report = self._report([{"severity": "LOW", "description": "[dynamodb] a"}])
        report["migration_waves"] = self._waves()
        facts = {"executive_brief": executive_brief_facts(report, self._waves(), [])}
        assert facts["executive_brief"]["overall_risk_level"] == "LOW"
        report["risk_assessment"]["risks"].append(
            {"severity": "HIGH", "description": "[dynamodb] b", "risk_id": "RISK-009"}
        )
        refreshed = refresh_risk_facts(facts, report)
        assert refreshed["executive_brief"]["overall_risk_level"] == "HIGH"
        assert refreshed["executive_brief"]["exposures"][0]["risk_id"] == "RISK-009"


def test_the_share_of_all_queries_is_set_only_with_queries_no_engine_serves() -> None:
    report = {
        "risk_assessment": {"risks": []},
        "tco_analysis": {"current_cost_known": False},
        "ranking": [
            {"target": "dynamodb", "assigned_queries": 65},
            {"target": "elasticache", "assigned_queries": 0},
        ],
    }
    assert executive_brief_facts(report, [], [], 65)["share_of_all_queries_percent"] == {}
    report["scope_unservable"] = [{"query_id": f"q{i}", "reason": "x"} for i in range(42)]
    brief = executive_brief_facts(report, [], [], 107)
    assert brief["share_of_all_queries_percent"] == {"dynamodb": 60.7}
