"""Mitigation strategies are derived from the risks actually present (#222).

The decision report prints ``risk_assessment.mitigation_strategies`` and the executive
deck quotes its first entry as "Specified mitigation". Both used to be template lines
picked by risk *type* ("Refactor stored procedures, triggers, and views ..." for any
operational risk, the same load-test / blue-green / monitoring lines for every job).
Each strategy must now name the risks, engines and tables it is about, the HIGH risks
must come first with their own mitigation, and stored-procedure advice appears only
when a migration note concerns a procedure, trigger or view. (The engineering report
repeating a mitigation that equals the description is a renderer concern: grounding
needs the mitigation field even when it repeats the text.)
"""

from __future__ import annotations

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import (
    _build_mitigation_strategies,
    build_risk_assessment,
)


def _risk(rid: str, rtype: str, sev: str, engine: str, **extra) -> dict:
    return {
        "risk_id": rid,
        "risk_type": rtype,
        "severity": sev,
        "description": f"[{engine}] something about {rid}",
        "affected_tables": extra.pop("tables", []),
        "mitigation": extra.pop("mitigation", None),
        "query_ids": extra.pop("query_ids", []),
        **extra,
    }


WORDPRESS_LIKE = [
    _risk(
        "RISK-001",
        "PERFORMANCE_DEGRADATION",
        "HIGH",
        "elasticache",
        tables=["wordpress.wp_posts", "wordpress.wp_woocommerce_order_items"],
        mitigation="Pre-compute aggregates on write.",
        query_ids=["q1", "q2"],
    ),
    _risk("RISK-002", "MIGRATION_COMPLEXITY", "MEDIUM", "dynamodb", query_ids=["q3"]),
    _risk("RISK-003", "MIGRATION_COMPLEXITY", "MEDIUM", "elasticache", query_ids=["q4"]),
    _risk(
        "RISK-004",
        "OPERATIONAL_RISK",
        "MEDIUM",
        "elasticache",
        object_type="transaction",
        object_name="SQL_CALC_FOUND_ROWS pagination",
    ),
    _risk(
        "RISK-005",
        "PERFORMANCE_DEGRADATION",
        "MEDIUM",
        "dynamodb",
        mitigation="Cover the remaining queries.",
    ),
]
EFFECTIVE = {"dynamodb", "elasticache", "aurora_mysql"}


def test_high_risks_come_first_with_their_own_mitigation() -> None:
    strategies = _build_mitigation_strategies(WORDPRESS_LIKE, EFFECTIVE)
    assert strategies[0] == (
        "RISK-001 (HIGH, ElastiCache; wordpress.wp_posts, "
        "wordpress.wp_woocommerce_order_items): Pre-compute aggregates on write."
    )


def test_no_stored_procedure_advice_without_a_procedure_trigger_or_view() -> None:
    strategies = _build_mitigation_strategies(WORDPRESS_LIKE, EFFECTIVE)
    joined = " ".join(strategies)
    assert "stored procedure" not in joined.lower()
    assert "trigger" not in joined.lower()
    assert any("SQL_CALC_FOUND_ROWS pagination (RISK-004)" in s for s in strategies)


def test_procedure_note_names_the_procedure() -> None:
    risks = WORDPRESS_LIKE + [
        _risk(
            "RISK-006",
            "OPERATIONAL_RISK",
            "MEDIUM",
            "elasticache",
            object_type="procedure",
            object_name="order report aggregation",
        )
    ]
    strategies = _build_mitigation_strategies(risks, EFFECTIVE)
    assert any(
        s == "Re-implement the procedure 'order report aggregation' as application logic "
        "before migration (RISK-006)."
        for s in strategies
    )


def test_unsupported_pattern_line_is_a_sentence_naming_risks_and_engines() -> None:
    strategies = _build_mitigation_strategies(WORDPRESS_LIKE, EFFECTIVE)
    line = next(s for s in strategies if "unsupported" in s)
    assert line == (
        "Serve the 2 unsupported query patterns on DynamoDB and ElastiCache "
        "(RISK-002, RISK-003) in Aurora MySQL (full-text indexes, SQL aggregation) "
        "or in application code."
    )


def test_unsupported_pattern_line_without_complementary_engines() -> None:
    strategies = _build_mitigation_strategies(WORDPRESS_LIKE, {"dynamodb", "elasticache"})
    line = next(s for s in strategies if "unsupported" in s)
    assert line.endswith("(RISK-002, RISK-003) in application code.")


def test_generic_lines_are_scoped_to_the_risks() -> None:
    strategies = _build_mitigation_strategies(WORDPRESS_LIKE, EFFECTIVE)
    assert (
        "Load-test the queries behind RISK-001 and RISK-005 on ElastiCache and DynamoDB "
        "with production-scale data before cutover."
    ) in strategies
    assert (
        "Use blue-green deployment with rollback for the tables behind the HIGH risks: "
        "wordpress.wp_posts, wordpress.wp_woocommerce_order_items."
    ) in strategies
    assert strategies[-1] == (
        "Monitor ElastiCache, DynamoDB and Aurora MySQL closely during the first 2 weeks "
        "post-migration."
    )


def test_no_high_risk_no_blue_green_line() -> None:
    risks = [r for r in WORDPRESS_LIKE if r["severity"] != "HIGH"]
    strategies = _build_mitigation_strategies(risks, EFFECTIVE)
    assert not any("blue-green" in s for s in strategies)
    assert not strategies[0].startswith("RISK-")


def test_high_risk_without_a_mitigation_says_so() -> None:
    risks = [_risk("RISK-001", "PERFORMANCE_DEGRADATION", "HIGH", "dynamodb", query_ids=["a"])]
    strategies = _build_mitigation_strategies(risks, EFFECTIVE)
    assert strategies[0] == (
        "RISK-001 (HIGH, DynamoDB; 1 query): no mitigation recorded; define one before " "cutover."
    )


def test_risk_assessment_carries_the_migration_note_object() -> None:
    schema = {
        "table_definitions": [{"table_name": "T", "source_tables": ["db.t"]}],
        "access_patterns": [],
        "migration_notes": [
            {
                "object_type": "trigger",
                "object_name": "audit_insert",
                "source_table": "db.t",
                "application_logic_required": "Write the audit row from the service.",
            }
        ],
    }
    data = SynthesisData(
        job_id="j",
        database_name="db",
        engines={"dynamodb": EngineArtifacts("dynamodb", analysis={}, schema_design=schema)},
    )
    ra = build_risk_assessment(data)
    note = next(r for r in ra["risks"] if r["risk_type"] == "OPERATIONAL_RISK")
    assert note["object_type"] == "trigger"
    assert note["object_name"] == "audit_insert"
    assert any("trigger 'audit_insert'" in s for s in ra["mitigation_strategies"])
