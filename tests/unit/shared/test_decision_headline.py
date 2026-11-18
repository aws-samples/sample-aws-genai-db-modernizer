# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The executive summary leads with the decision (#2315).

``facts.executive_brief.decision_headline`` is the one line the results page's summary
header, and the chat summary, open with. It is worded from the facts' own brief (the
decision wave, what each wave does), never from a model's text: the wave to approve and
what it does, or the plan itself when no wave is suggested yet. It gives no cutover
advice and never asks the reader to wait. A report saved before the field gets it from
the same function.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.report.facts import report_facts
from src.shared.decision_headline import decision_headline, wave_adds_engines
from src.shared.executive_brief import decision_paragraph
from src.shared.report_facts import BRIEF_WAVE_KINDS

FIXTURES = Path(__file__).resolve().parents[1] / "agents" / "referee" / "fixtures"

BRIEF: dict[str, Any] = {
    "decision_wave": 1,
    "waves": [
        {"wave": 1, "engines": ["aurora_mysql"], "kind": "homogeneous", "readiness": "suggested"},
        {"wave": 2, "engines": ["elasticache"], "kind": "cache", "readiness": "suggested"},
        {"wave": 3, "engines": ["dynamodb"], "kind": "migration", "readiness": "suggested"},
    ],
}


def _brief(decision: int | None, **kinds: str) -> dict:
    brief = copy.deepcopy(BRIEF)
    brief["decision_wave"] = decision
    for w in brief["waves"]:
        if f"w{w['wave']}" in kinds:
            w["kind"] = kinds[f"w{w['wave']}"]
    return brief


def test_a_homogeneous_wave_names_the_database_and_its_engine() -> None:
    assert decision_headline(BRIEF, "wordpress") == "Approve wave 1: move wordpress to Aurora MySQL"


def test_a_cross_engine_wave_names_its_target() -> None:
    brief = _brief(1, w1="cross_engine")
    brief["waves"][0]["engines"] = ["aurora_postgresql"]
    assert (
        decision_headline(brief, "trailgear")
        == "Approve wave 1: move trailgear to Aurora PostgreSQL"
    )


def test_a_cache_wave_adds_its_engine_and_a_later_wave_moves_queries() -> None:
    assert decision_headline(_brief(2), "wordpress") == "Approve wave 2: add ElastiCache"
    assert decision_headline(_brief(3), "wordpress") == "Approve wave 3: move queries to DynamoDB"


def test_a_wave_that_moves_no_data_is_named_alone() -> None:
    assert decision_headline(_brief(1, w1="no_move"), "wordpress") == "Approve wave 1"


def test_no_suggested_wave_states_the_plan_not_a_wait() -> None:
    brief = _brief(None)
    brief["waves"][0]["engines"] = ["aurora_postgresql"]
    for w in brief["waves"]:
        w["readiness"] = "not_designed"
    text = decision_headline(brief, "trailgear")
    # The cache layer moves no data: it is added, never moved to (#2458 review).
    assert (
        text == "Move trailgear to Aurora PostgreSQL and DynamoDB, and add ElastiCache, in 3 waves"
    )
    one = {"decision_wave": None, "waves": [brief["waves"][0]]}
    assert decision_headline(one, "trailgear") == "Move trailgear to Aurora PostgreSQL in 1 wave"


def test_an_engine_in_two_waves_is_named_once() -> None:
    brief = _brief(None)
    brief["waves"][2]["engines"] = ["aurora_mysql", "dynamodb"]
    assert (
        decision_headline(brief, "wordpress")
        == "Move wordpress to Aurora MySQL and DynamoDB, and add ElastiCache, in 3 waves"
    )


def test_a_read_model_wave_adds_its_engine() -> None:
    brief = _brief(3, w3="read_model")
    brief["waves"][2]["engines"] = ["opensearch"]
    assert decision_headline(brief, "wordpress") == "Approve wave 3: add OpenSearch"
    brief["decision_wave"] = None
    assert (
        decision_headline(brief, "wordpress")
        == "Move wordpress to Aurora MySQL, and add ElastiCache and OpenSearch, in 3 waves"
    )


def test_a_read_model_decision_paragraph_names_the_sync_and_never_says_no_data_moves() -> None:
    brief = {
        "decision_wave": 1,
        "waves": [
            {"wave": 1, "engines": ["opensearch"], "kind": "read_model", "readiness": "suggested"}
        ],
    }
    paragraph = decision_paragraph({"executive_brief": brief}) or ""
    assert "synced from the tables' owners" in paragraph
    assert "No table changes owner." in paragraph
    assert "moves no data" not in paragraph
    cache = {
        "decision_wave": 1,
        "waves": [
            {"wave": 1, "engines": ["elasticache"], "kind": "cache", "readiness": "suggested"}
        ],
    }
    assert "It adds ElastiCache and moves no data." in (
        decision_paragraph({"executive_brief": cache}) or ""
    )


def test_a_heterogeneous_decision_wave_is_named_alone() -> None:
    brief = _brief(1, w1="heterogeneous")
    brief["waves"][0]["engines"] = ["aurora_postgresql"]
    assert decision_headline(brief, "erp") == "Approve wave 1"


