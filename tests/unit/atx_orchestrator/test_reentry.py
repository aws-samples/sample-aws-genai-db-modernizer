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

    def test_reopen_resets_schema_view_for_clean_slate(self, store) -> None:
        # Re-entry must clear the previous round's schema-design and synthesis
        # states so the panel does not show stale SUCCEEDED/FAILED/STOPPED icons
        # that read as errors (ADR-029). The parent, all six per-engine sub-steps,
        # and synthesis must be reset to NOT_STARTED.
        _write_assignment(store, 1, [_qa("q1", "dynamodb")], previous=None)
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.tools.mark_step_not_started") as mock_reset,
        ):
            out = json.loads(tools.reopen_assignment_review(JOB, DB))
        assert out["status"] == "reopened"
        reset_phases = {call.args[0] for call in mock_reset.call_args_list}
        expected = {"schema", "synthesis"} | {
            f"schema_{engine}" for engine in set(tools._SCHEMA_ENGINES.values())
        }
        assert expected <= reset_phases


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

    def test_reused_engines_marked_succeeded_and_parent_running(self, store) -> None:
        # After reopen resets the schema view to NOT_STARTED, redispatch must
        # re-mark the copied-forward engines SUCCEEDED (reused) and the parent box
        # running, so reused engines do not linger at a pending clock while only
        # the affected engine re-runs.
        self._seed_v1_schema(store)
        _write_assignment(
            store,
            2,
            [_qa("q1", "dynamodb"), _qa("q2", "dynamodb"), _qa("q3", "aurora_postgresql")],
            previous=1,
        )
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.tools.mark_step_succeeded") as mock_succ,
            patch("src.atx_orchestrator.tools.mark_step_running") as mock_run,
        ):
            out = json.loads(tools.redispatch_after_reroute(JOB, DB))
        assert out["copied_forward_engines"] == ["dynamodb"]
        succeeded_phases = {call.args[0] for call in mock_succ.call_args_list}
        assert "schema_dynamodb" in succeeded_phases
        running_phases = {call.args[0] for call in mock_run.call_args_list}
        assert "schema" in running_phases

    def test_no_prior_schema_treats_all_as_affected(self, store) -> None:
        _write_assignment(store, 1, [_qa("q1", "dynamodb"), _qa("q3", "opensearch")], None)
        with patch("src.atx_orchestrator.tools._make_store", return_value=store):
            out = json.loads(tools.redispatch_after_reroute(JOB, DB))
        assert out["previous_schema_version"] is None
        assert set(out["affected_engines"]) == {"dynamodb", "opensearch"}
        assert out["copied_forward_engines"] == []
