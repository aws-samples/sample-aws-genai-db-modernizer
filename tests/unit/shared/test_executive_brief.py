"""The executive summary's three parts and their checks (#1403).

The deterministic summary words ``facts`` as recommendation, risk and decision; the
checks hold a model's summary to the same structure (the projected cost as the facts
state it, the overall risk level, a decision sentence, no saving without a source cost,
the word budget).
"""

from __future__ import annotations

import copy

import pytest

from src.shared.executive_brief import (
    SUMMARY_WORD_BUDGET,
    check_summary_brief,
    executive_brief_text,
    whole_usd,
    word_count,
)
from src.shared.report_facts import whole_dollars
from src.shared.skill_sections import skill_path

FACTS: dict = {
    "projected_monthly_cost_usd": 504.94,
    "engine_count": 3,
    "migration_wave_count": 3,
    "price_list": {
        "source": "AWS Price List",
        "region": "us-east-1",
        "pricing_model": "on-demand",
        "retrieved_on": "2026-10-19",
        "engine_dates": {},
        "undated_engines": [],
    },
    "engines": [
        {"engine": "aurora_mysql", "role": "Retained", "workload_percent": 62.6},
        {"engine": "dynamodb", "role": "Migration target", "workload_percent": 37.4},
        {
            "engine": "elasticache",
            "role": "Cache layer",
            "workload_percent": None,
            "cached_queries": 20,
            "cached_call_share_percent": 83.4,
        },
    ],
    "waves": [{"wave": 1, "lists_relational_gaps": False}],
    "retired_in_use_engines": [],
    "open_risk_count": 4,
    "executive_brief": {
        "projected_monthly_cost_whole_usd": 505,
        "source_engine": "mysql",
        "source_cost_known": False,
        "source_monthly_cost_usd": None,
        "savings_percent": None,
        "saving_counted": False,
        "waves": [
            {"wave": 1, "engines": ["aurora_mysql"], "kind": "homogeneous"},
            {"wave": 2, "engines": ["elasticache"], "kind": "cache"},
            {"wave": 3, "engines": ["dynamodb"], "kind": "migration"},
        ],
        "overall_risk_level": "MEDIUM",
        "exposures": [
            {"severity": "MEDIUM", "rule": "routing.close_calls", "engine": None, "wave": 3},
        ],
        "decision_wave": 1,
    },
}


def _facts(**brief) -> dict:
    facts = copy.deepcopy(FACTS)
    facts["executive_brief"].update(brief)
    return facts


def _with_engine_cost() -> dict:
    facts = copy.deepcopy(FACTS)
    facts["engines"][1]["monthly_cost_usd"] = 69.19
    return facts


def _text(facts: dict | None = None) -> str:
    text = executive_brief_text(facts or FACTS, "wordpress")
    assert text
    return text


