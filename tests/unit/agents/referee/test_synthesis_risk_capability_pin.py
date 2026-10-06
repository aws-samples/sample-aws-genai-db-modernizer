"""A DynamoDB-alternative anti-pattern risk (aurora-anti-05 "no-relational-need",
aurora-anti-06 "single-access-pattern-table") must not read as a flat
contradiction of the routing when the SAME table also carries another
in-scope query the capability gate (#338) or the utility pin (#327) pinned to
Aurora -- it must name that reason and offer DynamoDB as a later-wave
opportunity for the flagged queries specifically (review of #375)."""

from __future__ import annotations

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import build_risk_assessment


def _qa(query_id: str, engine: str, reason: str = "") -> dict:
    return {
        "query_id": query_id,
        "assigned_engine": engine,
        "in_scope": True,
        "assignment_reason": reason,
    }


def _anti_pattern() -> dict:
    return {
        "anti_pattern_type": "single-access-pattern-table",
        "description": "This is a key-value workload wearing a relational costume.",
        "query_ids": ["q-simple"],
        "table_ids": ["db.t"],
        "severity_weight": 0.5,
        "recommendation": "Consider DynamoDB for simple key-value access.",
    }


def _data(sibling_reason: str) -> SynthesisData:
    queries = [
        {"query_id": "q-simple", "tables_accessed": ["db.t"]},
        {"query_id": "q-sibling", "tables_accessed": ["db.t"]},
    ]
    aurora_analysis = {"workload_analysis": {"anti_patterns_detected": [_anti_pattern()]}}
    return SynthesisData(
        job_id="j",
        database_name="db",
        collector={"queries": {"query_patterns": queries}},
        engines={
            "aurora_mysql": EngineArtifacts(
                "aurora_mysql",
                analysis=aurora_analysis,
                schema_design={"source_database": "db", "access_patterns": []},
            ),
        },
        assignment={
            "query_assignments": [
                _qa("q-simple", "aurora_mysql", "co-dependency group → aurora_mysql"),
                _qa("q-sibling", "aurora_mysql", sibling_reason),
            ]
        },
    )


class TestCapabilityPinnedTableRisk:
    def test_sibling_query_needing_joins_names_the_reason_and_offers_a_later_wave(self) -> None:
        result = build_risk_assessment(
            _data(
                "highest confidence for aurora_mysql | [capability] dynamodb lacks required "
                "capability: complex_joins; [capability] elasticache lacks required capability: "
                "complex_joins"
            )
        )
        risk = next(r for r in result["risks"] if "key-value workload" in r["description"])
        assert "multi-table joins" in risk["description"]
        assert "deliberate, not an oversight" in risk["description"]
        assert "No action needed now" in risk["mitigation"]
        assert "later wave" in risk["mitigation"]
        assert "Consider DynamoDB" not in risk["mitigation"]

    def test_sibling_query_needing_aggregation_names_that_reason(self) -> None:
        result = build_risk_assessment(
            _data(
                "co-dependency group → aurora_mysql | [capability] dynamodb lacks required "
                "capability: aggregation"
            )
        )
        risk = next(r for r in result["risks"] if "key-value workload" in r["description"])
        assert "aggregation" in risk["description"]

    def test_sibling_utility_statement_pin_is_named_too(self) -> None:
        result = build_risk_assessment(
            _data(
                "utility/metadata statement — kept on source-compatible relational engine "
                "(aurora_mysql) | [capability] dynamodb lacks required capability: sql_admin"
            )
        )
        risk = next(r for r in result["risks"] if "key-value workload" in r["description"])
        assert "utility or DDL statement" in risk["description"]

    def test_no_sibling_pin_keeps_the_plain_recommendation(self) -> None:
        """Control: with no capability/utility reason anywhere on the table, the
        risk keeps today's behaviour -- the catalog's own recommendation, with
        no rewording claiming a reason that is not actually there."""
        result = build_risk_assessment(_data("co-dependency group → aurora_mysql"))
        risk = next(r for r in result["risks"] if "key-value workload" in r["description"])
        assert risk["mitigation"] == "Consider DynamoDB for simple key-value access."
        assert "deliberate, not an oversight" not in risk["description"]
