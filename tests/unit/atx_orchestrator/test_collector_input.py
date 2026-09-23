"""Tests for collector-input resolution and WebApp-upload discovery.

Two behaviours are pinned here:

* ``_resolve_collector_input`` (collector core) chooses between an explicit key
  and the seed, and never does upload discovery itself -- so it never depends on
  the collector subagent's own context matching the customer's Transform job.
* ``_discover_uploaded_input`` (called by the orchestrator) finds the single
  customer-uploaded collection under the job's ``User Uploads/`` prefix, keyed by
  the platform job UUID from the agent context, excluding the auto-written
  ``job_objective`` and this pipeline's own ``STATE``-category writes, then
  downloads and stages it at the seed key for the collector to read.

The orchestrator wiring test confirms ``run_assessment_core_via_a2a``
discovers the upload and passes its key to the assessment-core agent as
``input_key`` (the agent's collect step then falls back to the seed when empty).
"""

from __future__ import annotations

import json
import sys
import types
from unittest.mock import patch

import pytest

from src.atx_orchestrator import core
from src.atx_orchestrator.tools import run_assessment_core_via_a2a


class _FakeStore:
    def __init__(self, keys: tuple[str, ...] = ()) -> None:
        self._objects: dict[str, dict] = {k: {} for k in keys}

    def exists(self, path: str) -> bool:
        return path in self._objects

    def list_prefix(self, prefix: str) -> list[str]:
        return sorted(k for k in self._objects if k.startswith(prefix))

    def write_json(self, path: str, data: dict) -> None:
        self._objects[path] = data

    def read_json(self, path: str) -> dict:
        return self._objects[path]


# =============================================================================
# _resolve_collector_input  (collector core: explicit key > seed > raise)


class TestResolveCollectorInput:
    def test_explicit_key_used_when_present(self) -> None:
        store = _FakeStore(("some/explicit/key.json",))
        assert (
            core._resolve_collector_input(store, "job", "db", "some/explicit/key.json")
            == "some/explicit/key.json"
        )

    def test_explicit_key_missing_raises(self) -> None:
        store = _FakeStore(())
        with pytest.raises(FileNotFoundError, match="some/explicit/key.json"):
            core._resolve_collector_input(store, "job", "db", "some/explicit/key.json")

    def test_falls_back_to_seed(self) -> None:
        seed = core.default_input_key("job", "db")
        store = _FakeStore((seed,))
        assert core._resolve_collector_input(store, "job", "db", "") == seed

    def test_no_input_no_seed_raises(self) -> None:
        store = _FakeStore(())
        with pytest.raises(FileNotFoundError, match="no seed exists"):
            core._resolve_collector_input(store, "job", "db", "")

    def test_does_not_discover_uploads(self) -> None:
        """The collector core must never reach upload discovery -- discovery is the
        orchestrator's job. With no key and no seed it raises, even if an upload
        would exist."""
        store = _FakeStore(())
        with patch.object(core, "_discover_uploaded_input") as disc:
            with pytest.raises(FileNotFoundError):
                core._resolve_collector_input(store, "job", "db", "")
            disc.assert_not_called()


# =============================================================================
# _discover_uploaded_input


class _FakeAgenticClient:
    """Stand-in for the elasticgumbyagenticservice client. Records the filter it
    was called with so tests can assert the workspace-scoped listing is used."""

    def __init__(self, artifacts: list[dict]) -> None:
        self._artifacts = artifacts
        self.list_calls: list[dict] = []

    def list_artifacts(self, **kwargs):  # noqa: ANN003
        self.list_calls.append(kwargs)
        return {"artifacts": self._artifacts}


class _FakeArtifactStore:
    """Stand-in for the SDK ArtifactStore. Exposes ``.client`` and
    ``._create_request_context`` (discovery lists via the client directly with a
    workspaceFilter), records the download, and serves a canned collection."""

    def __init__(self, artifacts: list[dict], content: dict | None = None) -> None:
        self.client = _FakeAgenticClient(artifacts)
        self._content = content if content is not None else {"collection_version": 1}
        self.downloaded: list[str] = []

    def _create_request_context(self) -> dict:
        return {"jobMetadata": {"jobId": "uuid1", "workspaceId": "ws1"}}

    def download_artifact(self, artifact_id: str, destination_file_path: str) -> None:
        self.downloaded.append(artifact_id)
        with open(destination_file_path, "w") as fh:
            json.dump(self._content, fh)