class TestTheDeterministicSummary:
    def test_three_parts_in_order(self) -> None:
        parts = _text().split("\n\n")
        assert [p.split(":", 1)[0] for p in parts] == ["Recommendation", "Risk", "Decision"]

    def test_recommendation_states_the_engines_shares_waves_and_cost(self) -> None:
        recommendation = _text().split("\n\n")[0]
        assert recommendation.startswith(
            "Recommendation: move the wordpress MySQL database to three AWS services in "
            "three waves."
        )
        assert (
            "Wave 1 moves it to Aurora MySQL, which ends up serving 62.6% of the queries"
            in recommendation
        )
        assert "ElastiCache in front of 20 hot reads (83.4% of calls)" in recommendation
        assert "wave 3 moves 37.4% of the queries to DynamoDB" in recommendation
        assert (
            "$505 per month at AWS Price List on-demand prices for us-east-1, retrieved "
            "2026-10-19" in recommendation
        )

    def test_no_source_cost_says_so_and_claims_no_saving(self) -> None:
        text = _text()
        assert "No current cost was collected for the source database" in text
        assert check_summary_brief(text, FACTS) == []

    def test_a_known_source_cost_states_its_saving(self) -> None:
        facts = _facts(
            source_cost_known=True,
            source_monthly_cost_usd=899.5,
            source_monthly_cost_whole_usd=900,
            savings_percent=43.9,
            saving_counted=True,
        )
        text = _text(facts)
        assert "costs an estimated $900 per month today, so the plan saves 43.9%" in text
        assert "No current cost" not in text

    def test_risk_names_the_level_and_where_to_act(self) -> None:
        risk = _text().split("\n\n")[1]
        assert risk == (
            "Risk: overall risk is medium. The main exposure is the queries where two engines "
            "scored almost the same: confirm where those queries run before wave 3 starts."
        )

    def test_two_exposures_in_one_wave_name_it_once(self) -> None:
        facts = _facts(
            exposures=[
                {"severity": "MEDIUM", "rule": "sync.wave", "engine": "dynamodb", "wave": 3},
                {"severity": "MEDIUM", "rule": "routing.close_calls", "engine": None, "wave": 3},
            ]
        )
        risk = _text(facts).split("\n\n")[1]
        assert risk.count("wave 3") == 1
        assert risk.endswith(
            "test the CDC replication and confirm where those queries run before wave 3 starts."
        )

    def test_a_risk_with_no_rule_is_named_in_plain_words(self) -> None:
        facts = _facts(
            overall_risk_level="HIGH",
            exposures=[
                {
                    "severity": "HIGH",
                    "rule": None,
                    "risk_type": "PERFORMANCE_DEGRADATION",
                    "engine": "dynamodb",
                    "wave": 3,
                }
            ],
        )
        risk = _text(facts).split("\n\n")[1]
        assert "a high-severity risk of slower performance on DynamoDB" in risk
        assert "performance degradation" not in risk

    def test_no_open_risks(self) -> None:
        facts = _facts(overall_risk_level="LOW", exposures=[])
        facts["open_risk_count"] = 0
        assert _text(facts).split("\n\n")[1] == "Risk: overall risk is low, with no open risks."

    def test_decision_is_wave_one_and_why_it_is_safe(self) -> None:
        decision = _text().split("\n\n")[2]
        assert decision.startswith("Decision: approve wave 1. It is safe to approve:")
        assert "1:1 to Aurora MySQL, the same engine family as MySQL" in decision

    def test_a_cross_engine_wave_one_is_a_conversion(self) -> None:
        facts = _facts(
            source_engine="sqlserver",
            waves=[{"wave": 1, "engines": ["aurora_postgresql"], "kind": "cross_engine"}],
        )
        facts["waves"] = [{"wave": 1, "lists_relational_gaps": True}]
        text = _text(facts)
        assert "Wave 1 converts it to Aurora PostgreSQL" in text
        assert "from SQL Server to Aurora PostgreSQL and keeps the current data model" in text
        assert "SQL compatibility check lists what to rewrite first" in text

    def test_the_counts_are_the_facts_own(self) -> None:
        """ADR-032: the text reads engine_count and migration_wave_count, never counts."""
        facts = copy.deepcopy(FACTS)
        facts["engine_count"], facts["migration_wave_count"] = 4, 2
        assert "to four AWS services in two waves." in _text(facts)

    def test_within_the_word_budget(self) -> None:
        assert word_count(_text()) <= SUMMARY_WORD_BUDGET

    def test_an_older_report_has_none(self) -> None:
        facts = copy.deepcopy(FACTS)
        del facts["executive_brief"]
        assert executive_brief_text(facts) is None
        assert executive_brief_text(None) is None


