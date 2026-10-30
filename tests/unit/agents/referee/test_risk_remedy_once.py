"""#1510: each risk's remedy and each wave gate are stated once, by their owner.

The Risk register owns a risk's mitigation: a mitigation strategy names the risk by
id and adds only what the register does not say. The roadmap owns a wave's gate:
the risks to close before it are listed in that wave's gate, and a risk's mitigation
that needs the gate points at it ("See the Wave 3 gate in the roadmap."). The guard
helpers in ``tests/report_concision.py`` check this on every sample (e2e); here they
are checked themselves, and the synthesis side that satisfies them.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from src.agents.referee.migration_risks import GATE_IDS_NAMED, add_migration_risks
from src.agents.referee.synthesis_report import _build_mitigation_strategies
from src.contracts.synthesis_output import join_gate_parts
from src.shared.migration_risk_rules import backfill_report_risks
from src.shared.report_facts import compute_report_facts
from tests.report_concision import (
    gate_restatements,
    gate_risk_ids,
    remedy_copies,
    remedy_restatements,
)
from tests.unit.agents.referee.test_migration_risks import _all_kinds

EFFECTIVE = {"dynamodb", "aurora_postgresql"}


def _risk(rid: str, rtype: str, sev: str, mitigation: str | None, **extra: Any) -> dict:
    return {
        "risk_id": rid,
        "risk_type": rtype,
        "severity": sev,
        "description": f"[dynamodb] something about {rid}",
        "affected_tables": extra.pop("tables", ["db.t"]),
        "mitigation": mitigation,
        "query_ids": extra.pop("query_ids", ["q1"]),
        **extra,
    }


HIGH = _risk(
    "RISK-001",
    "PERFORMANCE_DEGRADATION",
    "HIGH",
    "Cover the 1 remaining query in the DynamoDB schema design or route it to an engine "
    "that serves it.",
)
LOAD_TESTED = _risk(
    "RISK-002",
    "PERFORMANCE_DEGRADATION",
    "MEDIUM",
    "Load-test the 1 flagged query on Aurora PostgreSQL with production-scale data before "
    "cutover and tune indexes or query plans where the measured latency misses the target.",
)
UNCHECKED = _risk(
    "RISK-003",
    "MIGRATION_COMPLEXITY",
    "LOW",
    "Run the DynamoDB schema design; any of these queries it does not serve then becomes a "
    "schema design gap.",
    coverage_gap=True,
)
GAP = _risk(
    "RISK-004",
    "MIGRATION_COMPLEXITY",
    "MEDIUM",
    "Add in-scope access patterns for these queries to the DynamoDB schema design, or route "
    "them to an engine that serves them.",
    coverage_gap=True,
    query_ids=["g1", "g2"],
)
NOTE = _risk(
    "RISK-005",
    "OPERATIONAL_RISK",
    "MEDIUM",
    "Implement as application logic: write the audit row from the service.",
    object_type="trigger",
    object_name="audit_insert",
)
RISKS = [HIGH, LOAD_TESTED, UNCHECKED, GAP, NOTE]


def _report(risks: list[dict], strategies: list[str], waves: list[dict] | None = None) -> dict:
    return {
        "risk_assessment": {"risks": risks, "mitigation_strategies": strategies},
        "migration_waves": waves or [],
    }


# ---------------------------------------------------------------------------
# The guard
# ---------------------------------------------------------------------------


@pytest.mark.passes_on_main
def test_the_guard_finds_a_paraphrase_and_a_copy_of_a_remedy() -> None:
    paraphrase = "Run the DynamoDB schema design to check the 20 queries assigned to it (RISK-003)."
    copied = f"RISK-001 (HIGH, DynamoDB; db.t): {HIGH['mitigation']}"
    report = _report(RISKS, [paraphrase, copied])
    assert remedy_restatements(report) == [("RISK-001", copied), ("RISK-003", paraphrase)]
    assert remedy_copies(f"- {copied}", report) == ["RISK-001"]


@pytest.mark.passes_on_main
def test_the_guard_passes_a_reference_and_a_line_that_adds_its_own_action() -> None:
    report = _report(
        RISKS,
        [
            "Apply the mitigation of RISK-001 (HIGH, DynamoDB) first.",
            "Load-test the queries behind RISK-001 on DynamoDB with production-scale data "
            "before cutover.",
            "Apply the mitigations of RISK-003, RISK-004 and RISK-005.",
        ],
    )
    assert remedy_restatements(report) == []
    assert (
        remedy_copies("\n".join(report["risk_assessment"]["mitigation_strategies"]), report) == []
    )


@pytest.mark.passes_on_main
def test_a_remedy_is_restated_only_where_its_risk_is_named() -> None:
    """A line about another risk may share common words with a remedy; it restates
    nothing about a risk it does not name."""
    report = _report(RISKS, ["Run the DynamoDB schema design for the cache (RISK-002)."])
    assert remedy_restatements(report) == []


@pytest.mark.passes_on_main
def test_the_guard_finds_a_wave_gate_stated_outside_the_roadmap() -> None:
    mitigation = "Design this query in DynamoDB before the Wave 3 gate, and serve none as a scan."
    report = _report(
        [_risk("RISK-009", "MIGRATION_COMPLEXITY", "MEDIUM", mitigation, rule="x", wave=3)],
        ["Before the Wave 3 gate, close RISK-009 (redesigns routing relies on)."],
    )
    assert len(gate_restatements(report)) == 2
    pointed = _report(
        [
            _risk(
                "RISK-009",
                "MIGRATION_COMPLEXITY",
                "MEDIUM",
                "Design this query in DynamoDB. See the Wave 3 gate in the roadmap.",
            )
        ],
        ["Before cutover, close RISK-010 (queries no engine in scope serves)."],
    )
    assert gate_restatements(pointed) == []


# ---------------------------------------------------------------------------
# Mitigation strategies name the risk, the register states the remedy
# ---------------------------------------------------------------------------


def test_a_high_risk_strategy_names_it_without_its_remedy() -> None:
    strategies = _build_mitigation_strategies(RISKS, EFFECTIVE)
    assert strategies[0] == "Apply the mitigation of RISK-001 (HIGH, DynamoDB) first."
    assert remedy_restatements(_report(RISKS, strategies)) == []


@pytest.mark.passes_on_main
def test_a_high_risk_with_no_mitigation_still_says_so() -> None:
    bare = {**HIGH, "mitigation": None}
    assert _build_mitigation_strategies([bare], EFFECTIVE)[0] == (
        "RISK-001 (HIGH, DynamoDB; db.t): no mitigation recorded; define one before cutover."
    )


def test_risks_whose_remedy_is_the_whole_strategy_are_listed_by_id() -> None:
    strategies = _build_mitigation_strategies(RISKS, EFFECTIVE)
    assert "Apply the mitigations of RISK-003, RISK-004 and RISK-005." in strategies
    assert not any("schema design" in s or "application logic" in s for s in strategies)


def test_the_load_test_line_leaves_out_a_risk_whose_remedy_is_a_load_test() -> None:
    strategies = _build_mitigation_strategies(RISKS, EFFECTIVE)
    [line] = [s for s in strategies if s.startswith("Load-test")]
    assert "RISK-001" in line and "RISK-002" not in line
    only = _build_mitigation_strategies([LOAD_TESTED], EFFECTIVE)
    assert not any(s.startswith("Load-test") for s in only)


# ---------------------------------------------------------------------------
# The roadmap owns each wave gate
# ---------------------------------------------------------------------------


def _waves() -> list[dict[str, Any]]:
    return [
        {
            "wave": 1,
            "gate": "Parity validated.",
            "gate_parts": [{"text": "Parity validated.", "items": []}],
        },
        {"wave": 2, "gate": "Hit rate verified."},
        {
            "wave": 3,
            "gate": "Dual-write validated (db.a, db.b)",
            "gate_parts": [{"text": "Dual-write validated", "items": ["db.a", "db.b"]}],
        },
    ]


def test_each_waves_gate_lists_the_risks_to_close_before_it() -> None:
    risks = _all_kinds()
    waves = _waves()
    assessment: dict[str, Any] = {"risks": [], "mitigation_strategies": ["Existing line."]}
    add_migration_risks(assessment, risks, waves)
    named = gate_risk_ids({"migration_waves": waves})
    for w in waves:
        ids = {r["risk_id"] for r in risks if r["wave"] == w["wave"]}
        if len(ids) <= GATE_IDS_NAMED:
            assert named[w["wave"]] == ids, w["wave"]
        else:
            assert f"Close the {len(ids)} Wave {w['wave']} risks before this gate" in w["gate"]
    for w in waves:
        assert "before this gate" in w["gate"], w["wave"]
        if "gate_parts" in w:
            assert join_gate_parts(w["gate_parts"]) == w["gate"]
    assert waves[0]["gate"].startswith("Parity validated. Close ")
    assert waves[2]["gate_parts"][0] == {"text": "Dual-write validated", "items": ["db.a", "db.b"]}
    # Only the risks with no wave keep a line among the strategies: no gate holds them.
    lines = assessment["mitigation_strategies"]
    assert lines[0] == "Existing line."
    assert [x.split(",")[0] for x in lines[1:]] == ["Before cutover"]
    assert gate_restatements(_report(assessment["risks"], lines, waves)) == []


def test_a_risk_with_no_wave_in_the_plan_keeps_its_gate_line_among_the_strategies() -> None:
    risks = [r for r in _all_kinds() if r["wave"] == 3]
    assessment: dict[str, Any] = {"risks": [], "mitigation_strategies": []}
    add_migration_risks(assessment, risks, [w for w in _waves() if w["wave"] != 3])
    assert [x.split(",")[0] for x in assessment["mitigation_strategies"]] == [
        "Before the Wave 3 gate"
    ]


def test_every_rule_risks_mitigation_points_at_its_waves_gate() -> None:
    """A mitigation due at its wave's gate points at it; one due before the wave starts
    (choose an engine, set up CDC) names the start, which no gate states."""
    risks = _all_kinds()
    assert gate_restatements(_report(risks, [])) == []
    for r in risks:
        if r["wave"]:
            assert (
                r["mitigation"].endswith(f"See the Wave {r['wave']} gate in the roadmap.")
                or f"before Wave {r['wave']} starts" in r["mitigation"]
            ), r
        else:
            assert "before cutover" in r["mitigation"].casefold(), r


def test_a_backfilled_old_report_moves_its_gate_lines_into_the_roadmap() -> None:
    from tests.unit.agents.referee.test_flagged_unservable_risks import _old_report

    old = _old_report()
    old["migration_waves"] = copy.deepcopy(old["migration_waves"])
    for w in old["migration_waves"]:
        w["gate"] = f"Gate {w['wave']}."
    old["facts"] = compute_report_facts(old)
    before = copy.deepcopy(old)
    new = backfill_report_risks(old)
    assert old == before, "the stored report is not changed in place"
    assert new["risk_assessment"]["mitigation_strategies"] == [
        "Analysis line.",
        "After wave 3 (DynamoDB): compare the latency.",
    ]
    gates = {w["wave"]: w["gate"] for w in new["migration_waves"]}
    assert gates[1] == (
        "Gate 1. Close RISK-003 before this gate (flagged queries no other engine takes)."
    )
    assert gates[3] == (
        "Gate 3. Close RISK-002 and RISK-004 before this gate (tables kept in sync and flagged "
        "queries no other engine takes)."
    )
    assert backfill_report_risks(new) is new


@pytest.mark.parametrize(
    "mitigation",
    [
        "Load test the flagged query with production data.",
        "Run load testing on the flagged query before cutover.",
        "LOAD-TEST the flagged query.",
    ],
)
def test_any_wording_of_a_load_test_leaves_the_risk_out_of_the_load_test_line(
    mitigation: str,
) -> None:
    risk = {**LOAD_TESTED, "mitigation": mitigation}
    strategies = _build_mitigation_strategies([HIGH, risk], EFFECTIVE)
    [line] = [s for s in strategies if s.startswith("Load-test")]
    assert "RISK-002" not in line
    assert remedy_restatements(_report([HIGH, risk], strategies)) == []


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "mitigation",
    [
        "Design it by the Wave 3 gate.",
        "Design it ahead of the Wave 3 gate.",
        "Design it before Wave 3's gate.",
        "Design it at this Wave 3 gate.",
    ],
)
def test_the_guard_finds_each_wording_of_a_wave_gate(mitigation: str) -> None:
    report = _report([_risk("RISK-009", "MIGRATION_COMPLEXITY", "MEDIUM", mitigation)], [])
    assert gate_restatements(report) == [mitigation]
