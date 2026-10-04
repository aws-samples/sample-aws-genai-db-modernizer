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