class TestTheChecks:
    def _reasons(self, summary: str, facts: dict | None = None) -> list[str]:
        findings = check_summary_brief(summary, facts or FACTS)
        assert all(f["high_confidence"] for f in findings)
        return [f["message"] for f in findings]

    def test_the_deterministic_summary_passes(self) -> None:
        assert self._reasons(_text()) == []

    @pytest.mark.passes_on_main  # the wording a labelled summary may use stays accepted
    @pytest.mark.parametrize(
        "unlabelled",
        [
            "The projected cost of the full modernization is $505 per month",
            "The projected cost of the full-modernization plan is $505 per month",
        ],
    )
    def test_the_cost_called_the_full_modernization_passes(self, unlabelled: str) -> None:
        text = _text().replace(
            "The projected cost of the full modernization is $505 per month", unlabelled
        )
        assert self._reasons(text) == []

    def test_a_cost_not_called_the_full_modernization_is_rejected(self) -> None:
        """#1474: the monthly cost is the whole plan's; wave 1 costs less."""
        text = _text().replace("The projected cost of the full modernization is", "The cost is")
        (msg,) = self._reasons(text)
        assert "does not call it the full modernization's" in msg

    def test_the_label_in_another_sentence_does_not_count(self) -> None:
        text = _text().replace("The projected cost of the full modernization is", "The cost is")
        text += " That is the full modernization."
        assert any("does not call it" in m for m in self._reasons(text))

    def test_missing_cost_is_rejected(self) -> None:
        reasons = self._reasons(_text().replace("$505 per month", "a modest sum"))
        assert reasons == [
            "[high confidence] executive structure: the summary leaves out the projected "
            "monthly cost $505"
        ]

    @pytest.mark.parametrize(
        "stated",
        [
            "$505 per month",
            "about $505 per month",
            "about $505 a month",
            "$505/month",
            "$505",
            "$ 505 per month",
            "US$505 per month",
            "USD 505 per month",
            "$504.94 per month",
            "$505.00 per month",
            "$504.5 per month",
        ],
    )
    def test_any_amount_that_rounds_to_the_facts_cost_passes(self, stated: str) -> None:
        assert self._reasons(_text().replace("$505 per month", stated)) == []

    @pytest.mark.parametrize("stated", ["$510 per month", "$504.49 per month", "$5,050 a month"])
    def test_a_different_amount_fails(self, stated: str) -> None:
        reasons = self._reasons(_text().replace("$505 per month", stated))
        assert any("leaves out the projected monthly cost $505" in r for r in reasons)
        assert any("states a cost the facts do not" in r for r in reasons)

    def test_another_cost_the_facts_state_may_be_quoted(self) -> None:
        text = _text() + " DynamoDB is $69.19 of it, about $69 a month."
        assert self._reasons(text, _with_engine_cost()) == []
        assert any(
            "states a cost the facts do not: $70.00" in r
            for r in self._reasons(_text() + " DynamoDB is $70.00 of it.", _with_engine_cost())
        )

    def test_thousands_separators(self) -> None:
        facts = _facts(projected_monthly_cost_whole_usd=1235)
        facts["projected_monthly_cost_usd"] = 1234.5
        text = _text(facts)
        assert "The projected cost of the full modernization is $1,235 per month" in text
        for stated in ("$1,235", "$1235", "$1,234.50", "$1234.5"):
            assert self._reasons(text.replace("$1,235", stated), facts) == [], stated

    def test_missing_risk_level_is_rejected(self) -> None:
        (msg,) = self._reasons(_text().replace("overall risk is medium", "overall risk is fine"))
        assert "leaves out the overall risk level (medium)" in msg

    def test_the_level_must_sit_with_risk(self) -> None:
        text = _text().replace("overall risk is medium", "the plan is ready") + " A medium plan."
        assert any("overall risk level" in m for m in self._reasons(text))
        # Within two words of "risk": another level's sentence does not count.
        text = _text().replace("overall risk is medium", "overall risk is low, with medium effort")
        assert any("overall risk level" in m for m in self._reasons(text))

    @pytest.mark.parametrize(
        "risk",
        ["Overall risk: medium", "The overall risk level is medium.", "Medium overall risk."],
    )
    def test_the_level_in_other_words_passes(self, risk: str) -> None:
        text = _text().replace("Risk: overall risk is medium.", risk)
        assert self._reasons(text) == []

    @pytest.mark.passes_on_main  # a line without closing punctuation is still a sentence
    def test_a_line_without_closing_punctuation_is_a_sentence(self) -> None:
        text = (
            "Recommendation: three AWS services, the full modernization, at $505 per month\n\n"
            "Risk: overall risk is medium\n\n"
            "Decision: approve wave 1."
        )
        assert self._reasons(text) == []
        heading = "Full modernization cost: $505 per month\nOverall risk: medium\nDecision needed: approve wave 1"
        assert self._reasons(heading) == []

    def test_missing_decision_is_rejected(self) -> None:
        text = _text().split("\n\nDecision:")[0]
        (msg,) = self._reasons(text)
        assert "leaves out the decision (approve wave 1)" in msg

    @pytest.mark.parametrize(
        "decision",
        [
            "approve the first wave.",
            "approve wave one.",
            "Approve Wave 1.",
            "we ask for your approval of wave 1.",
        ],
    )
    def test_a_decision_in_other_words_passes(self, decision: str) -> None:
        text = _text().replace("approve wave 1.", decision)
        assert self._reasons(text) == []

    @pytest.mark.parametrize(
        "decision", ["approve wave 2.", "do not approve wave 1 yet.", "approve the second wave."]
    )
    def test_the_wrong_or_a_negated_decision_fails(self, decision: str) -> None:
        text = _text().replace("approve wave 1.", decision)
        assert any("leaves out the decision" in r for r in self._reasons(text))

    def test_over_budget_is_rejected(self) -> None:
        padding = " The team has done this before." * 10
        (msg,) = self._reasons(_text() + padding)
        assert f"over the word budget of {SUMMARY_WORD_BUDGET}" in msg

    @pytest.mark.parametrize(
        "claim",
        [
            "The plan saves 40% on the database bill.",
            "This is cheaper than the current setup.",
            "It is cheaper than today.",
            "Savings start in wave 3.",
            "The plan saves $120 a month.",
            "It will save money.",
            "It reduces your database bill by a third.",
            "Expect a lower monthly bill.",
            "Expect a lower monthly cost.",
        ],
    )
    def test_a_saving_with_no_source_cost_is_rejected(self, claim: str) -> None:
        reasons = self._reasons(_text() + " " + claim)
        assert any("claims a saving, but no source cost was collected" in r for r in reasons)

    @pytest.mark.parametrize(
        "sentence",
        [
            "The plan claims no saving.",
            "ElastiCache saves round trips on 20 hot reads.",
            "DynamoDB lowers read latency.",
        ],
    )
    def test_no_money_claim_passes(self, sentence: str) -> None:
        assert self._reasons(_text() + " " + sentence) == []

    def test_a_counted_saving_may_be_stated(self) -> None:
        facts = _facts(saving_counted=True)
        assert self._reasons(_text() + " The plan saves 40%.", facts) == []

    def test_every_failure_is_reported(self) -> None:
        reasons = self._reasons("DynamoDB is the clear fit for this workload.")
        assert len(reasons) == 3

    def test_no_brief_means_nothing_to_check(self) -> None:
        facts = copy.deepcopy(FACTS)
        del facts["executive_brief"]
        assert check_summary_brief("anything", facts) == []
        assert check_summary_brief("anything", None) == []


