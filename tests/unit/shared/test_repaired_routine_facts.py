# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``facts.routines`` for a model-repaired routine (#2384): carried, repaired
(``repaired_count``), through #2420's sentences; its intervention line says what step 1 does
with it, never that step 1 does not deploy repaired routines yet."""

from __future__ import annotations

from src.agents.collector.routine_sources import routine_sources_digest
from src.contracts.synthesis_output import RoutineFacts
from src.shared.migration_risk_rules import routine_risks
from src.shared.routine_facts import read_routine_facts, with_routine_words
from tests.unit.tools.routines import repaired_fixtures as rf

NOT_YET = "does not deploy a repaired routine yet"


def _facts(store):
    facts = read_routine_facts(store, rf.DB, rf.JOB, 1)
    assert facts is not None
    return facts


def _item(facts):
    (item,) = [i for i in facts["interventions"] if i["routine_id"] == rf.WAL]
    return item


def test_a_deployed_repair_is_carried_repaired_and_its_line_says_step_1_deploys_it(tmp_path):
    store = rf.job(tmp_path)
    rf.mark_tested(store)
    facts = _facts(store)
    assert (facts["repaired_count"], facts["carried_count"], facts["needs_repair_count"]) == (
        1, 2, 2)  # fmt: skip
    assert "1 repaired" in facts["line"] and "1 repaired" in facts["executive_line"]
    assert "Tested on a throwaway Aurora: step 1 deploys all 2." in facts["executive_line"]
    assert "On a test Aurora, of 2 carried routines: 2 showed no difference" in facts["line"]
    item = _item(facts)
    assert item["deploy_gate"] == "deployed"
    assert NOT_YET not in item["line"] and "step 1 deploys it" in item["line"]
    (row,) = [r for r in facts["routines"] if r["routine_id"] == rf.WAL]
    assert (row["status"], row["category"], row["status_text"]) == (
        "model_repaired", "repaired", "Carried, repaired")  # fmt: skip
    assert row["risk_classes"] == ["carried_review"] and "model_repaired" in row["severity_basis"]
    # #2429: the test found no difference on it, so it no longer holds the carried risk.
    assert [r for r in routine_risks(facts) if r["rule"] == "routines.carried"] == []
    assert row["held_back"] is None
    (carried,) = [r for r in routine_risks(_facts(rf.job(tmp_path / "before")))
                  if r["rule"] == "routines.carried"]  # fmt: skip
    assert "1 was repaired by a model" in carried["description"]


def test_before_a_test_the_repair_is_carried_checked_not_tested_yet(tmp_path):
    store = rf.job(tmp_path)
    facts = _facts(store)
    assert facts["repaired_count"] == 1 and facts["proof_counts"] is None
    assert "The 2 carried routines are not tested yet" in facts["line"]
    assert "Before they are deployed, test them" in facts["executive_line"]
    item = _item(facts)
    assert item["deploy_gate"] == "not_tested" and NOT_YET not in item["line"]
    assert "it is checked, not tested yet on a test Aurora" in item["line"]
    assert "Test it with /test-routines before it is deployed." in item["line"]


def test_a_held_repair_says_why_and_counts_as_held(tmp_path):
    store = rf.job(tmp_path)
    rf.mark_tested(store, tested=rf.OTHER)  # the test ran another repair
    facts = _facts(store)
    assert "step 1 deploys 1; 1 is held back" in facts["executive_line"]
    assert "1 because the repair it has now is not the one tested" in facts["line"]
    item = _item(facts)
    assert item["deploy_gate"] == "rebuild_differs"
    assert "the repair it has now is not the one the test" in item["line"]


def test_a_repair_made_after_the_test_counts_as_not_tested(tmp_path):
    store = rf.job(tmp_path)
    rf.mark_tested(store, tested=None)
    facts = _facts(store)
    assert "of 2 carried routines: 1 showed no difference on the calls made; 1 was not tested" in (
        facts["line"])  # fmt: skip
    assert _item(facts)["deploy_gate"] == "not_tested"


def test_the_facts_round_trip_the_contract_and_an_old_block_gets_its_words(tmp_path):
    store = rf.job(tmp_path)
    rf.mark_tested(store)
    facts = _facts(store)
    again = RoutineFacts.model_validate(facts).model_dump(mode="json")
    assert _item(again)["deploy_gate"] == "deployed"
    # A block stored before #2384: no deploy_gate, so the line says it is not tested yet.
    old = {**facts, "interventions": [{k: v for k, v in _item(facts).items()
                                       if k not in ("deploy_gate", "line")}]}  # fmt: skip
    words = with_routine_words(old)
    assert NOT_YET not in words["interventions"][0]["line"]
    assert "checked, not tested yet on a test Aurora" in words["interventions"][0]["line"]
    assert routine_sources_digest(store, rf.DB, rf.JOB)  # the routines file the facts read


def test_the_executive_line_follows_the_deploy_gate_for_model_repairs(tmp_path):
    """#2385's executive clause reads the deploy gate (#2384): before a test the repairs
    are checked, not tested yet; after one, what step 1 deploys of them."""
    from src.shared.routine_facts import repaired_executive_clause

    store = rf.job(tmp_path)
    assert (
        "1 of the carried routines was repaired by a model and compared on a local engine "
        "of the source: it is "
        "checked, not tested yet on a test Aurora." in _facts(store)["executive_line"]
    )
    rf.mark_tested(store)
    assert _facts(store)["executive_line"].endswith(
        "1 of the carried routines was repaired by a model, and step 1 deploys it."
    )
    rf.mark_tested(store, tested=rf.OTHER)
    assert (
        "1 of the carried routines was repaired by a model; step 1 holds it back"
        in _facts(store)["executive_line"]
    )
    assert repaired_executive_clause(3, 1) == (
        "3 of the carried routines were repaired by a model; step 1 deploys 1 and holds back 2."
    )
    for text in (_facts(store)["executive_line"], repaired_executive_clause(2)):
        assert "does not deploy" not in text


def test_the_repair_clause_counts_with_the_routines_that_can_move(tmp_path):
    """Round 2 review of #2427: right after "N need work", the clause names the carried
    routines, never "of them", which read as one of the routines that need work."""
    store = rf.job(tmp_path)
    rf.mark_tested(store)
    line = _facts(store)["executive_line"]
    assert "need work" in line and "of them" not in line
    assert line.index("need work") < line.index("1 of the carried routines was repaired")
