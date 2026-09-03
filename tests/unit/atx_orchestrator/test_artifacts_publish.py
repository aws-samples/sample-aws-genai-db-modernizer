"""Tests for runtime.artifacts.publish — the EXTERNAL-visibility upload path.

Pins the artifact-visibility fix: CUSTOMER_OUTPUT deliverables are published with
``visibility="EXTERNAL"`` (so cross-account viewers can see and download them) and
``fileMetadata.path`` set (so the download saves under a friendly filename, not the
artifact UUID). The SDK's ``ArtifactStore.upload_artifact`` hardcodes INTERNAL and
sets no path, so ``publish`` uses the client-direct sequence instead.
"""

from __future__ import annotations

import sys
import types

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
            [(b"<html>", "HTML", "Decision Report", "CUSTOMER_OUTPUT", "decision-report-db.html")]
        )

        assert result == {"Decision Report": "art-1"}
        assert client.completed == ["art-1"]
        # copy_artifact is what actually makes it EXTERNAL — must run after complete.
        assert client.copied == ["art-1"]
        assert uploaded == [b"<html>"]

        call = client.create_calls[0]
        assert call["artifactReference"]["artifactType"]["categoryType"] == "CUSTOMER_OUTPUT"
        assert call["artifactReference"]["artifactType"]["fileType"] == "HTML"
        assert call["fileMetadata"] == {"path": "decision-report-db.html"}
        assert call["label"] == "Decision Report"

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

        result = artifacts.publish([(b"x", "HTML", "Decision Report", "CUSTOMER_OUTPUT")])

        assert result == {}  # no context -> no-op, never raises
        assert client.create_calls == []


class TestDefaultPath:
    def test_slugs_label_and_appends_extension(self) -> None:
        assert artifacts._default_path("Decision Report", "HTML") == "decision-report.html"
        assert artifacts._default_path("Assessment Data", "JSON") == "assessment-data.json"
        assert artifacts._default_path("Engineering Report", "MARKDOWN") == "engineering-report.md"

    def test_unknown_type_falls_back_to_dat(self) -> None:
        assert artifacts._default_path("Thing", "OTHER") == "thing.dat"
