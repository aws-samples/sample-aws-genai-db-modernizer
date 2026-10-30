# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The skipped placeholders are contracts of their own (#1023).

Three writers stored a placeholder at a contract file path that was not that
contract and carried no ``contract_version``: the schema design placeholder for
an engine with nothing assigned (``schema_design/handler.py``), the merge's
placeholder when every query touches no source table (``group_merger.py``), and
the empty synthesis report (``synthesis_handler.py``). Each is now a
``status: "skipped"`` variant model with a ``contract_version``, written through
the model. The contract file registry picks the variant for a payload whose
``status`` is ``"skipped"``, so a reader checks the placeholder's version, not the
design's; a placeholder saved before (no version) still loads.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import BaseModel

from src.contracts.dynamodb_model_output import DynamoDBModelOutputContract
from src.contracts.skipped_placeholders import SkippedSchemaOutput, SkippedSynthesisReport
from src.contracts.synthesis_output import SynthesisOutputContract
from src.shared.contract_versioning import (
    ContractVersionError,
    contract_for_payload,
    current_version,
    load_contract,
    stamp_artifact_version,
)
from src.storage.local_store import LocalArtifactStore

DB, JOB = "shop", "job-1"
SCHEMA_KEY = f"{DB}/{JOB}/schema-dynamodb/v1/schema_output.json"
REPORT_KEY = f"{DB}/{JOB}/synthesis/v1/report.json"
LEGACY_REPORT_KEY = f"{DB}/{JOB}/referee-synthesis/report.json"


def _stored(tmp_path: Path, key: str) -> dict:
    data: dict = json.loads((tmp_path / key).read_text())
    return data


def _write_raw(tmp_path: Path, key: str, data: dict) -> None:
    path = tmp_path / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@contextmanager
def bumped_major(model: type[BaseModel], version: str) -> Iterator[None]:
    field = model.model_fields["contract_version"]
    original = field.default
    field.default = version
    model.model_rebuild(force=True)
    try:
        yield
    finally:
        field.default = original
        model.model_rebuild(force=True)


# --- the writers ---------------------------------------------------------------


def test_the_schema_design_placeholder_is_written_through_its_model(tmp_path: Path) -> None:
    from src.agents.schema_design.handler import run_schema_design

    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "database_schema": {"tables": [{"table_id": "s.orders", "table_name": "orders"}]},
            "queries": {"query_patterns": []},
        },
    )
    store.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json",
        {"version": 1, "query_assignments": [], "table_assignments": []},
    )
    with patch(
        "src.agents.schema_design.handler._dispatch_schema_agent",
        side_effect=AssertionError("no designer runs for an engine with nothing assigned"),
    ):
        run_schema_design(JOB, DB, "dynamodb", store, assignment_version=1, llm_mode="bedrock")

    written = _stored(tmp_path, SCHEMA_KEY)
    assert written == {
        "contract_version": current_version(SkippedSchemaOutput),
        "target_type": "dynamodb",
        "status": "skipped",
        "reason": "No queries or tables assigned to this engine",
        "assignment_version": 1,
    }


def test_the_merge_placeholder_is_written_through_its_model(tmp_path: Path) -> None:
    from src.agents.schema_design.group_merger import merge_schema_groups

    store = LocalArtifactStore(base_dir=str(tmp_path))
    excluded = [{"query_id": "q-1", "reason": "not designed: no source table"}]
    store.write_json(
        f"{DB}/{JOB}/schema-dynamodb/v1/groups_manifest.json",
        {
            "job_id": JOB,
            "database_name": DB,
            "target_engine": "dynamodb",
            "total_queries": 1,
            "total_groups": 0,
            "groups": [],
            "excluded_queries": excluded,
        },
    )
    merged = merge_schema_groups(JOB, DB, "dynamodb", store)

    written = _stored(tmp_path, SCHEMA_KEY)
    assert written == merged
    assert written == {
        "contract_version": current_version(SkippedSchemaOutput),
        "target_type": "dynamodb",
        "status": "skipped",
        "reason": "every assigned query touches no source table",
        "excluded_queries": excluded,
    }


def test_the_empty_synthesis_report_is_written_through_its_model(tmp_path: Path) -> None:
    from src.agents.referee.synthesis_handler import run_synthesis

    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(
        f"{DB}/{JOB}/referee-triage/triage.json", {"selected_agents": [], "signals": []}
    )
    store.write_json(f"{DB}/{JOB}/collector/output.json", {"queries": {"query_patterns": []}})
    run_synthesis(JOB, DB, store, assignment_version=1, llm_mode="none")

    written = _stored(tmp_path, REPORT_KEY)
    assert written["contract_version"] == current_version(SkippedSynthesisReport)
    assert written["status"] == "skipped"
    assert written["ranking"] == []
    assert written["summary"] == "No analysis outputs were available for synthesis."
    SkippedSynthesisReport.model_validate(written)


# --- the registry: status first --------------------------------------------------


