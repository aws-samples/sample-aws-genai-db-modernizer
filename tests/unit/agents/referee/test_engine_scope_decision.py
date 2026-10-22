"""The job's recorded engine scope, ``decisions/engine_scope.json`` (#1328).

Every entry point that starts a job with ``target_databases`` (the API, the CLI, the
orchestrator's ``start_job``) records it once; triage reads it when it is not handed a
scope, so all of them route the same way. No decision is no scope, as before.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest

from src.agents.referee.engine_scope import (
    DECISION_FILE,
    decision_key,
    record_engine_scope,
    recorded_engine_scope,
)
from src.agents.referee.triage_handler import run_triage
from src.contracts.engine_scope_decision import EngineScopeDecision
from src.shared.contract_versioning import contract_for_artifact, current_version
from src.shared.target_databases import TargetDatabasesError
from src.storage.local_store import LocalArtifactStore
from tests.unit.agents.referee.test_engine_scope import _collector, _query

DB, JOB = "db", "j"


@pytest.fixture
def store(tmp_path) -> LocalArtifactStore:
    s = LocalArtifactStore(str(tmp_path))
    s.write_json(f"{DB}/{JOB}/collector/output.json", _collector("mysql", [_query()]))
    return s


def _triage(store: LocalArtifactStore) -> dict:
    return store.read_json(f"{DB}/{JOB}/referee-triage/triage.json")


class TestTheDecision:
    def test_it_records_the_normalized_scope_who_chose_it_and_when(self, store) -> None:
        got = record_engine_scope(store, DB, JOB, ["DynamoDB", "aurora_postgresql"], via="api")
        assert got == ["aurora_postgresql", "dynamodb"]
        doc = store.read_json(f"{DB}/{JOB}/{DECISION_FILE}")
        assert doc["engines"] == ["aurora_postgresql", "dynamodb"]
        assert doc["decided_by"] == "customer"
        assert doc["via"] == "api"
        assert doc["job_id"] == JOB and doc["database_name"] == DB
        assert datetime.fromisoformat(doc["decided_at"]).tzinfo is not None
        assert doc["contract_version"] == current_version(EngineScopeDecision)
        assert recorded_engine_scope(store, DB, JOB) == ["aurora_postgresql", "dynamodb"]

    def test_a_refused_scope_writes_nothing(self, store) -> None:
        for bad in ([], ["aurora"], ["neptune"], ["nope"]):
            with pytest.raises(TargetDatabasesError):
                record_engine_scope(store, DB, JOB, bad, via="cli")
        assert not store.exists(decision_key(DB, JOB))

    def test_no_decision_is_no_scope(self, store) -> None:
        assert recorded_engine_scope(store, DB, JOB) is None

    def test_the_file_is_a_registered_contract(self) -> None:
        assert contract_for_artifact(decision_key(DB, JOB)) is EngineScopeDecision


class TestTriageReadsIt:
    def test_triage_routes_within_the_recorded_scope(self, store) -> None:
        record_engine_scope(store, DB, JOB, ["dynamodb", "aurora_postgresql"], via="cli")
        run_triage(JOB, DB, store)
        doc = _triage(store)
        assert doc["engine_scope"] == ["aurora_postgresql", "dynamodb"]
        assert {a["agent_type"] for a in doc["selected_agents"]} == {
            "aurora_postgresql",
            "dynamodb",
        }

    def test_a_scope_handed_to_triage_wins_over_the_recorded_one(self, store) -> None:
        record_engine_scope(store, DB, JOB, ["dynamodb", "aurora_postgresql"], via="cli")
        run_triage(JOB, DB, store, engine_scope=["dynamodb", "aurora_mysql"])
        assert _triage(store)["engine_scope"] == ["aurora_mysql", "dynamodb"]

    def test_with_no_decision_triage_is_unscoped_as_before(self, store, tmp_path) -> None:
        run_triage(JOB, DB, store)
        unscoped = _triage(store)
        assert unscoped["engine_scope"] is None
        # The same selection as a job that never heard of a scope (main's behaviour).
        other = LocalArtifactStore(str(tmp_path / "other"))
        other.write_json(f"{DB}/{JOB}/collector/output.json", _collector("mysql", [_query()]))
        run_triage(JOB, DB, other, engine_scope=None)
        assert unscoped["selected_agents"] == _triage(other)["selected_agents"]
        assert unscoped["skipped_agents"] == _triage(other)["skipped_agents"]


class TestTheOrchestratorStart:
    def test_start_job_records_the_scope_before_any_phase_runs(self, store) -> None:
        from src.orchestrator.local_orchestrator import LocalOrchestrator

        orch = LocalOrchestrator(store=store, llm_mode="none")
        seen: list[list[str] | None] = []

        def phase(job_id, phase, config=None, scope=None):  # noqa: ANN001
            seen.append(recorded_engine_scope(store, DB, job_id))

        with patch.object(orch, "_run_phase", side_effect=phase):
            orch.start_job(
                JOB, {"database_name": DB, "target_databases": ["DYNAMODB", "aurora_mysql"]}
            )
        assert seen and all(s == ["aurora_mysql", "dynamodb"] for s in seen)
        assert store.read_json(decision_key(DB, JOB))["via"] == "library"

    def test_start_job_refuses_a_bad_scope_before_any_phase_runs(self, store) -> None:
        from src.orchestrator.local_orchestrator import LocalOrchestrator

        orch = LocalOrchestrator(store=store, llm_mode="none")
        with patch.object(orch, "_run_phase") as run:
            with pytest.raises(TargetDatabasesError, match="is empty"):
                orch.start_job(JOB, {"database_name": DB, "target_databases": []})
        run.assert_not_called()
        assert not store.exists(decision_key(DB, JOB))

    def test_start_job_without_a_scope_records_nothing(self, store) -> None:
        from src.orchestrator.local_orchestrator import LocalOrchestrator

        orch = LocalOrchestrator(store=store, llm_mode="none")
        with patch.object(orch, "_run_phase"):
            orch.start_job(JOB, {"database_name": DB})
        assert not store.exists(decision_key(DB, JOB))
