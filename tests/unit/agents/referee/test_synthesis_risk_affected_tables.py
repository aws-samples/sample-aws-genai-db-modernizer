"""Unsupported-pattern, migration-note and coverage-gap risks name their tables (#252).

Unsupported-pattern risks always had ``affected_tables: []`` although their queries'
source tables are known. They are now filled from the queries' ``tables_accessed``
(falling back to the assignment's ``source_tables``), in the collector's ``<db>.table``
form, with pseudo-tables (``unknown``, ``DUAL``) dropped.
"""

from __future__ import annotations

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import build_risk_assessment

_TABLES = ["wordpress.wp_postmeta", "wordpress.wp_posts", "wordpress.wp_users"]


def _data(schema: dict, queries: list[dict], assignment: dict | None = None) -> SynthesisData:
    data = SynthesisData(job_id="j", database_name="wordpress")
    data.collector = {
        "database_schema": {"tables": [{"table_id": t} for t in _TABLES]},
        "queries": {"query_patterns": queries},
    }
    data.engines["dynamodb"] = EngineArtifacts("dynamodb", analysis={}, schema_design=schema)
    data.assignment = assignment
    return data


def _unsupported(*ids: str) -> dict:
    return {
        "unsupported_patterns": [
            {"query_ids": list(ids), "pattern_type": "aggregation", "recommendation": "Count."}
        ]
    }


def test_unsupported_pattern_risk_lists_its_queries_tables() -> None:
    queries = [
        {"query_id": "q1", "tables_accessed": ["wordpress.wp_postmeta", "wordpress.wp_posts"]},
        {"query_id": "q2", "tables_accessed": ["wordpress.wp_posts"]},
    ]
    risk = build_risk_assessment(_data(_unsupported("q1", "q2"), queries))["risks"][0]
    assert risk["affected_tables"] == ["wordpress.wp_postmeta", "wordpress.wp_posts"]


def test_bare_names_are_qualified_and_pseudo_tables_dropped() -> None:
    queries = [{"query_id": "q1", "tables_accessed": ["wp_users", "unknown", "DUAL"]}]
    risk = build_risk_assessment(_data(_unsupported("q1"), queries))["risks"][0]
    assert risk["affected_tables"] == ["wordpress.wp_users"]


def test_query_without_tables_leaves_the_list_empty() -> None:
    queries = [{"query_id": "q1", "query_text": "SELECT FOUND_ROWS()", "tables_accessed": []}]
    risk = build_risk_assessment(_data(_unsupported("q1"), queries))["risks"][0]
    assert risk["affected_tables"] == []


def test_assignment_source_tables_are_the_fallback() -> None:
    assignment = {
        "query_assignments": [
            {"query_id": "q1", "assigned_engine": "dynamodb", "source_tables": ["wp_posts"]}
        ]
    }
    risk = build_risk_assessment(_data(_unsupported("q1"), [{"query_id": "q1"}], assignment))[
        "risks"
    ][0]
    assert risk["affected_tables"] == ["wordpress.wp_posts"]


def test_without_known_source_tables_names_are_kept_minus_placeholders() -> None:
    data = _data(_unsupported("q1"), [{"query_id": "q1", "tables_accessed": ["t1", "unknown"]}])
    data.collector["database_schema"] = {}
    risk = build_risk_assessment(data)["risks"][0]
    assert risk["affected_tables"] == ["t1"]


def test_migration_note_source_table_is_qualified() -> None:
    schema = {
        "migration_notes": [
            {
                "object_type": "trigger",
                "object_name": "audit",
                "source_table": "wp_posts",
                "application_logic_required": "Write the audit row in the service.",
            }
        ]
    }
    risk = build_risk_assessment(_data(schema, []))["risks"][0]
    assert risk["affected_tables"] == ["wordpress.wp_posts"]


