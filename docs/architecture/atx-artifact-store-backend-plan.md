# ATX Artifact Store Backend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a JSON-only `AtxArtifactStore` so ATX executions read/write all pipeline state through the ATX Agentic Artifact Store (never our S3), and read the customer's uploaded input in place.

**Architecture:** A new Transform-layer backend wraps the `agent_builder_sdk` `ArtifactStore`. Store-relative paths are stored as artifact `label`s under `CategoryType.STATE`; a job-scoped `path→artifactId` index powers `exists`/`read`/`list_prefix` and idempotent overwrite. `src/storage/` (upstream core-modernizer, kept byte-identical) is untouched — selection happens in `make_store()`. Binary/text methods raise; the graph rebuilds on demand.

**Tech Stack:** Python 3.12, `agent_builder_sdk` (ATX runtime only, imported lazily), pytest.

**Design source:** `docs/architecture/atx-artifact-store-backend-design.md`

**Scope note:** This plan is Phase 1 (the backend + wiring). Two follow-on plans come after it lands, in the same branch: (a) rerouting non-JSON deliverable writers (`write_text`/`write_bytes` in `tools.py`/`core.py`) to `publish()`; (b) the Phase-2 `query_context_graph` orchestrator tool. Both are gated on this plan.

---

## Task 1: `AtxArtifactStore` — JSON backend over the SDK

**Files:**

- Create: `src/atx_orchestrator/runtime/atx_store.py`
- Test: `tests/unit/atx_orchestrator/test_atx_store.py`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/atx_orchestrator/test_atx_store.py
"""Tests for the ATX-artifact-store-backed ArtifactStore (JSON-only)."""

from __future__ import annotations

import json

import pytest

from src.atx_orchestrator.runtime.atx_store import ARTIFACT_SCHEME, AtxArtifactStore


