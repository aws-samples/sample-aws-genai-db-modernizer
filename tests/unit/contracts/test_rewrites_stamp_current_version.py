"""A writer that rewrites a contract file as a raw dict writes the current version (#1018).

#971 makes a model loaded through ``load_contract`` write back the current
``contract_version``. A writer that reads a contract file as a dict, patches it
and writes it back never parses it, so it kept the version it read: after the
next MINOR bump the file would carry the newer MINOR's fields under the older
MINOR's number. Each writer below now stamps the registered contract's current
version (``stamp_artifact_version``, ADR-008 Decision 2).

Every test moves the contract's default to ``<MAJOR>.99`` (a version no code
spells out), so the file the test stores at today's version is one at an older
MINOR, and checks the rewrite says ``<MAJOR>.99``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from src.contracts.agent_interaction_models import AgentAnswers
from src.contracts.assignment_models import Assignment
from src.contracts.collector_output import CollectorOutputContract
from src.contracts.dynamodb_model_output import DynamoDBModelOutputContract
from src.contracts.phase_models import PhaseProgression
from src.shared.contract_versioning import current_version
from src.storage.local_store import LocalArtifactStore


@contextmanager
def bumped(model: type[BaseModel]) -> Iterator[str]:
    """``model``'s default moved to ``<MAJOR>.99`` for the duration."""
    field = model.model_fields["contract_version"]
    original = field.default
    field.default = f"{original.split('.', 1)[0]}.99"
    model.model_rebuild(force=True)
    try:
        yield field.default
    finally:
        field.default = original
        model.model_rebuild(force=True)


def _stored(tmp_path: Path, key: str) -> dict:
    data: dict = json.loads((tmp_path / key).read_text())
    return data


def test_the_bump_moves_the_current_version_and_restores_it() -> None:
    before = current_version(Assignment)
    with bumped(Assignment) as version:
        assert current_version(Assignment) == version != before
    assert current_version(Assignment) == before


# --- assignment rewrites ---------------------------------------------------------


def test_the_synthesis_cache_overlay_patch_writes_the_current_version(tmp_path: Path) -> None:
    from src.agents.referee.synthesis_handler import run_synthesis
    from tests.unit.agents.referee import test_cache_safety_net_notes_reset as overlay

    store = LocalArtifactStore(base_dir=str(tmp_path))
    overlay._seed(store)
    key = f"{overlay.DB}/{overlay.JOB}/assignment/v2/assignment.json"
    stored = {**overlay.ASSIGNMENT_V2, "contract_version": current_version(Assignment)}
    store.write_json(key, stored)

    with bumped(Assignment) as version:
        run_synthesis(overlay.JOB, overlay.DB, store, assignment_version=2, llm_mode="none")
        written = _stored(tmp_path, key)
    assert written["cache_safety_net_notes"], "the safety net should have dropped an overlay"
    assert written["contract_version"] == version


def test_the_reality_check_revision_writes_the_current_version(tmp_path: Path) -> None:
    from src.agents.referee.reality_check_handler import write_reality_check_result
    from tests.unit.agents.referee.ledger_fakes import recorded_ledger

    original = [
        {"query_id": "q1", "assigned_engine": "opensearch", "in_scope": True},
        {"query_id": "q2", "assigned_engine": "aurora_mysql", "in_scope": True},
    ]
    revised = [
        {"query_id": "q1", "assigned_engine": "aurora_mysql", "in_scope": True},
        {"query_id": "q2", "assigned_engine": "aurora_mysql", "in_scope": True},
    ]
    result = {
        "revised_assignments": revised,
        "move_ledger": recorded_ledger(original, revised),
        "consolidations": [
            {
                "from_engine": "opensearch",
                "to_engine": "aurora_mysql",
                "query_count": 1,
                "reason": "test consolidation",
                "saved_cost_estimate": 1.0,
                "action": "full",
                "queries_retained": [],
                "retention_reason": None,
            }
        ],
        "architectural_patterns": [],
        "recommendations": [],
        "unique_value_assessment": {},
        "executive_summary": None,
        "before_distribution": {"opensearch": 1, "aurora_mysql": 1},
        "after_distribution": {"aurora_mysql": 2},
        "collector_output": {},
        "analysis_outputs": {},
        # The version the revision spreads its fields from.
        "assignment": {
            "contract_version": current_version(Assignment),
            "query_assignments": original,
        },
    }
    store = LocalArtifactStore(base_dir=str(tmp_path))
    with bumped(Assignment) as version:
        new_version = write_reality_check_result(store, "job-1", "mydb", result, 1)
        assert new_version is not None
        written = _stored(tmp_path, f"mydb/job-1/assignment/v{new_version}/assignment.json")
    assert written["contract_version"] == version