def test_coverage_gap_tables_are_normalised() -> None:
    # The anti-pattern advised DynamoDB for q1 (now there); DynamoDB's design does not
    # serve it, so it becomes a coverage gap on DynamoDB.
    data = _data({}, [{"query_id": "q1", "tables_accessed": ["wp_posts", "unknown"]}])
    data.engines["aurora_mysql"] = EngineArtifacts(
        "aurora_mysql",
        analysis={
            "workload_analysis": {
                "anti_patterns_detected": [
                    {
                        "anti_pattern_type": "hot-key",
                        "description": "Hot key reads.",
                        "recommendation": "Move these lookups to DynamoDB.",
                        "query_ids": ["q1"],
                        "table_ids": ["wp_posts"],
                        "severity_weight": 0.5,
                    }
                ]
            }
        },
        schema_design={},
    )
    data.assignment = {
        "query_assignments": [
            {"query_id": "q1", "assigned_engine": "dynamodb"},
            {"query_id": "q2", "assigned_engine": "aurora_mysql"},
        ]
    }
    gap = next(r for r in build_risk_assessment(data)["risks"] if r.get("coverage_gap"))
    assert gap["affected_tables"] == ["wordpress.wp_posts"]


# Review of #252: table ids are "<schema>.<table>" on PostgreSQL, SQL Server and Oracle,
# and tables_accessed may be bare or differently cased.
def _pg_data(schema: dict, queries: list[dict], known: list[str]) -> SynthesisData:
    data = _data(schema, queries)
    data.database_name = "shop"
    data.collector["database_schema"] = {"tables": [{"table_id": t} for t in known]}
    return data


def test_postgres_schema_qualified_ids_match_bare_names() -> None:
    data = _pg_data(
        _unsupported("q1"),
        [{"query_id": "q1", "tables_accessed": ["orders", "Customers"]}],
        ["public.orders", "public.customers"],
    )
    risk = build_risk_assessment(data)["risks"][0]
    assert risk["affected_tables"] == ["public.customers", "public.orders"]


def test_case_insensitive_match_on_a_qualified_name() -> None:
    data = _pg_data(
        _unsupported("q1"),
        [{"query_id": "q1", "tables_accessed": ["DBO.Orders"]}],
        ["dbo.orders"],
    )
    assert build_risk_assessment(data)["risks"][0]["affected_tables"] == ["dbo.orders"]


def test_ambiguous_or_unknown_names_are_kept_as_given() -> None:
    data = _pg_data(
        _unsupported("q1"),
        [{"query_id": "q1", "tables_accessed": ["orders", "audit_log", "unknown"]}],
        ["sales.orders", "archive.orders"],
    )
    assert build_risk_assessment(data)["risks"][0]["affected_tables"] == [
        "audit_log",
        "orders",
    ]


def test_postgres_migration_note_and_coverage_gap_tables() -> None:
    note = {
        "object_type": "trigger",
        "object_name": "audit",
        "source_table": "Orders",
        "application_logic_required": "Write the audit row in the service.",
    }
    data = _pg_data(
        {"migration_notes": [note]},
        [{"query_id": "q1", "tables_accessed": ["orders"]}],
        ["public.orders"],
    )
    data.engines["aurora_postgresql"] = EngineArtifacts(
        "aurora_postgresql",
        analysis={
            "workload_analysis": {
                "anti_patterns_detected": [
                    {
                        "anti_pattern_type": "hot-key",
                        "description": "Hot key reads.",
                        "recommendation": "Move these lookups to DynamoDB.",
                        "query_ids": ["q1"],
                        "table_ids": ["orders"],
                        "severity_weight": 0.5,
                    }
                ]
            }
        },
        schema_design={},
    )
    data.assignment = {
        "query_assignments": [
            {"query_id": "q1", "assigned_engine": "dynamodb"},
            {"query_id": "q2", "assigned_engine": "aurora_postgresql"},
        ]
    }
    risks = build_risk_assessment(data)["risks"]
    note_risk = next(r for r in risks if r["risk_type"] == "OPERATIONAL_RISK")
    gap = next(r for r in risks if r.get("coverage_gap"))
    assert note_risk["affected_tables"] == ["public.orders"]
    assert gap["affected_tables"] == ["public.orders"]
