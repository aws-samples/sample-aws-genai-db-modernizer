# src/atx_orchestrator/runtime/atx_store.py
"""ATX-artifact-store-backed ArtifactStore (JSON-only).

Pipeline state is stored as INTERNAL artifacts in the ``STATE`` category, one per
store-relative path (the path is the artifact ``label``). A ``path -> artifactId``
index — built once from a job-scoped ``ListArtifacts`` and updated on write —
powers ``exists``/``read``/``list_prefix`` and idempotent overwrite via
``upload_artifact(artifact_id=...)``.

Binary/text methods raise: deliverables go through ``runtime.artifacts.publish()``
and the LadybugDB graph is rebuilt on demand, so nothing binary needs the store.
An ``artifact://<id>`` key reads a specific artifact by id (used for the customer
upload, whose label/path we do not control).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import tempfile

from src.storage.artifact_store import ArtifactStore

ARTIFACT_SCHEME = "artifact://"
_STATE_CATEGORY = "STATE"
_JSON_FILE_TYPE = "JSON"
_NON_JSON_MSG = (
    "ATX artifact store is JSON-only; binary/text deliverables must go through "
    "runtime.artifacts.publish(), and the graph is rebuilt on demand."
)


class AtxArtifactStore(ArtifactStore):
    """ArtifactStore backed by the ATX Agentic Artifact Store."""

    # GraphPersistence checks this to skip .lbug persistence on ATX.
    supports_bytes = False

    def __init__(self, sdk_store=None, agent_instance_id: str | None = None):
        if sdk_store is None:
            from agent_builder_sdk.agentic_framework.artifact_store import (
                ArtifactStore as SdkArtifactStore,
            )
            from agent_builder_sdk.agentic_framework.client_factory import get_agentic_api_client
            from agent_builder_sdk.env_var import get_agent_context_from_env

            ctx = get_agent_context_from_env()
            sdk_store = SdkArtifactStore(
                workspace_id=ctx.workspace_id,
                job_id=ctx.job_id,
                agent_instance_id=ctx.agent_instance_id,
                client=get_agentic_api_client(),
            )
            agent_instance_id = ctx.agent_instance_id
        self._sdk = sdk_store
        self._agent_instance_id = agent_instance_id
        self._index: dict[str, str] | None = None  # path (label) -> artifactId

    # ------------------------------------------------------------------ index
    def _load_index(self) -> dict[str, str]:
        # Cached for the lifetime of this store instance -- fine because each
        # pipeline phase runs as a fresh process with its own store instance.
        if self._index is not None:
            return self._index
        index: dict[str, str] = {}
        next_token: str | None = None
        # Job-scoped listing (requestContext carries the job) with NO agentFilter,
        # so artifacts written by any of the job's agent instances are visible —
        # the pipeline's phases run as separate instances. Filter category in
        # Python; list_artifacts is documented newest-first, so keep the first id
        # seen per label (newest wins).
        while True:
            kwargs: dict = {
                "requestContext": self._sdk._create_request_context(),
                "maxResults": 100,
            }
            if next_token:
                kwargs["nextToken"] = next_token
            resp = self._sdk.client.list_artifacts(**kwargs)
            for a in resp.get("artifacts") or []:
                category = (a.get("artifactType") or {}).get("categoryType")
                label = a.get("artifactLabel")
                if category == _STATE_CATEGORY and label:
                    index.setdefault(label, a["artifactId"])
            next_token = resp.get("nextToken")
            if not next_token:
                break
        self._index = index
        return index

    def _resolve_id(self, path: str) -> str | None:
        if path.startswith(ARTIFACT_SCHEME):
            return path[len(ARTIFACT_SCHEME) :]
        return self._load_index().get(path)

    @staticmethod
    def _digest(content: bytes) -> str:
        try:
            from agent_builder_sdk.agentic_framework.common import calculate_digest
        except ImportError:
            return hashlib.sha256(content).hexdigest()
        return calculate_digest(content)  # type: ignore[no-any-return]

    # ------------------------------------------------------------------ reads
    def read_json(self, path: str) -> dict:
        artifact_id = self._resolve_id(path)
        if artifact_id is None:
            raise FileNotFoundError(f"No STATE artifact for key {path!r}")
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            self._sdk.download_artifact(artifact_id, tmp_path)
            with open(tmp_path, encoding="utf-8") as fh:
                return json.load(fh)  # type: ignore[no-any-return]
        finally:
            with contextlib.suppress(OSError):
                os.remove(tmp_path)

    def exists(self, path: str) -> bool:
        if path.startswith(ARTIFACT_SCHEME):
            # Deliberate optimism: we can't verify the id without a fetch, so a
            # missing/bad artifact id fails later, at download, not here.
            return True
        return path in self._load_index()

    def list_prefix(self, prefix: str) -> list[str]:
        return sorted(k for k in self._load_index() if k.startswith(prefix))

    # ------------------------------------------------------------------ writes
    def write_json(self, path: str, data: dict) -> None:
        content = json.dumps(data, indent=2, default=str).encode("utf-8")
        digest = self._digest(content)
        index = self._load_index()
        existing = index.get(path)
        if existing is not None:
            artifact_id = self._sdk.upload_artifact(
                content=content, digest=digest, artifact_id=existing
            )
        else:
            artifact_id = self._sdk.upload_artifact(
                content=content,
                digest=digest,
                category_type=_STATE_CATEGORY,
                file_type=_JSON_FILE_TYPE,
                label=path,
            )
        index[path] = artifact_id

    # --------------------------------------------------------- non-JSON: raise
    def read_bytes(self, path: str) -> bytes:
        raise NotImplementedError(_NON_JSON_MSG)

    def write_bytes(self, path: str, data: bytes) -> None:
        raise NotImplementedError(_NON_JSON_MSG)


class TransformAtxStore(AtxArtifactStore):
    """ATX backend plus the Transform-layer ``write_text`` signature (raises)."""

    def write_text(self, path: str, content: str, content_type: str = "text/plain") -> None:
        raise NotImplementedError(_NON_JSON_MSG)
