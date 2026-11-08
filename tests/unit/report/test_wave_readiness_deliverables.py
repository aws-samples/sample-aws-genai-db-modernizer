# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every deliverable states whether the plan suggests each wave (#1992, ADR-032).

Synthesis computes each wave's readiness once (``facts.waves[].readiness_line``); the
engineering report, the executive PDF and the final chat summary print it as it is, with
the rule stated once. A report written before the check still loads and prints none.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from src.contracts.synthesis_output import ReportFacts
from src.report.facts import report_facts
from src.report.printed_summary import render_final_summary
from src.report.renderers import render_engineering_report_md
from src.shared.report_facts import compute_report_facts, refresh_risk_facts
from src.shared.wave_readiness import READINESS_RULE_NOTE
from tests.e2e.deliverable_figures import md_plain
from tests.executive_pdf import executive_text

HELD = (
    "Not suggested yet: an open high risk on DynamoDB (RISK-001, performance degradation). "
    "Next step: revise the DynamoDB design and review it again until no check holds it."
)


def _report(severity: str = "HIGH") -> dict[str, Any]:
    report: dict[str, Any] = {
        "database_name": "shop",
        "recommended_architecture": {"architecture_type": "MULTI_DATABASE", "databases": []},
        "ranking": [
            {"target": "aurora_mysql", "workload_percent": 60.0, "assigned_queries": 6},
            {"target": "dynamodb", "workload_percent": 40.0, "assigned_queries": 4},
        ],
        "risk_assessment": {
            "overall_risk_level": severity,
            "risks": [
                {
                    "risk_id": "RISK-001",
                    "risk_type": "PERFORMANCE_DEGRADATION",
                    "severity": severity,
                    "description": "[dynamodb] Reads at 900 calls/s on shop.b.",
                    "mitigation": "Spread the reads.",
                }
            ],
            "resolved_risks": [],
        },
        "tco_analysis": {
            "current_monthly_cost": 0.0,
            "current_cost_known": False,
            "projected_monthly_cost": 300.0,
            "savings_percent": 0,
            "cost_breakdown": [{"database": "dynamodb", "monthly_cost_usd": 300.0}],
        },
        "trade_offs": [],
        # Both engines designed and reviewed: readiness turns on the checks alone.
        "schema_designs": {
            "aurora_mysql": {"status": "completed", "open_review_count": 0},
            "dynamodb": {"status": "completed", "open_review_count": 0},
        },
        "migration_waves": [
            {
                "wave": 1,
                "title": "Move to Aurora MySQL",
                "engines": ["aurora_mysql"],
                "homogeneity": "homogeneous",
                "moves_from": ["mysql"],
                "tables": ["shop.a", "shop.b"],
                "table_count": 2,
                "query_count": 6,
                "workload_share_percent": 60.0,
                "rationale": "Same engine family.",
                "gate": "Schema and data parity.",
                "cutover_query_count": 10,
            },
            {
                "wave": 2,
                "title": "Key-value lookups to DynamoDB",
                "engines": ["dynamodb"],
                "moves_from": ["aurora_mysql"],
                "tables": ["shop.b"],
                "table_count": 1,
                "query_count": 4,
                "workload_share_percent": 40.0,
                "rationale": "Key lookups.",
                "gate": "Load test.",
            },
        ],
        "table_mappings": [],
        "query_groups": [],
        "cache_overlay": None,
    }
    report["facts"] = compute_report_facts(report)
    return report


def test_the_facts_hold_the_wave_and_validate() -> None:
    facts = ReportFacts.model_validate(_report()["facts"])
    one, two = facts.waves
    assert one.suggested is True and one.holds == []
    assert two.suggested is False and two.readiness_line == HELD
    assert [h.check for h in two.holds] == ["open_high_risk"]
    assert [s.step for s in two.next_steps] == ["revise_design"]
    assert one.readiness == "suggested" and two.readiness == "held"
    assert facts.executive_brief is not None
    assert facts.executive_brief.held_waves == [2]
    assert facts.executive_brief.decision_wave == 1


def test_a_medium_risk_holds_no_wave() -> None:
    facts = _report("MEDIUM")["facts"]
    assert [w["suggested"] for w in facts["waves"]] == [True, True]
    assert facts["executive_brief"]["held_waves"] == []


def test_the_engineering_report_states_each_waves_readiness_and_the_rule_once() -> None:
    md = md_plain(render_engineering_report_md(_report()))
    roadmap = md[md.index("## Migration roadmap") :]
    assert roadmap.count("- Readiness: Suggested: no check holds it.") == 1
    assert roadmap.count(f"- Readiness: {HELD}") == 1
    assert md.count(f"{READINESS_RULE_NOTE}.") == 1
    assert roadmap.index("### Wave 2") < roadmap.index(HELD)


def test_the_executive_report_states_each_waves_readiness() -> None:
    pdf = " ".join(executive_text(_report()).split())
    assert pdf.count(f"Readiness: {HELD}") == 1
    assert pdf.count("Readiness: Suggested: no check holds it.") == 1
    assert pdf.count(f"{READINESS_RULE_NOTE}.") == 1
    # The executive report names no table.
    assert "shop.b" not in pdf


def test_the_final_summary_states_the_held_wave() -> None:
    text = md_plain(
        render_final_summary(
            _report(), database_name="shop", job_id="j", deliverables_dir="d/", deliverables=[]
        )
    )
    glance = text[text.index("### At a glance") : text.index("### Engines")]
    assert f"- Wave 2: {HELD}" in glance
    assert "Wave 1:" not in glance
    # The executive summary's Decision says it too.
    assert "Wave 2 is not suggested yet: an open high risk on DynamoDB (RISK-001)" in text


