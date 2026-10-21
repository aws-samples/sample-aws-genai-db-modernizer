"""Every writer of an assignment version refuses one outside the engine scope (#1327).

ADR-031 Decision 2 Amendment 14: each writer runs ``engine_scope.check_in_scope``
next to ``move_ledger.check_replay``. These tests hand the writers the review named
(the source-of-truth reroute, the post-schema reroute and its rollback, and the
reality check after its refresh) a version with a query outside the scope recorded in
``triage.json`` and expect the typed refusal, with nothing written. Each fails when
its writer's check is given ``None`` as the scope.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from src.agents.referee.engine_scope import OutOfScopeAssignment
from src.orchestrator.local_orchestrator import LocalOrchestrator
from src.storage.local_store import LocalArtifactStore
from tests.unit.agents.referee import test_one_writer_per_table as one_writer
from tests.unit.tools.schema import test_pe_routing_flags as pe_routing


def _scope_triage(store, prefix: str, scope: list[str]) -> None:
    key = f"{prefix}/referee-triage/triage.json"
    triage = store.read_json(key) if store.exists(key) else {}
    store.write_json(key, {**triage, "engine_scope": scope})


class TestTheSourceOfTruthReroute:
    def test_a_reroute_outside_the_scope_is_refused_and_nothing_is_written(self) -> None:
        from src.agents.referee.assignment_overrides import (
            AssignmentOverrideError,
            OutOfScopeVersion,
            reroute_split_writes,
        )

        store = one_writer._MemStore()
        one_writer._seed_legacy_split(store)
        _scope_triage(store, f"{one_writer.DB}/{one_writer.JOB}", ["documentdb"])
        with pytest.raises(OutOfScopeVersion, match="source-of-truth reroute") as refused:
            reroute_split_writes(store, one_writer.DB, one_writer.JOB)
        # The API and the AWS Transform tools report it as a refused edit.
        assert isinstance(refused.value, AssignmentOverrideError)
        assert "DynamoDB is outside the scope" in str(refused.value)
        assert not store.exists(f"{one_writer.DB}/{one_writer.JOB}/assignment/v2/assignment.json")

    def test_a_reroute_inside_the_scope_is_written(self) -> None:
        from src.agents.referee.assignment_overrides import reroute_split_writes

        store = one_writer._MemStore()
        one_writer._seed_legacy_split(store)
        _scope_triage(store, f"{one_writer.DB}/{one_writer.JOB}", ["aurora_postgresql", "dynamodb"])
        assert reroute_split_writes(store, one_writer.DB, one_writer.JOB) is not None


CASE = pe_routing.TestTheOrchestratorRoutesAFlagByScore()
PREFIX = f"{CASE.DB}/{CASE.JOB}"


@pytest.mark.usefixtures("schema_contract_off")
class TestThePostSchemaReroute:
    def test_a_reroute_outside_the_scope_is_refused_and_nothing_is_written(self, tmp_path):
        store = LocalArtifactStore(base_dir=str(tmp_path))
        CASE._seed(store)
        # q-3 stays on DocumentDB, which the scope leaves out.
        _scope_triage(store, PREFIX, ["aurora_mysql", "dynamodb"])
        with pytest.raises(OutOfScopeAssignment, match="post-schema reroute"):
            CASE._run(store, [])
        assert not store.exists(f"{PREFIX}/assignment/v2/assignment.json")

    def test_a_reroute_inside_the_scope_is_written(self, tmp_path):
        store = LocalArtifactStore(base_dir=str(tmp_path))
        CASE._seed(store)
        _scope_triage(store, PREFIX, ["aurora_mysql", "documentdb", "dynamodb"])
        CASE._run(store, [])
        assert store.exists(f"{PREFIX}/assignment/v2/assignment.json")

    def test_a_rollback_outside_the_scope_is_refused_and_nothing_is_written(self, tmp_path):
        """The reroute (v2) is in scope; the scope then narrows before its redesign
        fails, so the rollback that would restate v1 is checked on its own."""
        store = LocalArtifactStore(base_dir=str(tmp_path))
        CASE._seed(store)
        _scope_triage(store, PREFIX, ["aurora_mysql", "documentdb", "dynamodb"])

        def designer_down(*_args, **_kwargs):
            _scope_triage(store, PREFIX, ["aurora_mysql", "documentdb"])
            raise RuntimeError("designer down")

        orch = LocalOrchestrator(store, llm_mode="bedrock")
        with (
            patch(
                "src.agents.schema_design.handler.run_schema_design_auto",
                side_effect=designer_down,
            ),
            pytest.raises(OutOfScopeAssignment, match="post-schema rollback") as refused,
        ):
            orch._run_post_schema_routing(CASE.JOB, CASE.DB)
        assert "DynamoDB is outside the scope" in str(refused.value)
        assert store.exists(f"{PREFIX}/assignment/v2/assignment.json")
        assert not store.exists(f"{PREFIX}/assignment/v3/assignment.json")


class TestTheRealityCheckChecksWhatItWrites:
    def test_a_cache_the_refresh_sets_outside_the_scope_is_refused(self) -> None:
        """The check runs on the version after ``refresh_consolidated_assignment``
        recomputed its cache fields, so a cache it sets is checked too."""
        from src.agents.referee import reality_check_handler
        from src.agents.referee.reality_check_handler import (
            run_reality_check_deterministic,
            write_reality_check_result,
        )
        from tests.unit.agents.referee.test_reality_check_llm_seam import _mock_store

        store = _mock_store()
        det = run_reality_check_deterministic("job-1", "mydb", store, 1)
        scope = sorted(
            {qa["assigned_engine"] for qa in det["revised_assignments"]}
            | {e for qa in det["revised_assignments"] for e in qa.get("tied_engines") or []}
        )
        assert "elasticache" not in scope
        det["triage"] = {**(det.get("triage") or {}), "engine_scope": scope}
        refresh = reality_check_handler.refresh_consolidated_assignment

        def cache_one(assignment, *args, **kwargs):
            out = refresh(assignment, *args, **kwargs)
            out["query_assignments"][0]["cache_engine"] = "elasticache"
            return out

        with (
            patch.object(reality_check_handler, "refresh_consolidated_assignment", cache_one),
            pytest.raises(OutOfScopeAssignment, match="ElastiCache, its cache, is outside"),
        ):
            write_reality_check_result(store, "job-1", "mydb", det, 1)
        assert not any("assignment/v2" in k for k in store._written)


class TestTheApiReportsAReadPathRefusal:
    def test_the_assignment_read_answers_409_naming_the_scope(self, tmp_path) -> None:
        """A saved split re-routed on read (#992) outside the scope is a refused
        request with the reason, not a server error."""
        from fastapi.testclient import TestClient

        from src.api.main import app
        from src.api.routes import assignments

        store = one_writer._local_legacy_split(tmp_path)
        _scope_triage(store, f"{one_writer.DB}/{one_writer.JOB}", ["documentdb"])
        assignments.artifact_store = store
        try:
            url = f"/api/v1/assessments/{one_writer.JOB}/assignments?database_name={one_writer.DB}"
            response = TestClient(app).get(url)
        finally:
            assignments.artifact_store = None
        assert response.status_code == 409
        assert "outside the customer's engine scope (DocumentDB)" in response.json()["detail"]
