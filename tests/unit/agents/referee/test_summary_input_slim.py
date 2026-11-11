# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""What the executive-summary request drops, and what it must keep (#1064)."""

from __future__ import annotations

import copy

from src.agents.referee.summary_input_slim import RANKING_KEEP, WAVE_DROP, slim_summary_input


def _payload() -> dict:
    return {
        "ranking": [
            {
                "target": "dynamodb",
                "role": "primary",
                "workload_percent": 77.7,
                "assigned_queries": 80,
                "assigned_queries_in_scope": 79,
                "schema_design_available": True,
                "target_tables": 4,
                "access_patterns": 9,
                "pattern_groups": 1,
                "confidence_score": 61,
                "routed_confidence": 73,
                "monthly_cost_usd": 12.5,
                "assignment_reason_summary": ["highest fit"],
                "rationale": "73% mean fit, estimated $12.50/month",
                "a_new_field_nobody_vetted": "x" * 500,
            }
        ],
        "migration_waves": [
            {
                "wave": 1,
                "title": "Move to Aurora",
                "engines": ["aurora_mysql"],
                "tables": ["db.a"],
                "query_count": 70,
                "workload_share_percent": 65.4,
                "rationale": "long prose",
                "gate": "parity",
                "gate_parts": ["a"],
                "share_basis": "queries",
            }
        ],
        "risk_assessment": {
            "overall_risk_level": "LOW",
            "risks": [{"severity": "HIGH", "description": "d", "query_ids": ["a" * 64, "b" * 64]}],
            "resolved_risks": [{"severity": "LOW", "description": "e", "query_ids": []}],
            "mitigation_strategies": ["m"],
        },
        "trade_offs": [
            {
                "description": "same names",
                "impact": "i",
                "source_tables": ["db.a", "db.b"],
                "target_tables": ["a", "b"],
                "query_ids": ["c" * 64],
                "engine": "dynamodb",
            },
            {
                "description": "renamed target",
                "impact": "i",
                "source_tables": ["db.a"],
                "target_tables": ["a_by_date"],
                "query_ids": [],
                "engine": "dynamodb",
            },
            {
                "description": "no targets",
                "impact": "i",
                "source_tables": ["db.a"],
                "target_tables": [],
                "engine": "reality-check",
            },
        ],
        "table_mappings": [
            {
                "source_table": "db.a",
                "recommended_database": "dynamodb",
                "target_table": "a",
                "aggregate_pattern": "separate",
                "confidence_score": 87,
                "alternatives": [
                    {"database": "elasticache", "target_table": "a", "confidence_score": 82}
                ],
            }
        ],
        "deterministic_summary": "kept",
        "tco_analysis": {"projected_monthly_cost": 5},
    }


def test_ranking_keeps_only_the_vetted_fields():
    (r,) = slim_summary_input(_payload())["ranking"]
    assert set(r) == set(RANKING_KEEP)
    assert r["workload_percent"] == 77.7
    assert r["target_tables"] == 4


def test_a_ranking_without_some_vetted_fields_is_not_padded():
    p = _payload()
    p["ranking"] = [{"target": "elasticache", "confidence_score": 5}]
    assert slim_summary_input(p)["ranking"] == [{"target": "elasticache"}]


def test_waves_lose_their_prose_and_keep_order_counts_and_shares():
    (w,) = slim_summary_input(_payload())["migration_waves"]
    assert not set(WAVE_DROP) & set(w)
    assert w["share_basis"] == "queries"
    assert (w["wave"], w["title"], w["query_count"], w["workload_share_percent"]) == (
        1,
        "Move to Aurora",
        70,
        65.4,
    )


def test_query_ids_become_a_count_everywhere():
    out = slim_summary_input(_payload())
    risk = out["risk_assessment"]
    assert risk["risks"][0]["query_count"] == 2 and "query_ids" not in risk["risks"][0]
    assert "resolved_risks" not in risk  # #2132: counts by severity stand in for them
    assert [t.get("query_count") for t in out["trade_offs"]] == [1, 0, None]
    assert not any("query_ids" in t for t in out["trade_offs"])


def test_only_a_carried_write_risk_sends_a_count_of_its_details():
    """#1362: the carried-write risk's details are one approach per statement, so the
    summary gets their count (its description counts them by intent); every other risk
    keeps its details as they are."""
    payload = _payload()
    risks = payload["risk_assessment"]["risks"]
    risks[0]["details"] = ["a design trade-off"]
    risks.append(
        {
            "severity": "MEDIUM",
            "rule": "design.carried_writes",
            "description": "d",
            "details": ["one long approach", "another"],
        }
    )
    out = slim_summary_input(payload)["risk_assessment"]["risks"]
    assert out[0]["details"] == ["a design trade-off"] and "detail_count" not in out[0]
    assert out[1]["detail_count"] == 2 and "details" not in out[1]
    assert risks[1]["details"] == ["one long approach", "another"]


