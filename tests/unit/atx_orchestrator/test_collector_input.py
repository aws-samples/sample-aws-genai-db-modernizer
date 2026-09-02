"""Tests for collector-input resolution and WebApp-upload discovery.

Two behaviours are pinned here:

* ``_resolve_collector_input`` (collector core) chooses between an explicit key
  and the seed, and never does upload discovery itself -- so it never depends on
  the collector subagent's own context matching the customer's Transform job.
* ``_discover_uploaded_input`` (called by the orchestrator) finds the single
  customer-uploaded collection under the job's ``User Uploads/`` prefix, keyed by
  the platform job UUID from the agent context, excluding the auto-written
  ``job_objective``.

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
        self._keys = set(keys)

    def exists(self, path: str) -> bool:
        return path in self._keys

    def list_prefix(self, prefix: str) -> list[str]:
        return sorted(k for k in self._keys if k.startswith(prefix))


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


def _inject_ctx(monkeypatch: pytest.MonkeyPatch, workspace_id: str, job_id: str) -> None:
    fake_mod = types.ModuleType("agent_builder_sdk.env_var")
    fake_mod.get_agent_context_from_env = lambda: types.SimpleNamespace(  # type: ignore[attr-defined]
        workspace_id=workspace_id, job_id=job_id
    )
    monkeypatch.setitem(sys.modules, "agent_builder_sdk", types.ModuleType("agent_builder_sdk"))
    monkeypatch.setitem(sys.modules, "agent_builder_sdk.env_var", fake_mod)


class TestDiscoverUploadedInput:
    PREFIX = "AWSTransform/Workspaces/ws1/Jobs/uuid1/User Uploads/"

    def test_no_agent_context_returns_none(self) -> None:
        # No injection: the SDK import fails or has no env context -> None.
        assert core._discover_uploaded_input(_FakeStore(())) is None

    def test_single_upload_found(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _inject_ctx(monkeypatch, "ws1", "uuid1")
        store = _FakeStore(
            (self.PREFIX + "discourse-collection.json", self.PREFIX + "job_objective")
        )
        assert core._discover_uploaded_input(store) == self.PREFIX + "discourse-collection.json"

    def test_job_objective_excluded(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _inject_ctx(monkeypatch, "ws1", "uuid1")
        # only job_objective present -> nothing to pick
        store = _FakeStore((self.PREFIX + "job_objective",))
        assert core._discover_uploaded_input(store) is None

    def test_ambiguous_upload_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _inject_ctx(monkeypatch, "ws1", "uuid1")
        store = _FakeStore((self.PREFIX + "a.json", self.PREFIX + "b.json"))
        with pytest.raises(ValueError, match="exactly one"):
            core._discover_uploaded_input(store)

    def test_empty_prefix_returns_none_and_lists(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """With a valid context but nothing uploaded, discovery must list the job's
        prefix and return None (empty input_key). Regression guard for the silent
        empty-input_key root cause: the listing must actually be attempted."""
        _inject_ctx(monkeypatch, "ws1", "uuid1")

        listed: list[str] = []

        class _RecordingStore(_FakeStore):
            def list_prefix(self, prefix: str) -> list[str]:
                listed.append(prefix)
                return super().list_prefix(prefix)

        assert core._discover_uploaded_input(_RecordingStore(())) is None
        assert listed == [self.PREFIX]


# =============================================================================
# orchestrator wiring: run_assessment_core_via_a2a discovers + passes the key


class TestOrchestratorPassesDiscoveredKey:
    def test_discovered_key_passed_as_input_key(self) -> None:
        key = "AWSTransform/Workspaces/ws1/Jobs/uuid1/User Uploads/coll.json"
        with (
            patch("src.atx_orchestrator.tools.invoke_and_wait", return_value={"ok": 1}) as m,
            patch("src.atx_orchestrator.tools._make_store", return_value=_FakeStore()),
            patch("src.atx_orchestrator.core._discover_uploaded_input", return_value=key),
        ):
            run_assessment_core_via_a2a(job_id="job", database_name="db")
        message = json.loads(m.call_args[0][1])
        assert message["input_key"] == key

    def test_no_upload_leaves_key_empty_for_seed_fallback(self) -> None:
        with (
            patch("src.atx_orchestrator.tools.invoke_and_wait", return_value={"ok": 1}) as m,
            patch("src.atx_orchestrator.tools._make_store", return_value=_FakeStore()),
            patch("src.atx_orchestrator.core._discover_uploaded_input", return_value=None),
        ):
            run_assessment_core_via_a2a(job_id="job", database_name="db")
        # empty input_key -> collect step falls back to the seed key
        assert json.loads(m.call_args[0][1])["input_key"] == ""
