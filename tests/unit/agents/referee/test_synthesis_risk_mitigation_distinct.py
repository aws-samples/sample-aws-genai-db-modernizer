"""A risk's mitigation is never a copy of its description (#252).

DynamoDB unsupported patterns carry only ``pattern_type`` and ``recommendation``, so
the risk used to read ``[dynamodb] aggregation: <recommendation>`` with the same
``recommendation`` as its mitigation. Engines with ``reason`` + ``workaround`` put both
in the description and repeated the workaround as the mitigation, and migration notes
repeated ``application_logic_required`` in both fields. The description now states the
problem and the mitigation the fix; a mitigation still contained in the description is
dropped (renderers omit an empty one).
"""

from __future__ import annotations

import pytest

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import build_risk_assessment

# Real DynamoDB unsupported patterns from the wordpress UI-mode run (RISK-001, RISK-005).
_COUNT_QID = "q-postmeta-count"
_FOUND_ROWS_QID = "q-found-rows"
_DYNAMODB_UNSUPPORTED = [
    {
        "query_ids": [_COUNT_QID],
        "pattern_type": "aggregation",
        "recommendation": (
            "COUNT of postmeta rows for a post and meta key: Query PK=post_id with SK "
            "begins_with 'META#<meta_key>#' and count the returned items in the "
            "application (normally 0 or 1)."
        ),
    },
    {
        "query_ids": [_FOUND_ROWS_QID],
        "pattern_type": "aggregation",
        "recommendation": (
            "SELECT FOUND_ROWS() is a MySQL session function returning the total row "
            "count of the previous SQL_CALC_FOUND_ROWS query and has no DynamoDB "
            "equivalent; the application must compute totals itself (counters or "
            "paginated Query counts)."
        ),
    },
]
_COUNT_SQL = "SELECT COUNT(*) FROM wp_postmeta WHERE post_id = ? AND meta_key = ?"
_SOURCE_QUERIES = [
    {"query_id": _COUNT_QID, "query_text": _COUNT_SQL, "tables_accessed": ["wp_postmeta"]},
    {"query_id": _FOUND_ROWS_QID, "query_text": "SELECT FOUND_ROWS()", "tables_accessed": []},
]

# Real ElastiCache unsupported patterns from the same run (RISK-007, RISK-008).
_ELASTICACHE_UNSUPPORTED = [
    {
        "source_query_ids": ["q-users-by-role"],
        "reason": (
            "Join of wp_users and wp_usermeta with LIKE on meta_value and ORDER BY "
            "display_name; pattern matching is not supported."
        ),
        "workaround": (
            "Maintain a per-role set of user IDs and resolve display names with pipelined "
            "HGETs, or keep in the relational database."
        ),
    },
    {
        "source_query_ids": ["q-set-session"],
        "reason": "SET SESSION is a relational administrative statement.",
        "workaround": "SET SESSION is a relational administrative statement.",
    },
]

# Real ElastiCache migration note from the combined UI/chat run (RISK-007).
_MIGRATION_NOTE = {
    "object_type": "transaction",
    "object_name": "sales report aggregation",
    "application_logic_required": (
        "A background job must recompute the daily sales aggregates (joins of posts, "
        "order items and item meta) and write them to the sorted sets, since Redis "
        "cannot join."
    ),
}


def _norm(text: str | None) -> str:
    return " ".join(str(text or "").split()).casefold()


def _data(schemas: dict[str, dict], source_queries: list[dict] | None = None) -> SynthesisData:
    data = SynthesisData(job_id="job-1", database_name="wordpress")
    data.triage = {"selected_agents": [{"agent_type": e} for e in schemas]}
    data.collector = {"queries": {"query_patterns": source_queries or []}}
    for engine, schema in schemas.items():
        data.engines[engine] = EngineArtifacts(engine=engine, analysis={}, schema_design=schema)
    return data


def _assert_distinct(risk: dict) -> None:
    mitigation = _norm(risk.get("mitigation"))
    if mitigation:
        assert mitigation not in _norm(risk["description"]), risk


class TestDynamoDBUnsupportedPatterns:
    def _risks(self) -> list[dict]:
        out = build_risk_assessment(
            _data({"dynamodb": {"unsupported_patterns": _DYNAMODB_UNSUPPORTED}}, _SOURCE_QUERIES)
        )
        risks: list[dict] = out["risks"]
        return risks

    def test_mitigation_is_the_recommendation_and_not_in_the_description(self) -> None:
        risks = self._risks()
        assert len(risks) == 2
        for risk, pattern in zip(risks, _DYNAMODB_UNSUPPORTED, strict=True):
            assert risk["mitigation"] == pattern["recommendation"]
            assert pattern["recommendation"] not in risk["description"]
            _assert_distinct(risk)

    def test_description_states_the_problem_and_quotes_the_source_query(self) -> None:
        risk = self._risks()[0]
        assert risk["description"] == (
            "[dynamodb] aggregation: DynamoDB has no native equivalent for this query: "
            f"{_COUNT_SQL}"
        )

    def test_without_source_query_text_the_description_still_states_the_problem(self) -> None:
        out = build_risk_assessment(
            _data({"dynamodb": {"unsupported_patterns": _DYNAMODB_UNSUPPORTED[:1]}})
        )
        assert out["risks"][0]["description"] == (
            "[dynamodb] aggregation: DynamoDB has no native equivalent for this query."
        )

    def test_several_queries_quote_the_first_one(self) -> None:
        pattern = {**_DYNAMODB_UNSUPPORTED[0], "query_ids": [_COUNT_QID, _FOUND_ROWS_QID]}
        out = build_risk_assessment(
            _data({"dynamodb": {"unsupported_patterns": [pattern]}}, _SOURCE_QUERIES)
        )
        assert out["risks"][0]["description"] == (
            "[dynamodb] aggregation: DynamoDB has no native equivalent for these 2 queries, "
            f"e.g. {_COUNT_SQL}"
        )

    def test_long_sql_is_clipped_to_one_line(self) -> None:
        long_sql = (
            "SELECT a,\n  b FROM t WHERE "  # nosec B608 -- SQL text is test fixture data, never executed
            + " AND ".join(f"c{i} = ?" for i in range(40))
        )
        out = build_risk_assessment(
            _data(
                {"dynamodb": {"unsupported_patterns": _DYNAMODB_UNSUPPORTED[:1]}},
                [{"query_id": _COUNT_QID, "query_text": long_sql}],
            )
        )
        description = out["risks"][0]["description"]
        assert "\n" not in description
        assert description.endswith("…")
        assert len(description) < 220