class _FakeSdkStore:
    """Stand-in for agent_builder_sdk ArtifactStore.

    Models the behaviours the backend relies on: upload creates a new id or
    overwrites an existing id in place; the raw client lists job-scoped artifacts
    (paginated) with a STATE category + label; download writes the stored bytes to
    a path. Only JSON content is exercised.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, tuple[str, str, bytes]] = {}  # id -> (label, category, content)
        self._seq = 0
        self.client = self  # backend calls self._sdk.client.list_artifacts(...)
        self.uploads: list[dict] = []

    # --- raw client seam ---
    def _create_request_context(self) -> dict:
        return {"jobMetadata": {"jobId": "job1", "workspaceId": "ws1"}}

    def list_artifacts(self, **kwargs):  # noqa: ANN003
        arts = [
            {
                "artifactId": aid,
                "artifactLabel": label,
                "artifactType": {"categoryType": cat, "fileType": "JSON"},
            }
            for aid, (label, cat, _content) in self._by_id.items()
        ]
        return {"artifacts": arts}

    # --- SDK ArtifactStore seam ---
    def upload_artifact(
        self,
        content: bytes,
        digest: str,
        artifact_id: str | None = None,
        category_type: str | None = None,
        file_type: str | None = None,
        plan_step_id: str | None = None,
        label: str | None = None,
    ) -> str:
        self.uploads.append({"artifact_id": artifact_id, "category_type": category_type, "label": label})
        if artifact_id is not None:
            old_label, old_cat, _ = self._by_id[artifact_id]
            self._by_id[artifact_id] = (old_label, old_cat, content)
            return artifact_id
        self._seq += 1
        new_id = f"art-{self._seq}"
        self._by_id[new_id] = (label or "", category_type or "", content)
        return new_id

    def download_artifact(self, artifact_id: str, destination_file_path: str) -> None:
        _label, _cat, content = self._by_id[artifact_id]
        with open(destination_file_path, "wb") as fh:
            fh.write(content)


def _store() -> tuple[AtxArtifactStore, _FakeSdkStore]:
    sdk = _FakeSdkStore()
    return AtxArtifactStore(sdk_store=sdk, agent_instance_id="inst1"), sdk


class TestJsonRoundTrip:
    def test_write_then_read(self) -> None:
        store, _ = _store()
        store.write_json("db/job/collector/output.json", {"n": 1})
        assert store.read_json("db/job/collector/output.json") == {"n": 1}

    def test_write_creates_state_labelled_artifact(self) -> None:
        store, sdk = _store()
        store.write_json("db/job/analysis.json", {"a": 2})
        assert sdk.uploads[0]["category_type"] == "STATE"
        assert sdk.uploads[0]["label"] == "db/job/analysis.json"
        assert sdk.uploads[0]["artifact_id"] is None  # create, not overwrite

    def test_second_write_overwrites_same_id(self) -> None:
        store, sdk = _store()
        store.write_json("db/job/x.json", {"v": 1})
        store.write_json("db/job/x.json", {"v": 2})
        # first write creates (artifact_id None); second overwrites in place (concrete id)
        assert sdk.uploads[0]["artifact_id"] is None
        assert sdk.uploads[1]["artifact_id"] is not None
        assert store.read_json("db/job/x.json") == {"v": 2}

    def test_exists_true_after_write_false_otherwise(self) -> None:
        store, _ = _store()
        assert store.exists("db/job/x.json") is False
        store.write_json("db/job/x.json", {"v": 1})
        assert store.exists("db/job/x.json") is True

    def test_read_missing_raises(self) -> None:
        store, _ = _store()
        with pytest.raises(FileNotFoundError):
            store.read_json("db/job/missing.json")

    def test_list_prefix(self) -> None:
        store, _ = _store()
        store.write_json("db/job/a.json", {})
        store.write_json("db/job/sub/b.json", {})
        store.write_json("other/c.json", {})
        assert store.list_prefix("db/job/") == ["db/job/a.json", "db/job/sub/b.json"]


class TestArtifactScheme:
    def test_read_by_artifact_uri(self) -> None:
        store, sdk = _store()
        aid = sdk.upload_artifact(json.dumps({"k": 9}).encode(), "d", category_type="STATE", file_type="JSON", label="whatever")
        assert store.read_json(f"{ARTIFACT_SCHEME}{aid}") == {"k": 9}

    def test_exists_true_for_artifact_uri(self) -> None:
        store, _ = _store()
        assert store.exists(f"{ARTIFACT_SCHEME}art-anything") is True


class TestNonJsonRaises:
    def test_read_bytes_raises(self) -> None:
        store, _ = _store()
        with pytest.raises(NotImplementedError, match="publish"):
            store.read_bytes("x")

    def test_write_bytes_raises(self) -> None:
        store, _ = _store()
        with pytest.raises(NotImplementedError, match="publish"):
            store.write_bytes("x", b"data")

    def test_supports_bytes_is_false(self) -> None:
        store, _ = _store()
        assert store.supports_bytes is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/atx_orchestrator/test_atx_store.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.atx_orchestrator.runtime.atx_store'`.

- [ ] **Step 3: Implement the backend**

```python
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
import json
import logging
import os
import tempfile

from src.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)

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
            from agent_builder_sdk.agentic_framework.client_factory import (
                get_agentic_api_client,
            )
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
        from agent_builder_sdk.agentic_framework.common import calculate_digest

        return calculate_digest(content)

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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/atx_orchestrator/test_atx_store.py -q`
Expected: PASS (all tests green).

- [ ] **Step 5: Commit**

```bash
git add src/atx_orchestrator/runtime/atx_store.py tests/unit/atx_orchestrator/test_atx_store.py
git commit -m "feat(storage): add JSON-only AtxArtifactStore backend"
```

---

## Task 2: `TransformAtxStore` + backend selection

**Files:**

- Modify: `src/atx_orchestrator/runtime/atx_store.py` (append `TransformAtxStore`)
- Modify: `src/atx_orchestrator/runtime/store.py:50-68` (`upgrade_store`)
- Modify: `src/atx_orchestrator/core.py:24-35` (`make_store`)
- Test: `tests/unit/atx_orchestrator/test_atx_store.py` (append), `tests/unit/atx_orchestrator/test_make_store_selection.py` (create)

- [ ] **Step 1: Write the failing tests**

Append to `tests/unit/atx_orchestrator/test_atx_store.py`:

```python
class TestTransformAtxStore:
    def test_write_text_raises(self) -> None:
        from src.atx_orchestrator.runtime.atx_store import TransformAtxStore

        store = TransformAtxStore(sdk_store=_FakeSdkStore(), agent_instance_id="inst1")
        with pytest.raises(NotImplementedError, match="publish"):
            store.write_text("db/job/report.md", "# hi", "text/markdown")
```

Create `tests/unit/atx_orchestrator/test_make_store_selection.py`:

```python
"""make_store() picks the ATX backend when STORAGE_BACKEND=atx, else S3/local."""

from __future__ import annotations

import pytest

from src.atx_orchestrator import core
from src.atx_orchestrator.runtime.atx_store import TransformAtxStore


def test_atx_backend_selected_by_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "atx")
    sentinel = object()
    monkeypatch.setattr(
        "src.atx_orchestrator.runtime.atx_store.TransformAtxStore.__init__",
        lambda self, *a, **k: None,
    )
    store = core.make_store()
    assert isinstance(store, TransformAtxStore)


def test_local_backend_when_env_unset(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv("STORAGE_BACKEND", raising=False)
    monkeypatch.delenv("S3_BUCKET", raising=False)
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path))
    from src.atx_orchestrator.runtime.store import TransformLocalStore

    assert isinstance(core.make_store(), TransformLocalStore)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/atx_orchestrator/test_atx_store.py::TestTransformAtxStore tests/unit/atx_orchestrator/test_make_store_selection.py -q`
Expected: FAIL — `ImportError: cannot import name 'TransformAtxStore'`.

- [ ] **Step 3: Implement**

Append to `src/atx_orchestrator/runtime/atx_store.py`:

```python
class TransformAtxStore(AtxArtifactStore):
    """ATX backend plus the Transform-layer ``write_text`` signature (raises)."""

    def write_text(self, path: str, content: str, content_type: str = "text/plain") -> None:
        raise NotImplementedError(_NON_JSON_MSG)
```

Modify `src/atx_orchestrator/runtime/store.py::upgrade_store` — add an ATX branch before the `raise TypeError` (so an already-constructed ATX backend is accepted; it already provides `write_text`):

```python
    if isinstance(inner, S3ArtifactStore):
        return TransformS3Store(inner.bucket, inner.s3)
    if isinstance(inner, LocalArtifactStore):
        return TransformLocalStore(str(inner.base_dir))
    from src.atx_orchestrator.runtime.atx_store import AtxArtifactStore, TransformAtxStore

    if isinstance(inner, AtxArtifactStore):
        return inner if isinstance(inner, TransformAtxStore) else TransformAtxStore(
            sdk_store=inner._sdk, agent_instance_id=inner._agent_instance_id
        )
    raise TypeError(
```

Modify `src/atx_orchestrator/core.py::make_store`:

```python
def make_store():
    """Create a text-capable ArtifactStore.

    ``STORAGE_BACKEND=atx`` selects the ATX Agentic Artifact Store backend (ATX
    runs store nothing in our S3). Otherwise core-modernizer's
    ``create_artifact_store()`` decides S3-vs-local from env and ``upgrade_store``
    re-homes it onto the Transform subclass that adds ``write_text``.
    """
    from src.atx_orchestrator.runtime.store import upgrade_store

    if os.environ.get("STORAGE_BACKEND") == "atx":
        from src.atx_orchestrator.runtime.atx_store import TransformAtxStore

        return TransformAtxStore()

    from src.storage import create_artifact_store

    return upgrade_store(create_artifact_store())
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/atx_orchestrator/test_atx_store.py tests/unit/atx_orchestrator/test_make_store_selection.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/atx_orchestrator/runtime/atx_store.py src/atx_orchestrator/runtime/store.py src/atx_orchestrator/core.py tests/unit/atx_orchestrator/test_atx_store.py tests/unit/atx_orchestrator/test_make_store_selection.py
git commit -m "feat(storage): select ATX backend via STORAGE_BACKEND=atx"
```

---

## Task 3: Read customer input in place (delete the S3 copy)

**Files:**

- Modify: `src/atx_orchestrator/core.py:181-201` (`_discover_uploaded_input` tail)
- Modify: `tests/unit/atx_orchestrator/test_collector_input.py` (update discovery + resolve tests)

- [ ] **Step 1: Update the failing tests**

In `tests/unit/atx_orchestrator/test_collector_input.py`, replace the download/stage assertions with `artifact://` expectations. Change `_FakeArtifactStore.download_artifact` to be unused (discovery no longer downloads), and update these tests:

```python
    def test_single_upload_returns_artifact_uri(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake = _FakeArtifactStore(
            [
                _artifact("art-1", "default", path="discourse-collection.json"),
                _artifact("obj-1", "default", path="job_objective"),
            ]
        )
        _inject_sdk(monkeypatch, fake)
        result = core._discover_uploaded_input(_FakeStore(), "uuid1", "discourse")
        assert result == "artifact://art-1"
        assert fake.downloaded == []  # no download, no S3 copy

    def test_real_shape_two_json_objective_excluded_by_basename(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        collection = _artifact("33641880", "default", path="discourse-collection.json")
        objective = _artifact("788c3a44", "default", path="job_objective")
        fake = _FakeArtifactStore([collection, objective])
        _inject_sdk(monkeypatch, fake)
        result = core._discover_uploaded_input(_FakeStore(), "uuid1", "discourse")
        assert result == "artifact://33641880"
```

Delete `test_single_upload_downloaded_and_staged` (replaced above). Add a resolve test for the scheme:

```python
    def test_resolve_uses_artifact_uri_without_seed(self) -> None:
        class _ArtifactUriStore(_FakeStore):
            def exists(self, path: str) -> bool:
                return path.startswith("artifact://") or super().exists(path)

        store = _ArtifactUriStore()
        assert (
            core._resolve_collector_input(store, "job", "db", "artifact://art-1")
            == "artifact://art-1"
        )
```

Update the orchestrator-wiring tests (`TestOrchestratorPassesDiscoveredKey`) that patch `_discover_uploaded_input` to return `"artifact://art-1"` instead of `seed`, and assert `message["input_key"] == "artifact://art-1"`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/unit/atx_orchestrator/test_collector_input.py -q`
Expected: FAIL — discovery still returns the seed key / still calls `write_json`.

- [ ] **Step 3: Implement — replace the copy with an `artifact://` key**

In `src/atx_orchestrator/core.py`, replace the block at lines 181-201 (from `artifact_id = candidates[0]["artifactId"]` through `return seed_key`) with:

```python
    artifact_id = candidates[0]["artifactId"]
    logger.info(
        "upload discovery: resolved CUSTOMER_INPUT artifact %s (label=%r) as artifact://%s "
        "(read in place; nothing staged to our store)",
        artifact_id,
        candidates[0].get("artifactLabel"),
        artifact_id,
    )
    from src.atx_orchestrator.runtime.atx_store import ARTIFACT_SCHEME

    return f"{ARTIFACT_SCHEME}{artifact_id}"
```

Then remove now-dead imports/uses in `core.py` **only if unused elsewhere in the file**: check `tempfile`, `contextlib`, `default_input_key`. Run `grep -n "tempfile\|contextlib\.\|default_input_key" src/atx_orchestrator/core.py`; drop an import only if it has no other reference. (`default_input_key` is still used by `_resolve_collector_input`'s seed fallback — keep it.)

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/unit/atx_orchestrator/test_collector_input.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/atx_orchestrator/core.py tests/unit/atx_orchestrator/test_collector_input.py
git commit -m "feat(atx): read customer upload in place via artifact:// key (no S3 copy)"
```

---

## Task 4: Graph persistence — skip when the backend has no bytes

**Files:**

- Modify: `src/graph/persistence.py:27-41`
- Test: `tests/unit/graph/test_graph_persistence_capability.py` (create)

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/graph/test_graph_persistence_capability.py
"""GraphPersistence must not touch a backend that does not support bytes (ATX)."""

from __future__ import annotations

from src.graph.persistence import GraphPersistence


class _NoBytesStore:
    supports_bytes = False

    def exists(self, path: str) -> bool:  # pragma: no cover - must not be called
        raise AssertionError("exists() must not be called on a no-bytes backend")

    def read_bytes(self, path: str) -> bytes:  # pragma: no cover
        raise AssertionError("read_bytes() must not be called")

    def write_bytes(self, path: str, data: bytes) -> None:  # pragma: no cover
        raise AssertionError("write_bytes() must not be called")


def test_download_if_exists_returns_false_without_touching_store(tmp_path) -> None:
    gp = GraphPersistence(_NoBytesStore())
    assert gp.download_if_exists("db", "job", str(tmp_path / "g.lbug")) is False


def test_upload_is_skipped(tmp_path) -> None:
    local = tmp_path / "g.lbug"
    local.write_bytes(b"graph")
    gp = GraphPersistence(_NoBytesStore())
    gp.upload("db", "job", str(local))  # must not raise
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/unit/graph/test_graph_persistence_capability.py -q`
Expected: FAIL — `AssertionError: exists() must not be called` (current code calls `store.exists`).

- [ ] **Step 3: Implement the capability guard**

In `src/graph/persistence.py`, guard both methods:

```python
    def download_if_exists(self, db_name: str, job_id: str, local_path: str) -> bool:
        """Download the graph to local_path. Return False if it isn't stored yet."""
        if not getattr(self._store, "supports_bytes", True):
            return False
        key = self.graph_key(db_name, job_id)
        if not self._store.exists(key):
            return False
        data = self._store.read_bytes(key)
        dest = Path(local_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True

    def upload(self, db_name: str, job_id: str, local_path: str) -> None:
        """Upload the freshly built graph file to the store."""
        if not getattr(self._store, "supports_bytes", True):
            logger.info(
                "graph persistence skipped: backend %s does not support bytes; "
                "the graph will be rebuilt on demand from STATE artifacts",
                type(self._store).__name__,
            )
            return
        data = Path(local_path).read_bytes()
        self._store.write_bytes(self.graph_key(db_name, job_id), data)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/unit/graph/test_graph_persistence_capability.py -q`
Expected: PASS. Then run the existing graph tests to confirm no regression: `python -m pytest tests/unit/graph tests/graph -q` → PASS.

- [ ] **Step 5: Commit**

```bash
git add src/graph/persistence.py tests/unit/graph/test_graph_persistence_capability.py
git commit -m "feat(graph): skip .lbug persistence when backend has no byte support"
```

---

## Task 5: Full-suite verification

- [ ] **Step 1: Run the whole unit suite**

Run: `python -m pytest tests/unit tests/graph -q`
Expected: PASS. Investigate and fix any failure caused by the changes above (most likely other tests that relied on `_discover_uploaded_input` staging behaviour — update them to the `artifact://` contract).

- [ ] **Step 2: Confirm `src/storage/` is untouched**

Run: `git diff --name-only main..HEAD -- src/storage/`
Expected: empty output (the upstream-mirrored package stays byte-identical; all ATX code lives under `src/atx_orchestrator/runtime/`).

- [ ] **Step 3: Commit any test fixups**

```bash
git add -A
git commit -m "test(atx): align remaining tests with the ATX artifact-store backend"
```

---

## Deferred to follow-on plans (same branch, after this lands)

1. **Non-JSON deliverable reroute.** Audit the store-backed `write_text`/
   `write_bytes` call sites (`atx_orchestrator/tools.py:406,522,1026,1027,1070,1106,1107,1286`,
   `core.py:638`) and route each to `runtime.artifacts.publish()` (deliverables)
   on the ATX path. Requires first mapping `publish()`'s interface and each
   deliverable's category/visibility. Gates flipping `STORAGE_BACKEND=atx` in the
   ATX Dockerfile/runtime and removing `S3_BUCKET`.
2. **Phase 2 — `query_context_graph` orchestrator tool.** Build the context graph
   in-process from `STATE` JSON artifacts (reusing `rebuild_graph`) and run
   curated/Cypher queries, returning rows. No S3, no API hop. See the design doc.

## Self-Review

- **Spec coverage:** Backend (Task 1), STATE category + label + index + overwrite
  (Task 1), `artifact://` input (Tasks 1, 3), selection via env (Task 2),
  `upgrade_store` (Task 2), graph rebuild/zero-S3 (Task 4), non-JSON raises
  (Tasks 1, 2). Deliverable reroute and Phase-2 tool are explicitly deferred with
  scope. `src/storage/` untouched (Task 5 Step 2) — refines spec decision #3.
- **Placeholders:** none — every code step is complete.
- **Type consistency:** `ARTIFACT_SCHEME`, `AtxArtifactStore`, `TransformAtxStore`,
  `supports_bytes`, `_sdk`, `_agent_instance_id`, `write_json`/`read_json`/`exists`/
  `list_prefix`/`read_bytes`/`write_bytes`/`write_text` are used consistently
  across tasks.