def test_every_risk_and_its_severity_stay():
    out = slim_summary_input(_payload())["risk_assessment"]
    assert [r["severity"] for r in out["risks"]] == ["HIGH"]
    assert out["overall_risk_level"] == "LOW"
    assert out["mitigation_strategies"] == ["m"]


def test_target_tables_drop_only_when_they_repeat_the_sources():
    same, renamed, empty = slim_summary_input(_payload())["trade_offs"]
    assert "target_tables" not in same
    assert renamed["target_tables"] == ["a_by_date"]
    assert empty["target_tables"] == []
    assert same["source_tables"] == ["db.a", "db.b"]


def test_table_mappings_keep_engine_names_without_scores():
    (m,) = slim_summary_input(_payload())["table_mappings"]
    assert "confidence_score" not in m
    assert m["alternatives"] == ["elasticache"]
    assert m["recommended_database"] == "dynamodb"


def test_everything_else_is_untouched_and_the_input_is_not_mutated():
    p = _payload()
    before = copy.deepcopy(p)
    out = slim_summary_input(p)
    assert p == before
    assert out["deterministic_summary"] == "kept"
    assert out["tco_analysis"] == before["tco_analysis"]


def test_slimming_twice_changes_nothing_more():
    once = slim_summary_input(_payload())
    assert slim_summary_input(once) == once


def test_an_empty_payload_is_fine():
    assert slim_summary_input({}) == {"ranking": []}
    assert slim_summary_input({"ranking": None, "trade_offs": None})["trade_offs"] is None


def test_every_wave_keeps_the_unit_of_its_share():
    p = _payload()
    p["migration_waves"].append({"wave": 2, "share_basis": "calls", "rationale": "x"})
    assert [w["share_basis"] for w in slim_summary_input(p)["migration_waves"]] == [
        "queries",
        "calls",
    ]


def _routine_risk(rule: str, description: str, **extra) -> dict:
    return {
        "risk_id": "RISK-014", "risk_type": "MIGRATION_COMPLEXITY", "severity": "HIGH",
        "rule": rule, "wave": 1, "description": description,
        "affected_tables": ["public.posts", "public.topics"], "query_ids": [],
        "mitigation": "Migrate these routines yourself before wave 1, then check them again. "
        "See the Wave 1 gate in the roadmap.",
        "to_do": "migrate 2 routines yourself", "details": ["function public.f: x"], **extra,
    }  # fmt: skip


def test_a_routine_risk_enters_as_one_short_line():
    """#2132: each routine risk keeps its id, severity, rule and wave, so it is still
    counted; its text repeats the Routines section and the wave's to-do line."""
    payload = _payload()
    risks = payload["risk_assessment"]["risks"]
    risks.append(
        _routine_risk(
            "routines.migrate_yourself",
            "[aurora_postgresql] We could not translate 2 routines automatically; migrate "
            "them yourself before wave 1. They are 1 function, 1 procedure.",
        )
    )
    risks.append(_routine_risk("routines.carried", "[aurora_postgresql] Wave 1: ...",
                               severity="LOW", risk_id="RISK-015"))  # fmt: skip
    out = slim_summary_input(payload)["risk_assessment"]["risks"]
    assert out[1:] == [
        {"risk_id": "RISK-014", "severity": "HIGH", "rule": "routines.migrate_yourself",
         "wave": 1},
        {"risk_id": "RISK-015", "severity": "LOW", "rule": "routines.carried", "wave": 1},
    ]  # fmt: skip
    # Every other risk keeps its fields, and the input is not changed.
    assert out[0] == slim_summary_input(_payload())["risk_assessment"]["risks"][0]
    assert risks[1]["mitigation"] and risks[1]["details"]


def test_open_risks_lose_their_mitigation_and_resolved_risks_become_counts():
    """#2132: the summary states risk counts and severities only: an open risk keeps its
    id, severity and description without its mitigation, and the resolved risks enter as
    their count by severity."""
    payload = _payload()
    risk = payload["risk_assessment"]
    risk["risks"][0]["mitigation"] = "Load-test the reads first. See the Wave 3 gate."
    risk["resolved_risks"] = [
        {"risk_id": "RISK-020", "severity": "HIGH", "description": "a", "mitigation": "m"},
        {"risk_id": "RISK-021", "severity": "low", "description": "b"},
        {"risk_id": "RISK-022", "severity": "HIGH", "description": "c"},
    ]
    out = slim_summary_input(payload)["risk_assessment"]
    assert "mitigation" not in out["risks"][0]
    assert out["risks"][0]["description"] == risk["risks"][0]["description"]
    assert "resolved_risks" not in out
    assert out["resolved_risk_count"] == 3
    assert out["resolved_risks_by_severity"] == {"HIGH": 2, "LOW": 1}
    assert risk["risks"][0]["mitigation"] and len(risk["resolved_risks"]) == 3  # not mutated
