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
    assert risk["resolved_risks"][0]["query_count"] == 0
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
