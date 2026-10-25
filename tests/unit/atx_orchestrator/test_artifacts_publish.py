"""Tests for runtime.artifacts.publish — the EXTERNAL-visibility upload path.

Pins the artifact-visibility fix: CUSTOMER_OUTPUT deliverables are published with
``visibility="EXTERNAL"`` (so cross-account viewers can see and download them) and
``fileMetadata.path`` set (so the download saves under a friendly filename, not the
artifact UUID). The SDK's ``ArtifactStore.upload_artifact`` hardcodes INTERNAL and
sets no path, so ``publish`` uses the client-direct sequence instead.
"""

from __future__ import annotations

import importlib.abc
import logging
import sys
import types

import pytest

from src.atx_orchestrator.runtime import artifacts


class _FakeClient:
    """Records the client-direct upload sequence; one create call per item."""

    def __init__(self, fail_labels: set[str] | None = None) -> None:
        self.create_calls: list[dict] = []
        self.completed: list[str] = []
        self.copied: list[str] = []
        self._fail_labels = fail_labels or set()
        self._n = 0

    def create_artifact_upload_url(self, **kwargs):
        self.create_calls.append(kwargs)
        if kwargs.get("label") in self._fail_labels:
            raise RuntimeError("create failed")
        self._n += 1
        return {
            "artifactId": f"art-{self._n}",
            "s3preSignedUrl": "https://example/upload",
            "requestHeaders": {},
        }

    def get_artifact_metadata(self, **kwargs):
        return {"artifact": {"storedInAtxBucket": True}}

    def complete_artifact_upload(self, *, artifactId, requestContext):
        self.completed.append(artifactId)

    def copy_artifact(self, *, artifactId, idempotencyToken, requestContext):
        # copy_artifact is the actual INTERNAL->EXTERNAL visibility switch.
        self.copied.append(artifactId)


def _inject_sdk(monkeypatch, client, *, have_runtime=True):
    """Inject the agent_builder_sdk modules publish() imports.

    have_runtime=False makes get_agent_context_from_env raise, simulating the
    not-in-ATX-runtime path (publish should no-op and return {}).
    """
    pkg = types.ModuleType("agent_builder_sdk")
    framework = types.ModuleType("agent_builder_sdk.agentic_framework")
    cf = types.ModuleType("agent_builder_sdk.agentic_framework.client_factory")
    common = types.ModuleType("agent_builder_sdk.agentic_framework.common")
    env = types.ModuleType("agent_builder_sdk.env_var")

    cf.get_agentic_api_client = lambda: client  # type: ignore[attr-defined]
    common.calculate_digest = lambda content: "digest"  # type: ignore[attr-defined]
    uploaded: list[bytes] = []
    common.upload_from_presigned_url = (  # type: ignore[attr-defined]
        lambda resp, content, managed=True: uploaded.append(content)
    )

    if have_runtime:
        ctx = types.SimpleNamespace(
            workspace_id="ws",
            job_id="job",
            agent_instance_id="inst",
            to_dict=lambda: {"jobId": "job", "workspaceId": "ws"},
        )
        env.get_agent_context_from_env = lambda: ctx  # type: ignore[attr-defined]
    else:

        def _raise():
            raise RuntimeError("no ATX env")

        env.get_agent_context_from_env = _raise  # type: ignore[attr-defined]

    for name, mod in {
        "agent_builder_sdk": pkg,
        "agent_builder_sdk.agentic_framework": framework,
        "agent_builder_sdk.agentic_framework.client_factory": cf,
        "agent_builder_sdk.agentic_framework.common": common,
        "agent_builder_sdk.env_var": env,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    return uploaded


class TestPublish:
    def test_uploads_external_customer_output_with_path(self, monkeypatch) -> None:
        client = _FakeClient()
        uploaded = _inject_sdk(monkeypatch, client)

        result = artifacts.publish(
            [(b"<html>", "HTML", "Sample Report", "CUSTOMER_OUTPUT", "sample-report-db.html")]
        )

        assert result == {"Sample Report": "art-1"}
        assert client.completed == ["art-1"]
        # copy_artifact is what actually makes it EXTERNAL — must run after complete.
        assert client.copied == ["art-1"]
        assert uploaded == [b"<html>"]

        call = client.create_calls[0]
        assert call["artifactReference"]["artifactType"]["categoryType"] == "CUSTOMER_OUTPUT"
        assert call["artifactReference"]["artifactType"]["fileType"] == "HTML"
        assert call["fileMetadata"] == {"path": "sample-report-db.html"}
        assert call["label"] == "Sample Report"

    def test_default_path_derived_from_label_and_type(self, monkeypatch) -> None:
        client = _FakeClient()
        _inject_sdk(monkeypatch, client)

        # 4-tuple (no explicit path) -> path derived from label + file type.
        artifacts.publish([(b"{}", "JSON", "Assessment Data", "CUSTOMER_OUTPUT")])

        assert client.create_calls[0]["fileMetadata"] == {"path": "assessment-data.json"}

    def test_one_failure_does_not_lose_others(self, monkeypatch) -> None:
        client = _FakeClient(fail_labels={"Bad"})
        _inject_sdk(monkeypatch, client)

        result = artifacts.publish(
            [
                (b"a", "HTML", "Good1", "CUSTOMER_OUTPUT"),
                (b"b", "JSON", "Bad", "CUSTOMER_OUTPUT"),
                (b"c", "MARKDOWN", "Good2", "CUSTOMER_OUTPUT"),
            ]
        )

        assert set(result) == {"Good1", "Good2"}  # the failing one is dropped
        assert "Bad" not in result

    def test_fail_open_outside_runtime(self, monkeypatch) -> None:
        client = _FakeClient()
        _inject_sdk(monkeypatch, client, have_runtime=False)

        result = artifacts.publish([(b"x", "HTML", "Sample Report", "CUSTOMER_OUTPUT")])

        assert result == {}  # no context -> no-op, never raises
        assert client.create_calls == []

    @pytest.mark.passes_on_main  # guards that the decision keeps this WARNING
    def test_no_agent_context_is_still_a_warning(self, monkeypatch, caplog) -> None:
        _inject_sdk(monkeypatch, _FakeClient(), have_runtime=False)
        with caplog.at_level(logging.INFO, logger=artifacts.logger.name):
            artifacts.publish([(b"x", "HTML", "Sample Report", "CUSTOMER_OUTPUT")])
        assert [r.levelno for r in caplog.records] == [logging.WARNING]


class _SdkAbsent(importlib.abc.MetaPathFinder):
    """Finds no ``agent_builder_sdk``, as when the package is not installed."""

    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] == "agent_builder_sdk":
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None


