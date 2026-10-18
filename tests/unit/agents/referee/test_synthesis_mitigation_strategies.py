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
    build_wave_checks,
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
        "Use blue-green deployment with rollback for the tables behind the critical or high "
        "risks: "
        "wordpress.wp_posts, wordpress.wp_woocommerce_order_items."
    ) in strategies
    assert not any("closely" in s or "2 weeks" in s for s in strategies)


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


def test_coverage_gap_gets_its_own_line_not_the_unsupported_one() -> None:
    risks = WORDPRESS_LIKE + [
        _risk(
            "RISK-006",
            "MIGRATION_COMPLEXITY",
            "MEDIUM",
            "dynamodb",
            query_ids=["g1", "g2"],
            coverage_gap=True,
        )
    ]
    strategies = _build_mitigation_strategies(risks, EFFECTIVE)
    assert (
        "Add in-scope access patterns for the 2 queries the DynamoDB schema design does not "
        "serve (RISK-006), or route them to an engine that serves them."
    ) in strategies
    unsupported = next(s for s in strategies if "unsupported" in s)
    assert "RISK-006" not in unsupported


def test_no_open_risks_gives_no_strategies() -> None:
    """#645: no generic "monitor closely" line, and no status sentence as a strategy."""
    assert _build_mitigation_strategies([], EFFECTIVE) == []
    data = SynthesisData(job_id="j", database_name="db", engines={})
    assert build_risk_assessment(data)["mitigation_strategies"] == []


def _group(queries: list[tuple[str, float, float]], patterns: list[dict]) -> dict:
    return {
        "source_queries": [
            {"query_id": q, "execution_time_ms_avg": ms, "frequency_per_hour": f}
            for q, ms, f in queries
        ],
        "access_patterns": patterns,
    }


WAVES = [
    # wave 1 is a cutover: every in-scope query moves, whichever engine ends up owning it
    {"wave": 1, "engines": ["aurora_mysql"], "query_count": 2, "cutover_query_count": 4},
    {"wave": 2, "engines": ["elasticache"], "query_count": 2, "fronts": "aurora_mysql"},
    {"wave": 3, "engines": ["dynamodb"], "query_count": 1, "fronts": None},
]
GROUPS = [
    _group(
        [("a", 10.0, 300.0), ("b", 20.0, 100.0), ("c", 5.0, 50.0), ("d", 0.0, 50.0)],
        [
            {
                "engine": "aurora_mysql",
                "pattern_id": "p1",
                "design_rps": 2.0,
                "query_ids": ["a", "b"],
            },
            # listed in two groups: counted once; out of scope never counts
            {"engine": "dynamodb", "pattern_id": "p2", "design_rps": 4.0, "query_ids": ["c"]},
            {
                "engine": "dynamodb",
                "pattern_id": "p3",
                "design_rps": 9.0,
                "in_scope": False,
                "query_ids": ["c"],
            },
            # the cache layer owns no query: never part of a cutover rate
            {"engine": "elasticache", "pattern_id": "e1", "design_rps": 50.0, "query_ids": ["a"]},
        ],
    ),
    _group(
        [("a", 10.0, 300.0)],
        [{"engine": "dynamodb", "pattern_id": "p2", "design_rps": 4.0, "query_ids": ["c"]}],
    ),
]
ASSIGNED: list[dict] = [
    {"query_id": "a", "assigned_engine": "aurora_mysql"},
    {"query_id": "b", "assigned_engine": "aurora_mysql"},
    {"query_id": "c", "assigned_engine": "dynamodb"},
    {"query_id": "d", "assigned_engine": "dynamodb", "in_scope": False},
]
OVERLAY = {"engine": "elasticache", "call_share_percent": 70.44, "calls_per_second": 185.07}


def test_one_check_per_wave_from_report_facts() -> None:
    checks = build_wave_checks(WAVES, GROUPS, ASSIGNED, OVERLAY)
    assert checks == [
        # 3 in-scope queries (a, b, c) share the cutover; baseline and rate cover those
        "After wave 1 (Aurora MySQL): compare the latency of 4 queries with their source "
        "baseline (weighted average 11.67 ms) and check the engine sustains the design rate "
        "of 6 requests/s before starting wave 2.",
        "After wave 2 (ElastiCache): track the cache hit rate for the 2 hot reads it fronts "
        "on Aurora MySQL (70.4% of calls, 185.1 calls/s) and compare their latency with the "
        "source before starting wave 3.",
        "After wave 3 (DynamoDB): compare the latency of 1 query with their source baseline "
        "(weighted average 5 ms) and check the engine sustains the design rate of "
        "4 requests/s.",
    ]


def test_a_zero_baseline_is_not_printed() -> None:
    groups = [_group([("a", 0.0, 10.0)], [])]
    waves = [{"wave": 1, "engines": ["dynamodb"], "query_count": 1}]
    assigned = [{"query_id": "a", "assigned_engine": "dynamodb"}]
    (line,) = build_wave_checks(waves, groups, assigned, None)
    assert (
        line
        == "After wave 1 (DynamoDB): compare the latency of 1 query with their source baseline."
    )


def test_wave_checks_say_nothing_the_report_does_not_support() -> None:
    assert build_wave_checks(None, GROUPS, ASSIGNED, OVERLAY) == []
    assert build_wave_checks([], GROUPS, ASSIGNED, OVERLAY) == []
    bare = build_wave_checks([{"wave": 1, "engines": ["dynamodb"]}], [], [], None)
    assert bare == [
        "After wave 1 (DynamoDB): compare the latency of its queries with their source baseline."
    ]
    for line in build_wave_checks(WAVES, GROUPS, ASSIGNED, OVERLAY) + bare:
        assert "closely" not in line and "2 weeks" not in line