@pytest.mark.usefixtures("table_confidence_scores")
@pytest.mark.usefixtures("schema_contract_off")
def test_the_post_schema_reroute_writes_the_current_version(tmp_path: Path) -> None:
    from tests.unit.orchestrator import test_post_schema_move_ledger as reroute

    store = LocalArtifactStore(base_dir=str(tmp_path))
    reroute._seed(store)
    key = f"{reroute.PREFIX}/assignment/v1/assignment.json"
    store.write_json(key, {**store.read_json(key), "contract_version": current_version(Assignment)})
    with bumped(Assignment) as version:
        reroute.CASE._run(store, [])
        written = _stored(tmp_path, f"{reroute.PREFIX}/assignment/v2/assignment.json")
    assert written["source"] == "post_schema_routing"
    assert written["contract_version"] == version


@pytest.mark.usefixtures("table_confidence_scores")
@pytest.mark.usefixtures("schema_contract_off")
def test_the_reroute_rollback_writes_the_current_version(tmp_path: Path) -> None:
    from tests.unit.orchestrator import test_post_schema_move_ledger as reroute

    store = LocalArtifactStore(base_dir=str(tmp_path))
    reroute._seed(store)
    with bumped(Assignment) as version:
        reroute._rollback_run(store)
        written = _stored(tmp_path, f"{reroute.PREFIX}/assignment/v3/assignment.json")
    assert written["status"] == "reroute_rolled_back"
    assert written["contract_version"] == version


# --- progression, answers --------------------------------------------------------


def test_the_assignment_review_mark_writes_the_current_version(tmp_path: Path) -> None:
    from src.shared.job_status import mark_assignment_reviewed

    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json("db/job-1/collector/output.json", {"queries": {"query_patterns": []}})
    meta_key = ".meta/job-1.json"
    store.write_json(
        meta_key,
        {
            "contract_version": current_version(PhaseProgression),
            "job_id": "job-1",
            "current_phase": "assignment",
            "phases": {},
        },
    )
    with bumped(PhaseProgression) as version:
        mark_assignment_reviewed(store, "db", "job-1", "synthesis")
        written = _stored(tmp_path, meta_key)
    assert written["phases"]["assignment_review"]["status"] == "completed"
    assert written["contract_version"] == version


def test_the_stored_input_redaction_writes_the_current_version(tmp_path: Path) -> None:
    from src.api.stored_input_redaction import redact_stored_inputs
    from tests.unit.api.test_submitted_secrets import URI

    store = LocalArtifactStore(base_dir=str(tmp_path))
    key = "db/job-1/collector/answers.json"
    store.write_json(
        key,
        {
            "contract_version": current_version(AgentAnswers),
            "answers": {"q1": URI},
            "agent_type": "collector",
        },
    )
    with bumped(AgentAnswers) as version:
        assert redact_stored_inputs(tmp_path).rewritten == [key]
        written = _stored(tmp_path, key)
    assert written["answers"] == {"q1": "<redacted>"}
    assert written["contract_version"] == version


def test_the_stored_input_redaction_leaves_a_clean_file_alone(tmp_path: Path) -> None:
    """Only a rewrite stamps: a file with nothing to redact is not written at all."""
    from src.api.stored_input_redaction import redact_stored_inputs

    store = LocalArtifactStore(base_dir=str(tmp_path))
    key = "db/job-1/collector/answers.json"
    stored = {"contract_version": current_version(AgentAnswers), "answers": {"q1": "nightly"}}
    store.write_json(key, stored)
    with bumped(AgentAnswers):
        assert redact_stored_inputs(tmp_path).rewritten == []
    assert _stored(tmp_path, key) == stored