def test_the_skill_states_the_word_budget_and_the_three_parts() -> None:
    skill = skill_path("executive-summary.md").read_text(encoding="utf-8")
    assert f"at most {SUMMARY_WORD_BUDGET} words" in skill
    for label in ("`Recommendation:`", "`Risk:`", "`Decision:`"):
        assert label in skill
    assert "Do not mention cost" not in skill


@pytest.mark.parametrize(
    ("cost", "shown"),
    [
        (504.94, "$505"),
        (504.5, "$505"),
        (504.49, "$504"),
        (0.5, "$1"),
        (1234.5, "$1,235"),
        (12345.678, "$12,346"),
    ],
)
def test_the_summary_figure_is_rounded_half_up_to_whole_dollars(cost: float, shown: str) -> None:
    assert whole_usd(whole_dollars(cost)) == shown


def test_a_retired_engine_saving_is_stated_in_whole_dollars() -> None:
    facts = _facts(saving_counted=True, retired_saving_whole_usd={"documentdb": 307})
    facts["retired_in_use_engines"] = [
        {"engine": "documentdb", "monthly_cost_usd": 307.03, "moved_to": ["aurora_mysql"]},
        {"engine": "opensearch", "monthly_cost_usd": None, "no_saving_reason": "no queries"},
    ]
    text = _text(facts)
    assert (
        "Retired by this plan: DocumentDB (runs today), $307/mo; OpenSearch (runs today), "
        "no saving counted (no queries)." in text
    )
    assert "$307.03" not in text
    assert check_summary_brief(text, facts) == []


def test_queries_with_no_engine_in_the_scope_are_stated_and_shares_are_of_all() -> None:
    """#1329: a scope that leaves 42 of 107 queries with no engine says so, and DynamoDB's
    share is of all 107 queries (60.7%), not 100% of the 65 it serves."""
    facts = _facts(
        waves=[
            {"wave": 1, "engines": [], "kind": "heterogeneous"},
            {"wave": 2, "engines": ["dynamodb"], "kind": "migration"},
        ],
        share_of_all_queries_percent={"dynamodb": 60.7},
    )
    facts["engines"] = [
        {"engine": "dynamodb", "role": "Migration target", "workload_percent": 100.0}
    ]
    facts["engine_count"], facts["migration_wave_count"] = 1, 2
    facts["no_engine_in_scope_query_count"], facts["query_total_count"] = 42, 107
    text = _text(facts)
    assert "wave 2 moves 60.7% of the queries to DynamoDB" in text
    assert "100.0%" not in text
    assert (
        "42 of the 107 queries have no engine: no engine in the customer's engine scope can "
        "serve them" in text
    )
