"""Staleness-driven re-entry tools (ADR-029 Layer A): reopen + redispatch."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from unittest.mock import patch

import pytest

from src.atx_orchestrator import tools
from src.atx_orchestrator.runtime.store import upgrade_store
from src.contracts.assignment_models import (
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    QueryAssignment,
)
from src.storage.local_store import LocalArtifactStore

DB = "discourse"
JOB = "job-reentry"


def _qa(qid: str, engine: str) -> QueryAssignment:
    return QueryAssignment(
        query_id=qid,
        assigned_engine=engine,
        confidence=80,
        source_tables=[f"t.{qid}"],
        assignment_reason="test",
    )


def _write_assignment(
    store, version: int, qas: list[QueryAssignment], previous: int | None
) -> None:
    a = Assignment(
        job_id=JOB,
        version=version,
        status=AssignmentStatus.CUSTOMER_MODIFIED,
        source=AssignmentSource.CUSTOMER_GATE,
        timestamp=datetime.now(UTC),
        query_assignments=qas,
        table_assignments=[],
        co_dependency_groups=[],
        validation_warnings=[],
        previous_version=previous,
    )
    store.write_json(f"{DB}/{JOB}/assignment/v{version}/assignment.json", a.model_dump(mode="json"))


@pytest.fixture
def store(tmp_path):
    return upgrade_store(LocalArtifactStore(base_dir=str(tmp_path)))


class TestReopen:
    def test_reopen_flips_gate_back_to_awaiting(self, store) -> None:
        _write_assignment(store, 1, [_qa("q1", "dynamodb")], previous=None)
        with patch("src.atx_orchestrator.tools._make_store", return_value=store):
            tools._mark_assignment_review(JOB, tools.PhaseStatus.COMPLETED)
            assert tools._assignment_review_approved(JOB) is True
            out = json.loads(tools.reopen_assignment_review(JOB, DB))
            assert out["status"] == "reopened"
            assert out["assignment_version"] == 1
            assert tools._assignment_review_approved(JOB) is False

    def test_reopen_errors_without_assignment(self, store) -> None:
        with patch("src.atx_orchestrator.tools._make_store", return_value=store):
            out = json.loads(tools.reopen_assignment_review(JOB, DB))
            assert "error" in out

    def test_reopen_resumes_job_to_executing(self, store) -> None:
        # The job rests at AWAITING_HUMAN_INPUT between rounds; reopen must move it
        # back to EXECUTING first, or the re-raised HITL routing table (raised on a
        # non-terminal job) would not be submittable.
        _write_assignment(store, 1, [_qa("q1", "dynamodb")], previous=None)
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.runtime.job_status.resume_executing") as mock_resume,
        ):
            out = json.loads(tools.reopen_assignment_review(JOB, DB))
        assert out["status"] == "reopened"
        mock_resume.assert_called_once()


class TestRedispatch:
    def _seed_v1_schema(self, store) -> None:
        # v1 routing: q1,q2 -> dynamodb; q3 -> opensearch. Schema built at v1.
        _write_assignment(
            store, 1, [_qa("q1", "dynamodb"), _qa("q2", "dynamodb"), _qa("q3", "opensearch")], None
        )
        store.write_json(
            f"{DB}/{JOB}/schema-dynamodb/v1/schema_output.json",
            {"table_definitions": [{"table_name": "T"}], "assignment_version": 1},
        )
        store.write_json(
            f"{DB}/{JOB}/schema-opensearch/v1/schema_output.json",
            {"index_designs": [], "assignment_version": 1},
        )

    def test_copies_unaffected_forward_and_flags_affected(self, store) -> None:
        self._seed_v1_schema(store)
        # v2 re-entry: q3 moved opensearch -> aurora_postgresql. dynamodb unchanged,
        # opensearch eliminated, aurora_postgresql newly affected.
        _write_assignment(
            store,
            2,
            [_qa("q1", "dynamodb"), _qa("q2", "dynamodb"), _qa("q3", "aurora_postgresql")],
            previous=1,
        )
        with patch("src.atx_orchestrator.tools._make_store", return_value=store):
            out = json.loads(tools.redispatch_after_reroute(JOB, DB))

        assert out["assignment_version"] == 2
        assert out["previous_schema_version"] == 1
        assert out["affected_engines"] == ["aurora_postgresql"]
        assert out["copied_forward_engines"] == ["dynamodb"]
        assert out["dispatch_tools"] == ["run_schema_design_aurora_pg_via_a2a"]
        # dynamodb schema copied forward to v2, restamped.
        copied = store.read_json(f"{DB}/{JOB}/schema-dynamodb/v2/schema_output.json")
        assert copied["assignment_version"] == 2
        assert copied["table_definitions"] == [{"table_name": "T"}]
        # eliminated opensearch is NOT carried forward.
        assert not store.exists(f"{DB}/{JOB}/schema-opensearch/v2/schema_output.json")

    def test_no_prior_schema_treats_all_as_affected(self, store) -> None:
        _write_assignment(store, 1, [_qa("q1", "dynamodb"), _qa("q3", "opensearch")], None)
        with patch("src.atx_orchestrator.tools._make_store", return_value=store):
            out = json.loads(tools.redispatch_after_reroute(JOB, DB))
        assert out["previous_schema_version"] is None
        assert set(out["affected_engines"]) == {"dynamodb", "opensearch"}
        assert out["copied_forward_engines"] == []
