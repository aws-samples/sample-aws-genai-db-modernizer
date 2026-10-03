"""Anti-pattern risks follow the queries to the engine they are assigned to (#221).

An analysis anti-pattern says "engine E is a poor fit for these queries". The
assignment (and the reality check) can then route some or all of those queries to
another engine. The risk register must describe the effective assignment:

- the ``[E]`` risk covers only the queries still assigned to E, and its
  "N remaining" count is computed over those queries only;
- queries moved to another engine T whose schema design addresses them (an access
  pattern, or an unsupported pattern that is already its own risk on T) are resolved
  by the move. They are recorded in ``resolved_risks``, never silently lost;
- queries moved to T that T's design does not address stay a risk, re-attributed to
  ``[T]`` with the original severity;
- placeholder table ids such as ``unknown`` are not affected tables.

The fixture mirrors the wordpress run that surfaced #221: Aurora MySQL's
"high-frequency PK lookup" (HIGH) query moved to DynamoDB, and DynamoDB's
"complex aggregation" (HIGH) queries moved to ElastiCache, whose design (keyed on
``source_query_ids``) covers only some of them.
"""

from __future__ import annotations

import pytest

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import build_risk_assessment


def _qa(query_id: str, engine: str, in_scope: bool = True) -> dict:
    return {"query_id": query_id, "assigned_engine": engine, "in_scope": in_scope}


def _anti(ap_type: str, query_ids: list[str], tables: list[str], weight: float) -> dict:
    return {
        "anti_pattern_type": ap_type,
        "description": f"{ap_type} description",
        "query_ids": query_ids,
        "table_ids": tables,
        "severity_weight": weight,
        "recommendation": f"{ap_type} fix",
    }


def _data() -> SynthesisData:
    queries = [
        {"query_id": "q-opt", "tables_accessed": ["wp.wp_options"]},
        {"query_id": "q-agg1", "tables_accessed": ["wp.wp_posts"]},
        {"query_id": "q-agg2", "tables_accessed": ["wp.wp_posts"]},
        {"query_id": "q-agg3", "tables_accessed": ["wp.wp_order_items"]},
        {"query_id": "q-kv1", "tables_accessed": ["wp.wp_comments"]},
        {"query_id": "q-kv2", "tables_accessed": ["wp.wp_api_keys"]},
        {"query_id": "q-kv3", "tables_accessed": ["wp.wp_api_keys"]},
        {"query_id": "q-found", "tables_accessed": []},
        {"query_id": "q-rel", "tables_accessed": ["wp.wp_terms"]},
    ]
    aurora_analysis = {
        "workload_analysis": {
            "anti_patterns_detected": [
                _anti("high-frequency-pk-lookup", ["q-opt"], ["wp.wp_options"], 0.8),
                _anti(
                    "single-access-pattern-table",
                    ["q-kv1", "q-kv2", "q-found"],
                    ["unknown", "wp.wp_comments", "wp.wp_api_keys"],
                    0.5,
                ),
                _anti(
                    "no-relational-need",
                    ["q-kv1", "q-kv3", "q-rel"],
                    ["wp.wp_comments", "wp.wp_api_keys", "wp.wp_terms"],
                    0.6,
                ),
            ]
        }
    }
    dynamodb_analysis = {
        "workload_analysis": {
            "anti_patterns_detected": [
                _anti(
                    "complex-aggregation",
                    ["q-agg1", "q-agg2", "q-agg3"],
                    ["wp.wp_posts", "wp.wp_order_items"],
                    0.7,
                ),
            ]
        }
    }
    dynamodb_schema = {
        "table_definitions": [{"table_name": "Options", "source_tables": ["wp.wp_options"]}],
        "access_patterns": [
            {"pattern_id": "DDB-AP-1", "query_ids": ["q-opt"], "in_scope": True},
            {"pattern_id": "DDB-AP-2", "query_ids": ["q-kv1"], "in_scope": True},
            {"pattern_id": "DDB-AP-3", "query_ids": ["q-kv2"], "in_scope": False},
        ],
        "unsupported_patterns": [
            {
                "query_ids": ["q-found"],
                "pattern_type": "aggregation",
                "recommendation": "Replace FOUND_ROWS() with a maintained counter.",
            }
        ],
    }
    elasticache_schema = {
        "key_designs": [{"key_pattern": "post:{id}", "source_tables": ["wp.wp_posts"]}],
        "access_patterns": [
            {"pattern_id": "EC-AP-1", "source_query_ids": ["q-agg1", "q-agg2"]},
        ],
    }
    aurora_schema = {
        "source_database": "wp",
        "table_definitions": [{"table_name": "wp_terms", "columns": []}],
        "access_patterns": [],
    }
    return SynthesisData(
        job_id="j",
        database_name="wp",
        collector={"queries": {"query_patterns": queries}},
        engines={
            "aurora_mysql": EngineArtifacts(
                "aurora_mysql", analysis=aurora_analysis, schema_design=aurora_schema
            ),
            "dynamodb": EngineArtifacts(
                "dynamodb", analysis=dynamodb_analysis, schema_design=dynamodb_schema
            ),
            "elasticache": EngineArtifacts(
                "elasticache", analysis={}, schema_design=elasticache_schema
            ),
        },
        assignment={
            "query_assignments": [
                _qa("q-opt", "dynamodb"),
                _qa("q-agg1", "elasticache"),
                _qa("q-agg2", "elasticache"),
                _qa("q-agg3", "elasticache"),
                _qa("q-kv1", "dynamodb"),
                _qa("q-kv2", "dynamodb"),
                _qa("q-kv3", "dynamodb"),
                _qa("q-found", "dynamodb"),
                _qa("q-rel", "aurora_mysql"),
            ]
        },
    )


