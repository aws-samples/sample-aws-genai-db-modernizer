# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A wave is suggested only when its design exists, was reviewed, and no deterministic
check holds it (#1992).

The rule (``src.shared.wave_readiness.wave_readiness``) reads facts only: the open
risks, the designs' unserved access patterns and reviews, the rate window's check and
the costs. No model decides it.
"""

from __future__ import annotations

from typing import Any

from src.shared.wave_readiness import (
    READINESS_RULE_NOTE,
    failed_review_count,
    open_review_count,
    risk_wave,
    wave_readiness,
)

WAVES: list[dict[str, Any]] = [
    {"wave": 1, "engines": ["aurora_mysql"], "homogeneity": "homogeneous"},
    {"wave": 2, "engines": ["elasticache"], "fronts": "aurora_mysql"},
    {"wave": 3, "engines": ["dynamodb"]},
]
ALL = {"aurora_mysql", "elasticache", "dynamodb", "documentdb"}


def _risk(severity: str, engine: str | None, wave: int | None = None, **kw: Any) -> dict:
    return {
        "risk_id": kw.get("risk_id", "RISK-001"),
        "severity": severity,
        "risk_type": kw.get("risk_type", "PERFORMANCE_DEGRADATION"),
        "rule": kw.get("rule"),
        "engine": engine,
        "wave": wave,
        "reattributed_from": kw.get("reattributed_from"),
        "receiving_engine_flags": kw.get("receiving_engine_flags", False),
    }


def _rows(**kw: Any) -> dict[int, dict[str, Any]]:
    args: dict[str, Any] = {
        "risks": [],
        "designed_engines": ALL,
        "unserved_by_engine": {},
        "open_reviews_by_engine": {},
        "failed_reviews_by_engine": {},
        "window_representative": None,
        "projected_cost": None,
        "source_cost": None,
    }
    args.update(kw)
    waves = args.pop("waves", WAVES)
    return {r["wave"]: r for r in wave_readiness(waves, **args)}


def _states(rows: dict[int, dict[str, Any]]) -> list[str]:
    return [r["readiness"] for r in rows.values()]


def test_every_designed_wave_is_suggested_when_no_check_holds_it() -> None:
    rows = _rows(risks=[_risk("MEDIUM", "dynamodb"), _risk("LOW", "aurora_mysql")])
    assert _states(rows) == ["suggested"] * 3
    for r in rows.values():
        assert r["suggested"] is True and r["holds"] == [] and r["next_steps"] == []
        assert r["readiness_line"] == "Suggested: no check holds it."


def test_an_open_high_risk_holds_its_engines_wave_only() -> None:
    rows = _rows(risks=[_risk("HIGH", "dynamodb")])
    assert _states(rows) == ["suggested", "suggested", "held"]
    assert rows[3]["suggested"] is False
    hold = rows[3]["holds"][0]
    assert hold["check"] == "open_high_risk" and hold["flagged_by"] is None
    assert rows[3]["readiness_line"] == (
        "Not suggested yet: an open high risk on DynamoDB (RISK-001, performance "
        "degradation). Next step: revise the DynamoDB design and review it again until "
        "no check holds it."
    )


def test_critical_holds_too_and_the_risks_own_wave_wins_over_its_engine() -> None:
    rows = _rows(risks=[_risk("CRITICAL", "dynamodb", wave=2)])
    assert _states(rows) == ["suggested", "held", "suggested"]


def test_a_risk_with_no_engine_and_no_wave_holds_nothing() -> None:
    rows = _rows(risks=[_risk("HIGH", None)])
    assert _states(rows) == ["suggested"] * 3


def test_another_engines_flag_holds_only_when_this_engine_flags_the_query_too() -> None:
    """A re-attributed flag (raised by the Aurora MySQL analysis on a query now on
    DynamoDB) holds nothing unless DynamoDB's own analysis or design flags one of the
    same queries; when it holds, the line names the engine that raised it."""
    moved = _risk("HIGH", "dynamodb", reattributed_from="aurora_mysql")
    assert _states(_rows(risks=[moved])) == ["suggested"] * 3
    confirmed = {**moved, "reattributed_from": "documentdb", "receiving_engine_flags": True}
    rows = _rows(risks=[confirmed])
    assert _states(rows) == ["suggested", "suggested", "held"]
    assert rows[3]["holds"][0]["flagged_by"] == "documentdb"
    assert rows[3]["holds"][0]["line"] == (
        "an open high risk on DynamoDB, flagged by the DocumentDB analysis (RISK-001, "
        "performance degradation)"
    )


def test_a_wave_with_no_design_is_neither_suggested_nor_held() -> None:
    rows = _rows(designed_engines={"aurora_mysql", "elasticache"})
    assert _states(rows) == ["suggested", "suggested", "not_designed"]
    assert rows[3]["suggested"] is None and rows[3]["holds"] == []
    assert rows[3]["readiness_line"] == (
        "Not designed yet: run the DynamoDB schema design, then check the wave again."
    )
    assert [s["step"] for s in rows[3]["next_steps"]] == ["design"]


def test_a_hold_on_an_undesigned_wave_sends_it_to_schema_design() -> None:
    rows = _rows(risks=[_risk("HIGH", "dynamodb")], designed_engines={"aurora_mysql"})
    assert rows[3]["readiness"] == "held"
    assert [s["step"] for s in rows[3]["next_steps"]] == ["design"]
    assert rows[3]["next_steps"][0]["line"] == (
        "run the DynamoDB schema design, then check the wave again"
    )


def test_a_failed_review_is_not_reviewed_neither_suggested_nor_held() -> None:
    rows = _rows(failed_reviews_by_engine={"dynamodb": 1})
    assert rows[3]["readiness"] == "not_reviewed" and rows[3]["suggested"] is None
    assert rows[3]["readiness_line"] == (
        "Not reviewed: the DynamoDB design was not reviewed (the review failed). Next "
        "step: review it again, then check the wave again."
    )
    assert [s["step"] for s in rows[3]["next_steps"]] == ["review_again"]


def test_collect_longer_only_for_a_rate_driven_risk_on_a_window_that_failed() -> None:
    hot = _risk("HIGH", "dynamodb", rule="design.hot_partition")
    rows = _rows(risks=[hot], window_representative=False)
    assert [s["step"] for s in rows[3]["next_steps"]] == ["revise_design", "collect_longer"]
    # A performance risk the rate does not drive (a correlated subquery, an analysis
    # flag) gets no longer collection.
    rows = _rows(risks=[_risk("HIGH", "dynamodb")], window_representative=False)
    assert [s["step"] for s in rows[3]["next_steps"]] == ["revise_design"]
    # Nor does a window that passed its check, or was not checked.
    for representative in (True, None):
        rows = _rows(risks=[hot], window_representative=representative)
        assert [s["step"] for s in rows[3]["next_steps"]] == ["revise_design"]


def test_unserved_access_patterns_hold_the_wave() -> None:
    rows = _rows(unserved_by_engine={"dynamodb": 2})
    assert rows[3]["readiness"] == "held"
    assert rows[3]["holds"][0]["check"] == "unserved_patterns"
    assert rows[3]["holds"][0]["line"] == "the DynamoDB design leaves 2 access patterns unserved"
    assert _rows(unserved_by_engine={"dynamodb": 0})[3]["readiness"] == "suggested"


def test_an_open_design_review_holds_the_wave() -> None:
    rows = _rows(open_reviews_by_engine={"elasticache": 1})
    assert _states(rows) == ["suggested", "held", "suggested"]
    assert rows[2]["holds"][0]["line"] == (
        "the last ElastiCache design review did not approve 1 design"
    )


def test_a_wave_one_that_is_not_suggested_holds_every_later_designed_wave() -> None:
    rows = _rows(risks=[_risk("HIGH", "aurora_mysql")])
    assert _states(rows) == ["held", "held", "held"]
    for n in (2, 3):
        assert [h["check"] for h in rows[n]["holds"]] == ["earlier_wave"]
        assert rows[n]["readiness_line"] == (
            "Not suggested yet: wave 1 is not suggested yet, and this wave builds on it. "
            "Next step: wait until wave 1 is suggested."
        )
    # A wave 1 not designed yet holds a designed later wave the same way; an undesigned
    # later wave stays not designed.
    rows = _rows(designed_engines={"elasticache"})
    assert _states(rows) == ["not_designed", "held", "not_designed"]


def test_a_wave_one_that_moves_nothing_holds_no_later_wave() -> None:
    waves = [
        {"wave": 1, "engines": [], "homogeneity": "no_move", "no_move_reason": "x"},
        {"wave": 2, "engines": ["dynamodb"]},
    ]
    rows = _rows(waves=waves, risks=[_risk("HIGH", "dynamodb")])
    assert rows[1]["readiness"] == "suggested" and rows[2]["readiness"] == "held"
    assert [h["check"] for h in rows[2]["holds"]] == ["open_high_risk"]


def test_a_costlier_plan_holds_a_wave_rated_slower() -> None:
    """Never a target rated slower and pricier: with a known source cost under the
    plan's, a medium performance risk holds its wave."""
    risks = [_risk("MEDIUM", "dynamodb")]
    rows = _rows(risks=risks, projected_cost=1310.46, source_cost=900.0)
    assert rows[3]["readiness"] == "held"
    assert rows[3]["holds"][0]["check"] == "slower_and_pricier"
    assert rows[3]["holds"][0]["line"] == (
        "the plan costs more than the source ($1,310 against $900 a month) and DynamoDB "
        "carries an open performance risk (RISK-001)"
    )
    # A cheaper or equal plan, an unknown source cost, a low or another kind of risk:
    # nothing holds it.
    assert _rows(risks=risks, projected_cost=900.0, source_cost=900.0)[3]["suggested"]
    assert _rows(risks=risks, projected_cost=1310.46, source_cost=None)[3]["suggested"]
    low = [_risk("LOW", "dynamodb")]
    assert _rows(risks=low, projected_cost=1310.46, source_cost=900.0)[3]["suggested"]
    other = [_risk("MEDIUM", "dynamodb", risk_type="DATA_CONSISTENCY")]
    assert _rows(risks=other, projected_cost=1310.46, source_cost=900.0)[3]["suggested"]


def test_one_line_per_hold_and_one_step_per_engine_and_kind() -> None:
    rows = _rows(
        risks=[_risk("HIGH", "dynamodb"), _risk("HIGH", "dynamodb", risk_id="RISK-004")],
        unserved_by_engine={"dynamodb": 1},
    )
    assert [h["check"] for h in rows[3]["holds"]] == [
        "open_high_risk",
        "open_high_risk",
        "unserved_patterns",
    ]
    assert [s["step"] for s in rows[3]["next_steps"]] == ["revise_design"]


def test_the_merged_wave_holds_on_either_engine() -> None:
    waves = [*WAVES[:2], {"wave": 3, "engines": ["dynamodb", "documentdb"]}]
    rows = _rows(waves=waves, risks=[_risk("HIGH", "documentdb")])
    assert rows[3]["readiness"] == "held"
    rows = _rows(waves=waves, designed_engines=ALL - {"documentdb"})
    assert rows[3]["readiness"] == "not_designed"


def test_no_waves_no_rows() -> None:
    assert wave_readiness([], risks=[], designed_engines=set()) == []


def test_the_rule_note_names_every_check() -> None:
    for phrase in (
        "design exists",
        "reviewed",
        "open critical or high risk",
        "unserved",
        "costs more than the source",
        "wave 1",
    ):
        assert phrase in READINESS_RULE_NOTE


def test_one_placement_for_holds_and_exposures() -> None:
    from src.shared.report_facts import _exposure_wave

    for risk, engine in (({"wave": 2}, "dynamodb"), ({}, "dynamodb"), ({}, None), ({}, "x")):
        assert _exposure_wave(risk, engine, WAVES) == risk_wave(risk, engine, WAVES)
    assert risk_wave({}, "dynamodb", WAVES) == 3


def test_the_review_counts_read_the_last_review_of_each_design() -> None:
    approved = {"type": "pe_review", "iteration": 1, "verdict": "APPROVED"}
    changes = {"type": "pe_review", "iteration": 0, "verdict": "CHANGES_REQUESTED"}
    error = {"type": "pe_error", "iteration": 1}
    assert open_review_count(None) == 0 and failed_review_count(None) == 0
    assert open_review_count({"trace": [{"type": "designer", "iteration": 0}]}) == 0
    assert open_review_count({"trace": [changes, approved]}) == 0
    assert open_review_count({"trace": [approved, changes]}) == 1
    # A review that failed is neither open nor passed: it is not reviewed.
    assert open_review_count({"trace": [changes, error]}) == 0
    assert failed_review_count({"trace": [changes, error]}) == 1
    assert failed_review_count({"trace": [error, approved]}) == 0
    grouped = {"groups": [{"trace": [changes]}, {"trace": [approved]}, {"trace": [error]}]}
    assert open_review_count(grouped) == 1 and failed_review_count(grouped) == 1
    # An external review recorded under pe_review.
    assert open_review_count({"pe_review": {"source": "external", "verdict": "REJECTED"}}) == 1
    failed = {"pe_review": {"source": "external", "error": "x"}}
    assert open_review_count(failed) == 0 and failed_review_count(failed) == 1


def test_assigned_queries_a_design_marks_unsupported_hold_the_wave() -> None:
    rows = _rows(unsupported_by_engine={"dynamodb": 2})
    assert rows[3]["readiness"] == "held"
    assert rows[3]["holds"][0]["check"] == "unsupported_queries"
    assert rows[3]["holds"][0]["line"] == (
        "the DynamoDB design marks 2 queries it is assigned as unsupported"
    )


def test_a_general_risk_on_a_wave_names_no_engine() -> None:
    rows = _rows(risks=[_risk("HIGH", None, wave=3)])
    hold = rows[3]["holds"][0]
    assert hold["engine"] is None
    assert hold["line"] == "an open high risk (RISK-001, performance degradation)"


def test_each_waves_rounds_and_why_the_last_kept_no_design() -> None:
    from src.shared.wave_readiness import redesign_facts

    def log(*outcomes: str, stopped: str | None = None) -> dict:
        rounds = [
            {"round": n, "engines": ["dynamodb"], "outcome": {"dynamodb": o}}
            for n, o in enumerate(outcomes, start=1)
        ]
        if stopped:
            rounds[-1]["stopped"] = stopped
        rounds[-1]["failed"] = {"dynamodb": "redesign round 2 failed: schema_design_dynamodb"}
        return {"max_rounds": 2, "rounds": rounds}

    def line(redesign: dict) -> str:
        rows = _rows(risks=[_risk("HIGH", "dynamodb")], redesign_by_engine=redesign)
        return str(rows[3]["readiness_line"])

    assert line(redesign_facts(log("improved", "improved"))).startswith(
        "Not suggested yet (redesigned twice, still held): "
    )
    assert line(redesign_facts(log("improved", "did_not_improve"))).startswith(
        "Not suggested yet (redesigned twice, still held) (redesign round 2 did not improve "
        "it; the earlier design is kept): "
    )
    assert "(redesign round 2 failed: schema_design_dynamodb; the earlier design is kept)" in (
        line(redesign_facts(log("improved", "failed")))
    )
    assert line(redesign_facts(log("stopped", stopped="the run's tokens ceiling"))).startswith(
        "Not suggested yet (no further redesign round ran: the run's tokens ceiling): "
    )
    fixed = _rows(redesign_by_engine=redesign_facts(log("fixed")))
    assert (
        fixed[3]["readiness_line"] == "Suggested: no check holds it after it was redesigned once."
    )
    assert redesign_facts(None) == {}