def _artifact(
    artifact_id: str,
    label: str,
    file_type: str = "JSON",
    path: str | None = None,
    category: str = "CUSTOMER_INPUT",
) -> dict:
    a = {
        "artifactId": artifact_id,
        "artifactLabel": label,
        "artifactType": {"categoryType": category, "fileType": file_type},
    }
    if path is not None:
        a["fileMetadata"] = {"path": path}
    return a


def _inject_sdk(monkeypatch: pytest.MonkeyPatch, fake_store: _FakeArtifactStore | None) -> None:
    """Inject the SDK modules _discover_uploaded_input imports. When fake_store is
    None, get_agent_context_from_env raises → the not-in-ATX-runtime path."""
    pkg = types.ModuleType("agent_builder_sdk")
    framework = types.ModuleType("agent_builder_sdk.agentic_framework")

    env_mod = types.ModuleType("agent_builder_sdk.env_var")
    if fake_store is None:

        def _raise():  # noqa: ANN202
            raise RuntimeError("no ATX agent context")

        env_mod.get_agent_context_from_env = _raise  # type: ignore[attr-defined]
    else:
        env_mod.get_agent_context_from_env = lambda: types.SimpleNamespace(  # type: ignore[attr-defined]
            workspace_id="ws1", job_id="uuid1", agent_instance_id="inst1"
        )

    store_mod = types.ModuleType("agent_builder_sdk.agentic_framework.artifact_store")
    store_mod.ArtifactStore = lambda **kwargs: fake_store  # type: ignore[attr-defined]

    client_mod = types.ModuleType("agent_builder_sdk.agentic_framework.client_factory")
    client_mod.get_agentic_api_client = lambda: object()  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "agent_builder_sdk", pkg)
    monkeypatch.setitem(sys.modules, "agent_builder_sdk.agentic_framework", framework)
    monkeypatch.setitem(sys.modules, "agent_builder_sdk.env_var", env_mod)
    monkeypatch.setitem(
        sys.modules, "agent_builder_sdk.agentic_framework.artifact_store", store_mod
    )
    monkeypatch.setitem(
        sys.modules, "agent_builder_sdk.agentic_framework.client_factory", client_mod
    )