# --- schema outputs --------------------------------------------------------------


def _ddb_schema_key(version: int = 1) -> str:
    return f"mydb/job-1/schema-dynamodb/v{version}/schema_output.json"


@pytest.mark.usefixtures("schema_contract_off")
def test_the_dynamodb_finalize_scope_rewrite_writes_the_current_version(
    tmp_path: Path,
) -> None:
    from scripts import run_schema_design as script
    from src.agents.schema_design.handler import ScopeReport

    store = LocalArtifactStore(base_dir=str(tmp_path))
    from tests.unit.agents.schema_design.valid_designs import ddb_design, ddb_pattern, ddb_table

    design = ddb_design([ddb_table("T", "mydb.t")], [ddb_pattern("DDB-AP-1", "T", "mydb.t", "q")])
    stored = {**design, "contract_version": current_version(DynamoDBModelOutputContract)}
    store.write_json(_ddb_schema_key(), stored)

    def flagged(store, db, job_id, engine, output, version):
        return {**output, "validation_passed": False}, ScopeReport([], [])

    with (
        bumped(DynamoDBModelOutputContract) as version,
        patch("src.agents.schema_design.handler.apply_schema_scope", flagged),
    ):
        script.run_finalize(store, "job-1", "mydb", "dynamodb", 1)
        written = _stored(tmp_path, _ddb_schema_key())
    assert written["validation_passed"] is False
    assert written["contract_version"] == version


@pytest.mark.usefixtures("schema_contract_off")
def test_the_transform_copy_forward_writes_the_current_version(tmp_path: Path) -> None:
    from src.atx_orchestrator.tools import _copy_schema_forward

    store = LocalArtifactStore(base_dir=str(tmp_path))
    stored = {
        "contract_version": current_version(DynamoDBModelOutputContract),
        "assignment_version": 1,
    }
    store.write_json(_ddb_schema_key(1), stored)
    with bumped(DynamoDBModelOutputContract) as version:
        assert _copy_schema_forward(store, "mydb", "job-1", "dynamodb", 1, 2)
        written = _stored(tmp_path, _ddb_schema_key(2))
    assert written["assignment_version"] == 2
    assert written["contract_version"] == version


# --- uploaded collector outputs --------------------------------------------------


def _collector_upload() -> dict:
    return {
        "contract_version": current_version(CollectorOutputContract),
        "job_id": "job-up",
        "database_schema": {"tables": []},
        "queries": {"query_patterns": []},
    }


def test_ingest_writes_an_uploaded_collector_output_at_the_current_version() -> None:
    from src.atx_orchestrator import core

    written: dict[str, dict] = {}

    class _Store:
        def write_json(self, path: str, data: dict) -> None:
            written[path] = data

    upload = _collector_upload()
    with bumped(CollectorOutputContract) as version:
        core.ingest_offline_collection(_Store(), "job-1", "testdb", upload)
    assert written["testdb/job-1/collector/output.json"]["contract_version"] == version


@pytest.mark.parametrize("script", ["run_collect", "run_assessment"])
def test_the_collect_scripts_write_an_uploaded_collector_output_at_the_current_version(
    script: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from scripts import run_assessment, run_collect

    upload = tmp_path / "upload.json"
    upload.write_text(json.dumps(_collector_upload()))
    artifacts = tmp_path / "artifacts"
    with bumped(CollectorOutputContract) as version:
        if script == "run_collect":
            argv = ["run_collect.py", "--file", str(upload), "--db", "testdb"]
            argv += ["--artifact-root", str(artifacts)]
            monkeypatch.setattr(sys, "argv", argv)
            run_collect.main()
        else:
            run_assessment.phase_collect(str(upload), "testdb", LocalArtifactStore(str(artifacts)))
        (output,) = artifacts.glob("testdb/*/collector/output.json")
        written = json.loads(output.read_text())
    assert written["contract_version"] == version