@pytest.fixture
def result() -> dict:
    return build_risk_assessment(_data())


def _risks_by_type(result: dict, needle: str) -> list[dict]:
    return [r for r in result["risks"] if needle in r["description"]]


def _engine(risk: dict) -> str:
    return str(risk["description"]).split("]", 1)[0].lstrip("[")


class TestRiskFollowsItsQueries:
    def test_moved_and_covered_high_risk_is_resolved_not_kept_on_the_old_engine(
        self, result
    ) -> None:
        # q-opt moved to DynamoDB, where DDB-AP-1 serves it.
        assert not _risks_by_type(result, "high-frequency-pk-lookup")
        resolved = [
            r for r in result["resolved_risks"] if "high-frequency-pk-lookup" in r["description"]
        ]
        assert len(resolved) == 1
        assert resolved[0]["engine"] == "aurora_mysql"
        assert resolved[0]["severity"] == "HIGH"
        assert resolved[0]["resolved_on"] == "dynamodb"
        assert resolved[0]["query_ids"] == ["q-opt"]

    def test_moved_but_uncovered_high_risk_is_reattributed_with_its_severity(self, result) -> None:
        # q-agg1/2 are covered by ElastiCache's design (source_query_ids); q-agg3 is not.
        risks = _risks_by_type(result, "complex-aggregation")
        assert len(risks) == 1
        risk = risks[0]
        assert _engine(risk) == "elasticache"
        assert risk["severity"] == "HIGH"
        assert risk["query_ids"] == ["q-agg1", "q-agg2", "q-agg3"]
        assert "1 remaining" in risk["description"]
        assert "DynamoDB analysis" in risk["description"]
        assert risk["reattributed_from"] == "dynamodb"
        assert "complex-aggregation fix" in risk["mitigation"]

    def test_risk_on_own_engine_is_narrowed_to_its_own_queries(self, result) -> None:
        # no-relational-need: q-rel stays on Aurora; q-kv1 (DDB-AP-2) is covered on
        # DynamoDB; q-kv3 moved to DynamoDB but no DynamoDB pattern addresses it.
        risks = _risks_by_type(result, "no-relational-need")
        by_engine = {_engine(r): r for r in risks}
        assert set(by_engine) == {"aurora_mysql", "dynamodb"}
        own = by_engine["aurora_mysql"]
        assert own["query_ids"] == ["q-rel"]
        assert own["affected_tables"] == ["wp.wp_terms"]
        moved = by_engine["dynamodb"]
        assert moved["query_ids"] == ["q-kv1", "q-kv3"]
        assert moved["severity"] == "MEDIUM"
        assert "1 remaining" in moved["description"]
        assert moved["affected_tables"] == ["wp.wp_api_keys", "wp.wp_comments"]

    def test_unsupported_and_out_of_scope_patterns_count_as_addressed(self, result) -> None:
        # single-access-pattern-table: q-kv1 in scope, q-kv2 out of scope by design,
        # q-found already its own DynamoDB unsupported-pattern risk.
        assert not _risks_by_type(result, "single-access-pattern-table")
        assert any(
            "single-access-pattern-table" in r["description"] for r in result["resolved_risks"]
        )

    def test_unknown_is_never_an_affected_table(self, result) -> None:
        for risk in result["risks"] + result["resolved_risks"]:
            assert "unknown" not in (risk.get("affected_tables") or [])

    def test_high_risks_are_never_silently_lost(self, result) -> None:
        high_in = 2  # high-frequency-pk-lookup + complex-aggregation
        kept = [r for r in result["risks"] if r["severity"] == "HIGH"]
        resolved = [r for r in result["resolved_risks"] if r["severity"] == "HIGH"]
        assert len(kept) + len(resolved) == high_in


def test_own_engine_remaining_count_ignores_moved_queries() -> None:
    data = _data()
    # Put q-agg3 back on DynamoDB: the [dynamodb] risk keeps only that query, and the
    # covered-on-ElastiCache queries are resolved by the move.
    for qa in data.assignment["query_assignments"]:
        if qa["query_id"] == "q-agg3":
            qa["assigned_engine"] = "dynamodb"
    result = build_risk_assessment(data)
    risks = _risks_by_type(result, "complex-aggregation")
    assert [_engine(r) for r in risks] == ["dynamodb"]
    assert risks[0]["query_ids"] == ["q-agg3"]
    assert "(0% of queries resolved by schema design, 1 remaining)" in risks[0]["description"]
    assert risks[0]["affected_tables"] == ["wp.wp_order_items"]
    assert any(
        r["resolved_on"] == "elasticache" and "complex-aggregation" in r["description"]
        for r in result["resolved_risks"]
    )


def test_query_moved_to_aurora_runs_as_sql_and_resolves_a_nosql_risk() -> None:
    data = _data()
    for qa in data.assignment["query_assignments"]:
        if qa["query_id"].startswith("q-agg"):
            qa["assigned_engine"] = "aurora_mysql"
    result = build_risk_assessment(data)
    assert not _risks_by_type(result, "complex-aggregation")
    assert any(
        r["resolved_on"] == "aurora_mysql" and "complex-aggregation" in r["description"]
        for r in result["resolved_risks"]
    )


def test_without_an_assignment_every_risk_is_kept_on_its_engine() -> None:
    data = _data()
    data.assignment = None
    result = build_risk_assessment(data)
    agg = _risks_by_type(result, "complex-aggregation")
    assert [_engine(r) for r in agg] == ["dynamodb"]
    assert agg[0]["query_ids"] == ["q-agg1", "q-agg2", "q-agg3"]
    assert _risks_by_type(result, "high-frequency-pk-lookup")
    assert result["resolved_risks"] == []