class TestDiscoverUploadedInput:
    def test_no_agent_context_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # SDK context resolution fails (not in ATX runtime) -> None, no staging.
        _inject_sdk(monkeypatch, None)
        assert core._discover_uploaded_input(_FakeStore(), "uuid1", "discourse") is None

    def test_single_upload_downloaded_and_staged(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeArtifactStore(
            [
                _artifact("art-1", "default", path="discourse-collection.json"),
                _artifact("obj-1", "default", path="job_objective"),
            ],
            content={"collection_version": 7},
        )
        _inject_sdk(monkeypatch, fake)
        store = _FakeStore()
        seed = core.default_input_key("uuid1", "discourse")

        result = core._discover_uploaded_input(store, "uuid1", "discourse")

        assert result == seed
        # It downloaded the collection artifact (not the job_objective)...
        assert fake.downloaded == ["art-1"]
        # ...and staged the content at the seed key for the collector to read.
        assert store.read_json(seed) == {"collection_version": 7}

    def test_lists_without_server_side_filter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Regression guard: server-side category/agent filters returned listed=0
        in the field even though the upload existed. Discovery must list WITHOUT an
        artifactFilter and match in Python on the artifact's real type/path."""
        fake = _FakeArtifactStore([_artifact("art-1", "default", path="coll.json")])
        _inject_sdk(monkeypatch, fake)

        core._discover_uploaded_input(_FakeStore(), "uuid1", "discourse")

        assert fake.client.list_calls, "list_artifacts was not called"
        assert "artifactFilter" not in fake.client.list_calls[0]

    def test_real_shape_two_json_objective_excluded_by_basename(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The field case: BOTH the upload and the auto-written objective are
        CUSTOMER_INPUT/JSON with label 'default'. The objective has a bare
        'job_objective' path (no .json, no leading slash). Only the real
        collection must be picked — the objective is excluded by path basename."""
        collection = _artifact("33641880", "default", path="discourse-collection.json")
        objective = _artifact("788c3a44", "default", path="job_objective")
        fake = _FakeArtifactStore([collection, objective], content={"collection_version": 5})
        _inject_sdk(monkeypatch, fake)
        store = _FakeStore()
        seed = core.default_input_key("uuid1", "discourse")

        result = core._discover_uploaded_input(store, "uuid1", "discourse")

        assert result == seed
        assert fake.downloaded == ["33641880"]  # the collection, not the objective
        assert store.read_json(seed) == {"collection_version": 5}

    def test_job_objective_only_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Only the auto-written objective present (bare 'job_objective' path).
        _inject_sdk(
            monkeypatch, _FakeArtifactStore([_artifact("obj-1", "default", path="job_objective")])
        )
        assert core._discover_uploaded_input(_FakeStore(), "uuid1", "discourse") is None

    def test_non_json_excluded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # A non-JSON upload (e.g. a ZIP) is not a collection candidate.
        _inject_sdk(
            monkeypatch,
            _FakeArtifactStore([_artifact("z-1", "default", file_type="ZIP", path="bundle.zip")]),
        )
        assert core._discover_uploaded_input(_FakeStore(), "uuid1", "discourse") is None

    def test_ambiguous_two_collections_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Two genuine .json collection uploads (neither is the objective) -> raise.
        _inject_sdk(
            monkeypatch,
            _FakeArtifactStore(
                [
                    _artifact("a-1", "default", path="coll-a.json"),
                    _artifact("b-1", "default", path="coll-b.json"),
                ]
            ),
        )
        with pytest.raises(ValueError, match="found 2"):
            core._discover_uploaded_input(_FakeStore(), "uuid1", "discourse")

    def test_no_artifacts_returns_none(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Valid context but nothing uploaded -> None (collector falls back to a
        pre-staged seed). Regression guard for the silent empty-input_key cause."""
        _inject_sdk(monkeypatch, _FakeArtifactStore([]))
        assert core._discover_uploaded_input(_FakeStore(), "uuid1", "discourse") is None

    def test_state_artifacts_excluded_no_false_ambiguity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Our own pipeline writes JSON artifacts under CategoryType.STATE into
        the same job (e.g. collector/output.json) on a retry/resume. Those must
        not be mistaken for the customer upload -- and must not create a false
        ambiguity with the real upload."""
        upload = _artifact("art-1", "default", path="user-upload.json")
        own_state_write = _artifact(
            "state-1",
            "default",
            path="discourse/uuid1/collector/output.json",
            category="STATE",
        )
        fake = _FakeArtifactStore([upload, own_state_write], content={"collection_version": 9})
        _inject_sdk(monkeypatch, fake)
        store = _FakeStore()
        seed = core.default_input_key("uuid1", "discourse")

        result = core._discover_uploaded_input(store, "uuid1", "discourse")

        assert result == seed
        # The customer upload was downloaded and staged, not the STATE artifact.
        assert fake.downloaded == ["art-1"]
        assert store.read_json(seed) == {"collection_version": 9}


# =============================================================================
# orchestrator wiring: run_assessment_core_via_a2a reads the upload-gate's key


class _FakeStoreWithText(_FakeStore):
    """_FakeStore plus write_text, so the upload-gate pointers can round-trip."""

    def write_text(self, path: str, content: str, content_type: str = "text/plain") -> None:
        self._objects[path] = json.loads(content)


class TestOrchestratorPassesResolvedKey:
    def test_recorded_key_passed_as_input_key(self) -> None:
        """The key recorded by finalize_collection_upload is passed to the collector."""
        from src.atx_orchestrator.tools import _record_resolved_input_key

        store = _FakeStoreWithText()
        _record_resolved_input_key(store, "db", "job", "artifact://abc-123")
        with (
            patch("src.atx_orchestrator.tools.invoke_and_wait", return_value={"ok": 1}) as m,
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
        ):
            run_assessment_core_via_a2a(job_id="job", database_name="db")
        message = json.loads(m.call_args[0][1])
        assert message["input_key"] == "artifact://abc-123"

    def test_no_upload_leaves_key_empty_for_seed_fallback(self) -> None:
        """With no recorded upload (dev/reference), input_key is empty -> seed fallback."""
        with (
            patch("src.atx_orchestrator.tools.invoke_and_wait", return_value={"ok": 1}) as m,
            patch("src.atx_orchestrator.tools._make_store", return_value=_FakeStoreWithText()),
        ):
            run_assessment_core_via_a2a(job_id="job", database_name="db")
        assert json.loads(m.call_args[0][1])["input_key"] == ""

    def test_does_not_call_discovery(self) -> None:
        """The assessment tool no longer performs upload discovery; the upload gate
        supplies the artifact id instead."""
        with (
            patch("src.atx_orchestrator.tools.invoke_and_wait", return_value={"ok": 1}),
            patch("src.atx_orchestrator.tools._make_store", return_value=_FakeStoreWithText()),
            patch("src.atx_orchestrator.core._discover_uploaded_input") as disc,
        ):
            run_assessment_core_via_a2a(job_id="job", database_name="db")
        disc.assert_not_called()
