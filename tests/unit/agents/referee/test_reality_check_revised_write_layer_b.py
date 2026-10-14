"""The reality check's revised-assignment write keeps the gate honest (ADR-029 Layer B, #704).

``write_reality_check_result`` must recompute ``co_dependency_groups`` and
``table_assignments`` against the consolidated routing, and re-run ``AssignmentValidator``,
instead of spreading the previous version's derived views forward.
"""

from __future__ import annotations

from src.agents.referee.reality_check_handler import write_reality_check_result
from src.storage.local_store import LocalArtifactStore

DB, JOB = "mydb", "job-1"
STALE_WARNING = "WARNING [HIGH]: a split that belonged to the previous version"


def _qa(query_id: str, engine: str, table: str) -> dict:
    return {
        "query_id": query_id,
        "assigned_engine": engine,
        "confidence": 80,
        "source_tables": [table],
        "assignment_reason": "fixture",
        "in_scope": True,
    }


def _consolidation() -> dict:
    return {
        "from_engine": "opensearch",
        "to_engine": "dynamodb",
        "query_count": 1,
        "reason": "test consolidation",
        "saved_cost_estimate": 1.0,
        "action": "full",
        "queries_retained": [],
        "retention_reason": None,
    }


def _result(*, revised: list[dict], collector_queries: list[dict]) -> dict:
    """A reality-check result whose input assignment carries stale derived views."""
    original = [
        _qa("q1", "dynamodb", "t.users"),
        _qa("q2", "opensearch", "t.posts"),
        _qa("q3", "opensearch", "t.docs"),
    ]
    return {
        "revised_assignments": revised,
        "consolidations": [_consolidation()],
        "architectural_patterns": [],
        "recommendations": [],
        "unique_value_assessment": {},
        "executive_summary": None,
        "before_distribution": {"dynamodb": 1, "opensearch": 2},
        "after_distribution": {"dynamodb": 2, "opensearch": 1},
        "lightweight_recommendations": [],
        "collector_output": {
            "queries": {"query_patterns": collector_queries},
            "database_schema": {"tables": []},
        },
        "analysis_outputs": {"dynamodb": {}, "opensearch": {}},
        "assignment": {
            "job_id": JOB,
            "version": 1,
            "query_assignments": original,
            "co_dependency_groups": [["q1", "q2"]],
            "table_assignments": [
                {"table_id": "t.stale", "primary_engine": "opensearch", "secondary_engines": []}
            ],
            "validation_warnings": [STALE_WARNING],
        },
    }


def _written(tmp_path, result: dict) -> dict:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    version = write_reality_check_result(store, JOB, DB, result, assignment_version=1)
    assert version is not None
    return store.read_json(f"{DB}/{JOB}/assignment/v{version}/assignment.json")


class TestRevisedWriteRecomputesDerivedViews:
    def test_a_stale_co_dependency_group_is_dropped(self, tmp_path) -> None:
        # The collector's queries share no JOIN, so no group survives the recompute.
        revised = [
            _qa("q1", "dynamodb", "t.users"),
            _qa("q2", "dynamodb", "t.posts"),
            _qa("q3", "dynamodb", "t.docs"),
        ]
        collector = [
            {"query_id": "q1", "tables_accessed": ["t.users"]},
            {"query_id": "q2", "tables_accessed": ["t.posts"]},
            {"query_id": "q3", "tables_accessed": ["t.docs"]},
        ]

        written = _written(tmp_path, _result(revised=revised, collector_queries=collector))

        assert written["co_dependency_groups"] == []

    def test_table_assignments_follow_the_consolidated_routing(self, tmp_path) -> None:
        revised = [
            _qa("q1", "dynamodb", "t.users"),
            _qa("q2", "dynamodb", "t.posts"),
            _qa("q3", "dynamodb", "t.docs"),
        ]
        collector = [
            {"query_id": "q1", "tables_accessed": ["t.users"]},
            {"query_id": "q2", "tables_accessed": ["t.posts"]},
            {"query_id": "q3", "tables_accessed": ["t.docs"]},
        ]

        written = _written(tmp_path, _result(revised=revised, collector_queries=collector))

        by_table = {ta["table_id"]: ta["primary_engine"] for ta in written["table_assignments"]}
        assert by_table == {"t.users": "dynamodb", "t.posts": "dynamodb", "t.docs": "dynamodb"}

    def test_the_validator_is_re_run_on_the_consolidated_routing(self, tmp_path) -> None:
        # Two JOIN-sharing queries the consolidated routing leaves on different engines:
        # the fresh warning is written and the previous version's warning is gone.
        join = {"tables_accessed": ["t.users", "t.posts"], "has_joins": True, "join_count": 2}
        revised = [
            _qa("q1", "dynamodb", "t.users"),
            _qa("q2", "opensearch", "t.posts"),
            _qa("q3", "dynamodb", "t.docs"),
        ]
        collector = [{"query_id": "q1", **join}, {"query_id": "q2", **join}]

        written = _written(tmp_path, _result(revised=revised, collector_queries=collector))

        assert STALE_WARNING not in written["validation_warnings"]
        assert any(
            w.startswith("WARNING [HIGH]: Co-dependent queries ['q1', 'q2']")
            for w in written["validation_warnings"]
        ), written["validation_warnings"]
        assert written["co_dependency_groups"] == [["q1", "q2"]]