@pytest.mark.parametrize(
    ("key", "data", "model"),
    [
        (SCHEMA_KEY, {"status": "skipped"}, SkippedSchemaOutput),
        (SCHEMA_KEY, {"status": "completed"}, DynamoDBModelOutputContract),
        (SCHEMA_KEY, {}, DynamoDBModelOutputContract),
        (REPORT_KEY, {"status": "skipped"}, SkippedSynthesisReport),
        (LEGACY_REPORT_KEY, {"status": "skipped"}, SkippedSynthesisReport),
        (REPORT_KEY, {"status": "completed"}, SynthesisOutputContract),
        (f"{DB}/{JOB}/collector/output.json", {"status": "skipped"}, None),
    ],
)
def test_the_registry_reads_the_status_first(key: str, data: dict, model: object) -> None:
    found = contract_for_payload(key, data)
    if model is None:
        assert found is not SkippedSchemaOutput and found is not SkippedSynthesisReport
    else:
        assert found is model


def test_a_placeholder_is_checked_against_its_own_version(tmp_path: Path) -> None:
    """A placeholder at 1.x reads even when the engine's design contract is at 2.x."""
    placeholder = SkippedSchemaOutput(target_type="dynamodb", reason="x").model_dump(mode="json")
    _write_raw(tmp_path, SCHEMA_KEY, placeholder)
    with bumped_major(DynamoDBModelOutputContract, "2.0"):
        assert LocalArtifactStore(base_dir=str(tmp_path)).read_json(SCHEMA_KEY) == placeholder


def test_a_placeholder_at_another_major_is_refused(tmp_path: Path) -> None:
    placeholder = {
        **SkippedSchemaOutput(target_type="dynamodb", reason="x").model_dump(mode="json"),
        "contract_version": "9.0",
    }
    _write_raw(tmp_path, SCHEMA_KEY, placeholder)
    with pytest.raises(ContractVersionError, match="SkippedSchemaOutput"):
        LocalArtifactStore(base_dir=str(tmp_path)).read_json(SCHEMA_KEY)


def test_a_stamp_of_a_placeholder_says_the_placeholders_version() -> None:
    with bumped_major(DynamoDBModelOutputContract, "2.0"):
        stamped = stamp_artifact_version(SCHEMA_KEY, {"status": "skipped", "reason": "x"})
    assert stamped["contract_version"] == current_version(SkippedSchemaOutput)


# --- placeholders saved before #1023 ---------------------------------------------


def test_a_schema_placeholder_saved_before_still_loads(tmp_path: Path) -> None:
    legacy = {
        "target_type": "dynamodb",
        "status": "skipped",
        "reason": "No queries or tables assigned to this engine",
        "assignment_version": 2,
    }
    _write_raw(tmp_path, SCHEMA_KEY, legacy)
    read = LocalArtifactStore(base_dir=str(tmp_path)).read_json(SCHEMA_KEY)
    assert read == legacy
    loaded = load_contract(SkippedSchemaOutput, read, artifact=SCHEMA_KEY)
    assert loaded.model_dump(mode="json") == {
        "contract_version": current_version(SkippedSchemaOutput),
        **legacy,
    }


def test_an_empty_report_saved_before_still_loads(tmp_path: Path) -> None:
    """Before #1023 the empty report said ``status: completed`` and had no version."""
    legacy = {
        "job_id": JOB,
        "database_name": DB,
        "agent_type": "referee-synthesis",
        "status": "completed",
        "ranking": [],
        "needs_deeper_analysis": False,
        "summary": "No analysis outputs were available for synthesis.",
        "timestamp": "2026-10-01T00:00:00+00:00",
    }
    _write_raw(tmp_path, LEGACY_REPORT_KEY, legacy)
    assert LocalArtifactStore(base_dir=str(tmp_path)).read_json(LEGACY_REPORT_KEY) == legacy


def test_a_placeholder_for_an_engine_with_no_design_contract_is_checked(tmp_path: Path) -> None:
    """The handler writes a placeholder for any engine; its version is checked even
    where the engine has no design contract of its own."""
    key = f"{DB}/{JOB}/schema-neptune/v1/schema_output.json"
    assert contract_for_payload(key, {}) is None
    assert contract_for_payload(key, {"status": "skipped"}) is SkippedSchemaOutput
    _write_raw(tmp_path, key, {"contract_version": "9.0", "status": "skipped", "reason": "x"})
    with pytest.raises(ContractVersionError, match="SkippedSchemaOutput"):
        LocalArtifactStore(base_dir=str(tmp_path)).read_json(key)


def test_a_design_at_another_major_that_says_skipped_is_not_taken_for_a_placeholder(
    tmp_path: Path,
) -> None:
    """The status is read first, but the version still decides: a design file saying
    ``skipped`` at a MAJOR the placeholder does not have is refused, not read."""
    design = {"contract_version": "2.0", "status": "skipped", "reason": "x", "tables": []}
    _write_raw(tmp_path, SCHEMA_KEY, design)
    with pytest.raises(ContractVersionError, match="SkippedSchemaOutput"):
        LocalArtifactStore(base_dir=str(tmp_path)).read_json(SCHEMA_KEY)
