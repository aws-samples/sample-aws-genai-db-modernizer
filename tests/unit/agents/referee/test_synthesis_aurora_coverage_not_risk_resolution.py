"""Aurora's own access patterns must not resolve an anti-pattern risk by
"coverage" alone (#157 review, I2).

Carry_over derives a pattern for every query Aurora owns mechanically -- the
query runs as SQL, unchanged. That is not a design decision the way a
DynamoDB key schema or a GSI is, so an anti-pattern flagged on a query that
stays on Aurora (``no-relational-need``/``single-access-pattern-table``, the
same two the capability-pin tests in test_synthesis_risk_capability_pin.py
cover) must not read as resolved just because Aurora's derivation happens to
include that query id. It still resolves, deliberately, when a sibling query
on the same table pins the table to Aurora by a hard capability or a
utility/DDL statement (``_capability_pin_reason`` -- unaffected by this fix,
since that path never read ``covered_by`` at all).
"""

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


def _real_access_pattern(query_id: str) -> dict:
    """A realistic, carry_over-derived Aurora access pattern (#157): script
    derived, in scope by assignment, covering ``query_id`` -- the shape
    ``derive_access_patterns`` actually produces, not a hand-wavy stub."""
    return {
        "pattern_id": query_id,
        "description": f"SELECT on db.t; filtered by id ({query_id})",
        "operation": "SELECT",
        "sql_text": "SELECT * FROM db.t WHERE id = ?",
        "source_tables": ["db.t"],
        "source_query_ids": [query_id],
        "filter_columns": ["id"],
        "sort_columns": [],
        "index_used": "PRIMARY",
        "design_rps": 1.0,
        "script_derived": True,
        "needs_judgment": False,
        "judgment_reason": None,
        "in_scope": True,
        "out_of_scope_reason": None,
        "load_testable": True,
        "load_testable_reason": None,
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
                schema_design={
                    "source_database": "db",
                    # Both flagged queries are "covered" -- carry_over derives
                    # one for every query Aurora owns, regardless of whether
                    # anything was actually designed for it.
                    "access_patterns": [
                        _real_access_pattern("q-simple"),
                        _real_access_pattern("q-sibling"),
                    ],
                },
            ),
        },
        assignment={
            "query_assignments": [
                _qa("q-simple", "aurora_mysql", "co-dependency group → aurora_mysql"),
                _qa("q-sibling", "aurora_mysql", sibling_reason),
            ]
        },
    )


def test_aurora_coverage_alone_does_not_resolve_the_risk() -> None:
    """No sibling pin reason: the risk stays open, even though Aurora's own
    derived access pattern technically "covers" the flagged query id."""
    result = build_risk_assessment(_data(sibling_reason=""))

    assert any("key-value workload" in r["description"] for r in result["risks"])
    assert not any(
        "the schema design covers every flagged query" in r.get("reason", "")
        for r in result["resolved_risks"]
    )


def test_utility_ddl_pin_still_resolves_with_the_deliberate_routing_explanation() -> None:
    """A sibling utility/DDL statement pinning the table still resolves the
    risk -- that path reads the assignment reason, not access-pattern
    coverage, so it is unaffected by Aurora's carry_over derivation."""
    result = build_risk_assessment(
        _data(
            sibling_reason=(
                "utility/metadata statement — kept on source-compatible relational engine "
                "(aurora_mysql) | [capability] dynamodb lacks required capability: sql_admin"
            )
        )
    )

    assert not any("key-value workload" in r["description"] for r in result["risks"])
    assert len(result["resolved_risks"]) == 1
    resolved = result["resolved_risks"][0]
    assert "Kept on Aurora MySQL because of a utility or DDL statement on this table." in (
        resolved["description"]
    )
    assert "the schema design covers every flagged query" not in resolved["reason"]