class TestPublishWithoutTheSdk:
    """The SDK not installed means not the ATX runtime (a local run, a library call):
    INFO, not WARNING. Any other import failure is a real publishing problem."""

    def _publish(self, caplog) -> tuple[dict, list[logging.LogRecord]]:
        with caplog.at_level(logging.INFO, logger=artifacts.logger.name):
            result = artifacts.publish([(b"x", "HTML", "Sample Report", "CUSTOMER_OUTPUT")])
        return result, [r for r in caplog.records if r.name == artifacts.logger.name]

    def test_a_missing_sdk_is_logged_at_info(self, monkeypatch, caplog) -> None:
        # The import fails as for an uninstalled package (ModuleNotFoundError, name
        # "agent_builder_sdk"), even where the SDK is installed and imported already.
        for name in [m for m in sys.modules if m.split(".")[0] == "agent_builder_sdk"]:
            monkeypatch.delitem(sys.modules, name)
        monkeypatch.setattr(sys, "meta_path", [_SdkAbsent(), *sys.meta_path])
        result, records = self._publish(caplog)
        assert result == {}
        assert [r.levelno for r in records] == [logging.INFO]
        assert "skipped outside the ATX runtime" in records[0].getMessage()

    @pytest.mark.passes_on_main  # guards that the decision keeps this WARNING
    def test_another_import_failure_stays_a_warning(self, monkeypatch, caplog) -> None:
        _inject_sdk(monkeypatch, _FakeClient())
        monkeypatch.setitem(sys.modules, "agent_builder_types", None)
        result, records = self._publish(caplog)
        assert result == {}
        assert [r.levelno for r in records] == [logging.WARNING]
        assert "agent_builder_types" in records[0].getMessage()

    @pytest.mark.passes_on_main  # guards that an incompatible SDK keeps this WARNING
    def test_an_installed_but_incompatible_sdk_stays_a_warning(self, monkeypatch, caplog) -> None:
        """The SDK is there, but a name publish() imports is not (another SDK version):
        a real failure inside the runtime, not "outside the runtime"."""
        _inject_sdk(monkeypatch, _FakeClient())
        common = sys.modules["agent_builder_sdk.agentic_framework.common"]
        monkeypatch.delattr(common, "calculate_digest")
        result, records = self._publish(caplog)
        assert result == {}
        assert [r.levelno for r in records] == [logging.WARNING]
        assert "calculate_digest" in records[0].getMessage()

    def test_only_the_missing_sdk_package_counts_as_missing(self) -> None:
        assert artifacts._is_sdk_missing(ModuleNotFoundError(name="agent_builder_sdk"))
        # An installed SDK with a missing submodule or name is incompatible, not absent.
        assert not artifacts._is_sdk_missing(ModuleNotFoundError(name="agent_builder_sdk.env_var"))
        assert not artifacts._is_sdk_missing(
            ImportError(name="agent_builder_sdk.agentic_framework.common")
        )
        assert not artifacts._is_sdk_missing(ImportError(name="agent_builder_sdk"))
        assert not artifacts._is_sdk_missing(ModuleNotFoundError(name="agent_builder_types"))
        assert not artifacts._is_sdk_missing(ModuleNotFoundError(name="agent_builder_sdk_other"))
        assert not artifacts._is_sdk_missing(ImportError("no name"))


class TestDefaultPath:
    def test_slugs_label_and_appends_extension(self) -> None:
        assert artifacts._default_path("Sample Report", "HTML") == "sample-report.html"
        assert artifacts._default_path("Assessment Data", "JSON") == "assessment-data.json"
        assert artifacts._default_path("Engineering Report", "MARKDOWN") == "engineering-report.md"

    def test_unknown_type_falls_back_to_dat(self) -> None:
        assert artifacts._default_path("Thing", "OTHER") == "thing.dat"
