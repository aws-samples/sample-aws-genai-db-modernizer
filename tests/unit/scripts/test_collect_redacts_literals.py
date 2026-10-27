"""Every collection ingest writes no literal value to disk (#1625).

A collector output handed in as the collection (or one saved before #1625) can hold
the literals of the text its collector read. ``run_assessment.py``, ``run_collect.py``,
the library ingest and the AWS Transform staging copy redact it before they write
anything, so no file under the job holds a seeded value.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from scripts import run_assessment, run_collect
from src.agents.collector.offline_ingest import build_offline_collector_output
from src.contracts.collector_output import CollectorOutputContract
from src.storage.local_store import LocalArtifactStore
from src.tools.database.offline_parser import parse_offline_collection
from tests.fixtures.pii_collections import ENGINES, keep_literals, leaked, pii_collection


def _uploaded_collector_output(engine: str, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """A collector output whose text still holds the seeded values (an older one, or
    one a caller built), at today's contract version."""
    with monkeypatch.context() as patch:
        keep_literals(patch)
        out = build_offline_collector_output(
            parse_offline_collection(pii_collection(engine)),
            job_id="old",
            database_name="trailgear",
            s3_key="x",
        )
    out["contract_version"] = CollectorOutputContract.model_fields["contract_version"].default
    assert leaked(json.dumps(out))
    return out


def _no_file_leaks(root: Path) -> None:
    files = [p for p in root.rglob("*") if p.is_file()]
    assert files
    for path in files:
        assert not leaked(path.read_text(errors="ignore")), path


@pytest.mark.parametrize("engine", ENGINES)
def test_run_assessment_writes_an_uploaded_collector_output_redacted(
    engine: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "collection.json"
    path.write_text(json.dumps(_uploaded_collector_output(engine, monkeypatch)))
    root = tmp_path / "artifacts"
    run_assessment.phase_collect(str(path), None, LocalArtifactStore(base_dir=str(root)))
    assert list(root.rglob("collector/output.json"))
    _no_file_leaks(root)


@pytest.mark.parametrize("engine", ENGINES)
def test_run_collect_writes_an_uploaded_collector_output_redacted(
    engine: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "collection.json"
    path.write_text(json.dumps(_uploaded_collector_output(engine, monkeypatch)))
    root = tmp_path / "artifacts"
    monkeypatch.setattr(
        sys, "argv", ["run_collect.py", "--file", str(path), "--artifact-root", str(root)]
    )
    run_collect.main()
    assert list(root.rglob("collector/output.json"))
    _no_file_leaks(root)


@pytest.mark.parametrize("engine", ENGINES)
def test_the_scripts_copy_of_a_raw_collection_is_redacted(engine: str, tmp_path: Path) -> None:
    path = tmp_path / "collection.json"
    path.write_text(json.dumps(pii_collection(engine)))
    root = tmp_path / "artifacts"
    run_assessment.phase_collect(str(path), None, LocalArtifactStore(base_dir=str(root)))
    assert list(root.rglob("uploads/collector-output.json"))
    _no_file_leaks(root)


class _Store:
    def __init__(self) -> None:
        self.objects: dict[str, Any] = {}

    def write_json(self, path: str, data: Any) -> None:
        self.objects[path] = data


class _Artifacts:
    def __init__(self, content: dict[str, Any]) -> None:
        self._content = content
        self.client = types.SimpleNamespace(
            list_artifacts=lambda **_: {
                "artifacts": [
                    {
                        "artifactId": "art-1",
                        "artifactLabel": "default",
                        "artifactType": {"categoryType": "CUSTOMER_INPUT", "fileType": "JSON"},
                        "fileMetadata": {"path": "collection.json"},
                    }
                ]
            }
        )

    def _create_request_context(self) -> dict:
        return {"jobMetadata": {"jobId": "uuid1", "workspaceId": "ws1"}}

    def download_artifact(self, _artifact_id: str, destination: str) -> None:
        Path(destination).write_text(json.dumps(self._content))


@pytest.mark.parametrize("kind", ["raw", "collector_output"])
def test_the_aws_transform_staging_copy_is_redacted(
    kind: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.atx_orchestrator import core

    content = (
        pii_collection("sqlserver")
        if kind == "raw"
        else _uploaded_collector_output("sqlserver", monkeypatch)
    )
    artifacts = _Artifacts(content)
    modules = {
        "agent_builder_sdk": types.ModuleType("agent_builder_sdk"),
        "agent_builder_sdk.agentic_framework": types.ModuleType("f"),
        "agent_builder_sdk.env_var": types.SimpleNamespace(
            get_agent_context_from_env=lambda: types.SimpleNamespace(
                workspace_id="ws1", job_id="uuid1", agent_instance_id="inst1"
            )
        ),
        "agent_builder_sdk.agentic_framework.artifact_store": types.SimpleNamespace(
            ArtifactStore=lambda **_: artifacts
        ),
        "agent_builder_sdk.agentic_framework.client_factory": types.SimpleNamespace(
            get_agentic_api_client=lambda: object()
        ),
    }
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    store = _Store()
    key = core._discover_uploaded_input(store, "uuid1", "trailgear")
    assert key is not None
    assert not leaked(json.dumps(store.objects[key]))