def test_a_plan_with_a_wave_that_moves_nothing_counts_every_wave() -> None:
    """The count is the plan's (the Waves figure), so a no_move wave counts; it names no
    engine, so the line names only the wave that moves."""
    brief = {
        "decision_wave": None,
        "waves": [
            {"wave": 1, "engines": [], "kind": "no_move", "readiness": "not_designed"},
            {"wave": 2, "engines": ["dynamodb"], "kind": "migration", "readiness": "not_designed"},
        ],
    }
    assert decision_headline(brief, "shop") == "Move shop to DynamoDB in 2 waves"


def test_a_plan_that_only_adds_engines_says_add() -> None:
    brief = {
        "decision_wave": None,
        "waves": [{"wave": 1, "engines": ["elasticache"], "kind": "cache"}],
    }
    assert decision_headline(brief, "shop") == "Add ElastiCache to shop in 1 wave"


def test_waves_that_name_no_engine_give_no_headline() -> None:
    brief = {
        "decision_wave": None,
        "waves": [
            {"wave": 1, "engines": [], "kind": "no_move"},
            {"wave": 2, "engines": [], "kind": "heterogeneous"},
        ],
    }
    assert decision_headline(brief, "shop") is None


def test_an_old_brief_with_no_decision_wave_key_states_the_plan() -> None:
    brief = {k: v for k, v in _brief(None).items() if k != "decision_wave"}
    assert "decision_wave" not in brief
    assert (
        decision_headline(brief, "wordpress")
        == "Move wordpress to Aurora MySQL and DynamoDB, and add ElastiCache, in 3 waves"
    )


@pytest.mark.parametrize("kind", BRIEF_WAVE_KINDS)
def test_the_headline_and_the_decision_paragraph_agree_on_add(kind: str) -> None:
    """One rule says which waves add an engine and move no data: the headline says "add"
    exactly when the Decision paragraph says "It adds"."""
    brief = {
        "decision_wave": 1,
        "waves": [{"wave": 1, "engines": ["opensearch"], "kind": kind, "readiness": "suggested"}],
    }
    headline = decision_headline(brief, "shop") or ""
    paragraph = decision_paragraph({"executive_brief": brief}) or ""
    assert headline.startswith("Approve wave 1: add ") == ("It adds OpenSearch" in paragraph), (
        headline,
        paragraph,
    )
    assert wave_adds_engines(kind) == headline.startswith("Approve wave 1: add ")


def test_no_database_name_says_the_database() -> None:
    assert decision_headline(BRIEF, "") == "Approve wave 1: move the database to Aurora MySQL"
    assert decision_headline(BRIEF, None) == "Approve wave 1: move the database to Aurora MySQL"


@pytest.mark.parametrize("brief", [None, {}, {"decision_wave": None, "waves": []}, "x"])
def test_no_waves_means_no_headline(brief) -> None:
    assert decision_headline(brief, "wordpress") is None


@pytest.mark.parametrize("decision", [1, 2, 3, None])
def test_the_headline_never_gives_cutover_or_waiting_advice(decision) -> None:
    text = decision_headline(_brief(decision), "wordpress") or ""
    for word in ("cutover", "wait", "not ready", "collect", "safe", "ready"):
        assert word not in text.lower(), (word, text)


def _old_report() -> dict[str, Any]:
    report: dict[str, Any] = json.loads((FIXTURES / "report_1_85_customer_growth.json").read_text())
    return report


def test_a_report_saved_before_the_headline_gets_it_from_the_same_function() -> None:
    report = _old_report()
    assert "decision_headline" not in report["facts"]["executive_brief"]
    brief = report_facts(report)["executive_brief"]
    assert brief["decision_headline"] == "Approve wave 1: move shop to Aurora MySQL"
    assert brief["decision_headline"] == decision_headline(brief, report["database_name"])
    # Everything else in the brief stays as stored (its exposures grouped, #2324).
    stored = report["facts"]["executive_brief"]
    keep = {"decision_headline", "exposures"}
    assert {k: v for k, v in brief.items() if k not in keep} == {
        k: v for k, v in stored.items() if k not in keep
    }


def test_a_headline_read_back_as_none_is_filled_in_too() -> None:
    """The value decides, not the key: a block re-dumped through the contract reads the
    field back as None."""
    report = _old_report()
    report["facts"]["executive_brief"]["decision_headline"] = None
    brief = report_facts(report)["executive_brief"]
    assert brief["decision_headline"] == "Approve wave 1: move shop to Aurora MySQL"


def test_a_stored_headline_is_kept_as_stored() -> None:
    report = _old_report()
    report["facts"]["executive_brief"]["decision_headline"] = "Approve wave 1: as stored"
    assert report_facts(report)["executive_brief"]["decision_headline"] == (
        "Approve wave 1: as stored"
    )


def test_synthesis_writes_the_headline_into_the_brief() -> None:
    from src.shared.report_facts import executive_brief_facts

    report = _old_report()
    waves = [w for w in report.get("migration_waves") or [] if isinstance(w, dict)]
    brief = executive_brief_facts(report, waves, [])
    assert brief["decision_headline"] == decision_headline(brief, report["database_name"])
    assert brief["decision_headline"].startswith("Approve wave 1: move shop to ")


def test_the_contract_carries_the_headline_and_an_old_brief_still_loads() -> None:
    from src.contracts.synthesis_output import ExecutiveBriefFacts

    stored = _old_report()["facts"]["executive_brief"]
    assert ExecutiveBriefFacts.model_validate(stored).decision_headline is None
    loaded = ExecutiveBriefFacts.model_validate({**stored, "decision_headline": "Approve wave 1"})
    assert loaded.decision_headline == "Approve wave 1"