def test_refreshed_risks_refresh_the_readiness() -> None:
    """A risk backfilled at render time (#1148) re-runs the check over the same risks."""
    report = _report("MEDIUM")
    report["risk_assessment"]["risks"][0]["severity"] = "HIGH"
    facts = refresh_risk_facts(report["facts"], report)
    assert [w["suggested"] for w in facts["waves"]] == [True, False]
    assert facts["executive_brief"]["held_waves"] == [2]


def _old(report: dict[str, Any]) -> dict[str, Any]:
    """The same report as synthesis wrote it before the check."""
    old = copy.deepcopy(report)
    for w in old["facts"]["waves"]:
        for key in ("readiness", "suggested", "holds", "next_steps", "readiness_line"):
            w.pop(key, None)
    brief = old["facts"]["executive_brief"]
    brief.pop("held_waves", None)
    old["facts"].pop("wave_readiness_notes", None)
    for w in brief["waves"]:
        w.pop("suggested", None)
        w.pop("readiness", None)
    for d in old["schema_designs"].values():
        d.pop("open_review_count", None)
    return old


def test_a_report_written_before_the_check_loads_and_states_none() -> None:
    old = _old(_report())
    facts = ReportFacts.model_validate(old["facts"])
    assert [w.suggested for w in facts.waves] == [None, None]
    assert facts.executive_brief is not None and facts.executive_brief.held_waves == []
    md = render_engineering_report_md(old)
    assert "Readiness" not in md and READINESS_RULE_NOTE not in md
    assert "Readiness" not in executive_text(old)
    text = render_final_summary(
        old, database_name="shop", job_id="j", deliverables_dir="d/", deliverables=[]
    )
    assert "not suggested yet" not in text.lower()
    # Its refreshed risks keep it as it was: no check runs on a block written before it.
    refreshed = refresh_risk_facts(old["facts"], old)
    assert all("readiness" not in w for w in refreshed["waves"])
    assert refreshed["executive_brief"]["held_waves"] == []


def test_a_report_with_no_facts_block_states_no_readiness() -> None:
    """A report written before the facts block existed is rebuilt from its fields
    (``src.report.facts.report_facts``); its designs carry no review counts, so it
    states no readiness either (the review's probe)."""
    old = _old(_report())
    old.pop("facts")
    facts = report_facts(old)
    assert all(w.get("readiness") is None for w in facts["waves"])
    assert facts["executive_brief"]["held_waves"] == []
    assert facts["executive_brief"]["decision_wave"] == 1
    md = render_engineering_report_md(old)
    assert "Readiness" not in md and "not suggested yet" not in md.lower()
    assert "Readiness" not in executive_text(old)


def test_a_wave_with_no_design_is_not_designed_and_never_the_decision() -> None:
    report = _report("MEDIUM")
    report["schema_designs"].pop("aurora_mysql")
    report["facts"] = compute_report_facts(report)
    one, two = report["facts"]["waves"]
    assert one["readiness"] == "not_designed"
    assert one["readiness_line"] == (
        "Not designed yet: run the Aurora MySQL schema design, then check the wave again."
    )
    # Wave 2 builds on wave 1, so it is held until wave 1 is suggested.
    assert two["readiness"] == "held"
    assert report["facts"]["executive_brief"]["decision_wave"] is None
    md = md_plain(render_engineering_report_md(report))
    assert "Suggested: no check holds it." not in md
    assert "Wave 1 is not designed yet: run its schema design." in md


def test_another_engines_flag_holds_only_when_this_engine_flags_the_query() -> None:
    """The wordpress case: an Aurora MySQL note on a query now on DynamoDB holds no wave;
    with DynamoDB's own analysis flagging the same query, it holds and names the engine
    that raised it."""
    report = _report()
    moved = report["risk_assessment"]["risks"][0]
    moved.update(reattributed_from="aurora_mysql", query_ids=["q1"])
    report["facts"] = compute_report_facts(report)
    assert report["facts"]["waves"][1]["readiness"] == "suggested"
    report["risk_assessment"]["risks"].append(
        {
            "risk_id": "RISK-002",
            "risk_type": "MIGRATION_COMPLEXITY",
            "severity": "MEDIUM",
            "description": "[dynamodb] A write wider than one transaction.",
            "mitigation": "Split it.",
            "query_ids": ["q1"],
        }
    )
    report["facts"] = compute_report_facts(report)
    two = report["facts"]["waves"][1]
    assert two["readiness"] == "held"
    assert two["holds"][0]["flagged_by"] == "aurora_mysql"
    assert "flagged by the Aurora MySQL analysis" in two["readiness_line"]


def test_the_results_page_states_the_same_rule() -> None:
    en = json.loads(Path("src/ui/src/locales/en.json").read_text(encoding="utf-8"))
    assert en["analysis-results.waves.readiness-rule"] == f"{READINESS_RULE_NOTE}."


def test_the_designs_unserved_patterns_and_open_reviews_hold_the_wave() -> None:
    """Read from report.json alone: the merge's unresolved conflicts and the review count
    synthesis wrote into each design summary."""
    report = _report("MEDIUM")
    report["schema_designs"] = {
        "aurora_mysql": {"status": "completed", "open_review_count": 0},
        "dynamodb": {
            "status": "completed",
            "open_review_count": 1,
            "unresolved_merge_conflicts": [
                {"failure": "two homes", "status": "unserved", "pattern_ids": ["DDB-AP-1"]}
            ],
        },
    }
    report["facts"] = compute_report_facts(report)
    two = report["facts"]["waves"][1]
    assert two["readiness"] == "held"
    assert [h["check"] for h in two["holds"]] == ["unserved_patterns", "open_review"]
    assert [s["step"] for s in two["next_steps"]] == ["revise_design"]
    assert report["facts"]["waves"][0]["suggested"] is True