class TestReasonAndWorkaroundPatterns:
    def test_description_is_the_reason_and_mitigation_the_workaround(self) -> None:
        out = build_risk_assessment(
            _data({"elasticache": {"unsupported_patterns": _ELASTICACHE_UNSUPPORTED[:1]}})
        )
        risk = out["risks"][0]
        pattern = _ELASTICACHE_UNSUPPORTED[0]
        assert risk["description"] == f"[elasticache] unsupported pattern: {pattern['reason']}"
        assert risk["mitigation"] == pattern["workaround"]

    def test_workaround_equal_to_reason_leaves_no_mitigation(self) -> None:
        out = build_risk_assessment(
            _data({"elasticache": {"unsupported_patterns": _ELASTICACHE_UNSUPPORTED[1:]}})
        )
        assert out["risks"][0]["mitigation"] is None


class TestMigrationNotes:
    def test_description_names_the_object_and_mitigation_the_logic(self) -> None:
        out = build_risk_assessment(_data({"elasticache": {"migration_notes": [_MIGRATION_NOTE]}}))
        risk = out["risks"][0]
        assert risk["description"] == (
            "[elasticache] transaction: sales report aggregation needs application-side "
            "logic on ElastiCache."
        )
        assert risk["mitigation"] == (
            "Implement as application logic: " + _MIGRATION_NOTE["application_logic_required"]
        )
        _assert_distinct(risk)

    def test_no_logic_text_still_gets_a_concrete_mitigation(self) -> None:
        """#380: a MEDIUM+ risk always carries a concrete mitigation -- with no
        logic text to build from, this falls back to a reviewable action
        instead of leaving the risk with no mitigation at all."""
        note = {**_MIGRATION_NOTE, "application_logic_required": ""}
        out = build_risk_assessment(_data({"elasticache": {"migration_notes": [note]}}))
        mitigation = out["risks"][0]["mitigation"]
        assert mitigation is not None
        assert "sales report aggregation" in mitigation
        assert "before cutover" in mitigation


@pytest.mark.parametrize(
    "recommendation",
    [
        "Covered by the composite index.",
        "  covered BY the composite   index. ",
    ],
)
def test_anti_pattern_recommendation_repeating_the_description_is_dropped(
    recommendation: str,
) -> None:
    analysis = {
        "workload_analysis": {
            "anti_patterns_detected": [
                {
                    "anti_pattern_type": "hot-partition",
                    "description": "Hot key on wp_options. Covered by the composite index.",
                    "recommendation": recommendation,
                    "query_ids": ["q1"],
                    "table_ids": ["wp_options"],
                    "severity_weight": 0.5,
                }
            ]
        }
    }
    data = _data({"dynamodb": {}})
    data.engines["dynamodb"].analysis = analysis
    risk = build_risk_assessment(data)["risks"][0]
    assert risk["mitigation"] is None


def test_every_risk_in_the_evidence_shape_has_a_distinct_mitigation() -> None:
    out = build_risk_assessment(
        _data(
            {
                "dynamodb": {"unsupported_patterns": _DYNAMODB_UNSUPPORTED},
                "elasticache": {
                    "unsupported_patterns": _ELASTICACHE_UNSUPPORTED,
                    "migration_notes": [_MIGRATION_NOTE],
                },
            },
            _SOURCE_QUERIES,
        )
    )
    assert len(out["risks"]) == 5
    for risk in out["risks"]:
        _assert_distinct(risk)


def test_sql_excerpt_drops_backticks() -> None:
    """Backticks would open code spans in the Markdown engineering report (#252 review)."""
    out = build_risk_assessment(
        _data(
            {"dynamodb": {"unsupported_patterns": _DYNAMODB_UNSUPPORTED[:1]}},
            [{"query_id": _COUNT_QID, "query_text": "SELECT COUNT ( * ) FROM `wp_postmeta`"}],
        )
    )
    description = out["risks"][0]["description"]
    assert "`" not in description
    assert description.endswith("this query: SELECT COUNT ( * ) FROM wp_postmeta")


def test_opensearch_source_query_is_the_sql_fallback() -> None:
    pattern = {"query_ids": ["q-missing"], "source_query": "SELECT * FROM t WHERE x LIKE ?"}
    out = build_risk_assessment(_data({"opensearch": {"unsupported_patterns": [pattern]}}))
    assert out["risks"][0]["description"] == (
        "[opensearch] unsupported pattern: OpenSearch has no native equivalent for this "
        "query: SELECT * FROM t WHERE x LIKE ?"
    )
