# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The summary names each distinct exposure once (#2324).

A risk with no exposure rule is named by its severity, type and engine, so two such
risks read the same. The facts group identical exposures and count them; the summary
names a group once, with its count when there are several.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from src.report.engineering_report import brief_summary
from src.shared.executive_brief import executive_brief_text
from src.shared.report_facts import executive_brief_facts
from tests.unit.shared.test_executive_brief import FACTS

WAVES = [
    {"wave": 1, "engines": ["aurora_mysql"], "homogeneity": "homogeneous", "moves_from": ["mysql"]},
    {"wave": 2, "engines": ["aurora_postgresql"], "moves_from": ["mysql"]},
    {"wave": 3, "engines": ["dynamodb"], "moves_from": ["aurora_mysql"]},
]


def _perf(rid: str, engine: str = "aurora_postgresql", **extra: Any) -> dict[str, Any]:
    return {
        "risk_id": rid,
        "severity": "MEDIUM",
        "risk_type": "PERFORMANCE_DEGRADATION",
        "description": f"[{engine}] slower {rid}",
        **extra,
    }


def _brief(risks: list[dict[str, Any]]) -> dict[str, Any]:
    report = {
        "risk_assessment": {"risks": risks},
        "tco_analysis": {"current_cost_known": False, "current_monthly_cost": 0},
        "engines_in_use": ["mysql"],
    }
    return executive_brief_facts(report, WAVES, [])


def _risk_paragraph(brief: dict[str, Any], open_risks: int) -> str:
    facts = copy.deepcopy(FACTS)
    facts["executive_brief"] = {**facts["executive_brief"], **brief}
    facts["open_risk_count"] = open_risks
    text = executive_brief_text(facts, "trailgear")
    assert text
    return text.split("\n\n")[1]


class TestTheFactsGroupIdenticalExposures:
    def test_one_exposure_counts_one(self) -> None:
        brief = _brief([_perf("R1")])
        assert [(e["risk_id"], e["count"], e["risk_ids"]) for e in brief["exposures"]] == [
            ("R1", 1, ["R1"])
        ]

    def test_two_identical_exposures_are_one_entry_counted_twice(self) -> None:
        brief = _brief([_perf("R1"), _perf("R2")])
        assert len(brief["exposures"]) == 1
        (e,) = brief["exposures"]
        assert (e["risk_id"], e["count"], e["risk_ids"]) == ("R1", 2, ["R1", "R2"])
        assert e["waves"] == [2]

    def test_three_identical_exposures_count_three(self) -> None:
        brief = _brief([_perf("R1"), _perf("R2"), _perf("R3")])
        assert [(e["count"], e["risk_ids"]) for e in brief["exposures"]] == [
            (3, ["R1", "R2", "R3"])
        ]

    def test_mixed_exposures_keep_register_order_and_the_limit_counts_groups(self) -> None:
        risks = [
            _perf("R1"),
            _perf("R2", engine="dynamodb"),
            _perf("R3"),
            _perf("R4", engine="aurora_mysql"),
        ]
        brief = _brief(risks)
        assert [(e["engine"], e["count"], e["risk_ids"]) for e in brief["exposures"]] == [
            ("aurora_postgresql", 2, ["R1", "R3"]),
            ("dynamodb", 1, ["R2"]),
        ]

    def test_a_tie_in_two_waves_is_one_entry_with_both_waves_in_order(self) -> None:
        risks = [_perf("R1", wave=3), _perf("R2", wave=1)]
        (e,) = _brief(risks)["exposures"]
        assert e["count"] == 2 and e["waves"] == [1, 3] and e["wave"] == 3

    def test_a_different_type_or_rule_is_not_grouped(self) -> None:
        risks = [
            _perf("R1"),
            {**_perf("R2"), "risk_type": "DATA_CONSISTENCY"},
            {**_perf("R3"), "rule": "routing.close_calls"},
        ]
        brief = _brief(risks)
        assert [e["count"] for e in brief["exposures"]] == [1, 1]
        assert [e["risk_id"] for e in brief["exposures"]] == ["R1", "R2"]


class TestTheSummaryNamesEachExposureOnce:
    def test_the_trailgear_sentence_reads_two_risks_once(self) -> None:
        risk = _risk_paragraph(_brief([_perf("R1"), _perf("R2")]), 2)
        assert risk == (
            "Risk: overall risk is medium. The main exposures are two medium-severity risks "
            "of slower performance on Aurora PostgreSQL: review them in the risk register "
            "before wave 2 starts."
        )

    def test_three_identical_risks_read_three(self) -> None:
        risk = _risk_paragraph(_brief([_perf("R1"), _perf("R2"), _perf("R3")]), 3)
        assert "three medium-severity risks of slower performance on Aurora PostgreSQL" in risk
        assert risk.count("slower performance") == 1

    @pytest.mark.passes_on_main
    def test_one_risk_reads_as_before(self) -> None:
        risk = _risk_paragraph(_brief([_perf("R1")]), 1)
        assert risk == (
            "Risk: overall risk is medium. The main exposure is a medium-severity risk of "
            "slower performance on Aurora PostgreSQL: review it in the risk register before "
            "wave 2 starts."
        )

    def test_mixed_risks_name_each_group_once(self) -> None:
        risks = [_perf("R1"), {**_perf("R2", engine="dynamodb"), "risk_type": "DATA_CONSISTENCY"}]
        risks.append(_perf("R3"))
        risk = _risk_paragraph(_brief(risks), 3)
        assert risk == (
            "Risk: overall risk is medium. The main exposures are two medium-severity risks "
            "of slower performance on Aurora PostgreSQL and a medium-severity risk to data "
            "consistency on DynamoDB: review them in the risk register before waves 2 and 3 "
            "start."
        )

    def test_one_identical_pair_in_two_waves_names_both_waves(self) -> None:
        risk = _risk_paragraph(_brief([_perf("R1", wave=3), _perf("R2", wave=1)]), 2)
        assert risk.endswith(": review them in the risk register before waves 1 and 3 start.")
        assert risk.count("slower performance") == 1

    @pytest.mark.passes_on_main
    def test_a_stored_exposure_without_a_count_reads_as_one(self) -> None:
        brief = {
            "exposures": [
                {"severity": "HIGH", "risk_type": "PERFORMANCE_DEGRADATION", "rule": None,
                 "engine": "dynamodb", "wave": 3},
            ],
            "overall_risk_level": "HIGH",
        }  # fmt: skip
        risk = _risk_paragraph(brief, 1)
        assert "The main exposure is a high-severity risk of slower performance on DynamoDB" in risk


def _flagged(rid: str, rule: str, engine: str = "aurora_postgresql") -> dict[str, Any]:
    return {
        "risk_id": rid,
        "severity": "HIGH",
        "risk_type": "MIGRATION_COMPLEXITY",
        "rule": rule,
        "description": f"[{engine}] {rule} {rid}",
    }


class TestTheGroupIsWhatTheSummarySays:
    """Two rules with no phrase of their own read by severity, type and engine, so they
    are one exposure; two rules with one phrase are one too (review of #2404)."""

    def test_two_unworded_rules_on_one_engine_are_one_counted_exposure(self) -> None:
        brief = _brief(
            [_flagged("R1", "routing.flagged"), _flagged("R2", "routines.migrate_yourself")]
        )
        assert [(e["count"], e["risk_ids"]) for e in brief["exposures"]] == [(2, ["R1", "R2"])]
        risk = _risk_paragraph({**brief, "overall_risk_level": "HIGH"}, 2)
        assert (
            "The main exposures are two high-severity migration complexity risks on Aurora PostgreSQL:"
            in risk
        )
        assert risk.endswith(": review them in the risk register before wave 2 starts.")

    def test_three_such_risks_read_three_once(self) -> None:
        risks = [
            _flagged("R1", "routing.flagged"),
            _flagged("R2", "routines.migrate_yourself"),
            _flagged("R3", "routing.flagged"),
        ]
        brief = _brief(risks)
        assert [e["count"] for e in brief["exposures"]] == [3]
        risk = _risk_paragraph({**brief, "overall_risk_level": "HIGH"}, 3)
        assert risk.count("migration complexity risk") == 1
        assert "three high-severity migration complexity risks on Aurora PostgreSQL" in risk

    def test_one_phrase_on_two_engines_is_one_exposure(self) -> None:
        risks = [
            {**_perf("R1", engine="dynamodb"), "rule": "sync.wave", "wave": 3},
            {**_perf("R2", engine="aurora_postgresql"), "rule": "sync.wave", "wave": 3},
        ]
        brief = _brief(risks)
        assert [(e["count"], e["risk_ids"]) for e in brief["exposures"]] == [(2, ["R1", "R2"])]

    def test_an_old_block_with_no_count_reads_grouped_through_the_reader(self) -> None:
        facts = copy.deepcopy(FACTS)
        facts["open_risk_count"] = 2
        facts["executive_brief"]["exposures"] = [
            {"severity": "MEDIUM", "risk_type": "PERFORMANCE_DEGRADATION", "rule": None,
             "engine": "aurora_postgresql", "wave": 2, "risk_id": "R1"},
            {"severity": "MEDIUM", "risk_type": "PERFORMANCE_DEGRADATION", "rule": None,
             "engine": "aurora_postgresql", "wave": 3, "risk_id": "R2"},
        ]  # fmt: skip
        text = brief_summary({"facts": facts, "database_name": "trailgear"})
        assert text
        risk = text.split("\n\n")[1]
        assert risk == (
            "Risk: overall risk is medium. The main exposures are two medium-severity risks "
            "of slower performance on Aurora PostgreSQL: review them in the risk register "
            "before waves 2 and 3 start."
        )

    def test_a_counted_exposure_over_two_waves_states_its_action_once(self) -> None:
        risks = [
            {**_perf("R1"), "rule": "routing.close_calls", "wave": 1},
            {**_perf("R2"), "rule": "routing.close_calls", "wave": 2},
        ]
        risk = _risk_paragraph(_brief(risks), 2)
        assert risk.count("confirm where those queries run") == 1
        assert risk.endswith(": confirm where those queries run before waves 1 and 2 start.")
