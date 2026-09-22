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
        self.uploads.append(
            {"artifact_id": artifact_id, "category_type": category_type, "label": label}
        )
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


class _PagedFakeSdkStore:
    """Minimal fake exercising ``_load_index``'s pagination + newest-first dedup.

    Takes pre-built pages of raw ``ListArtifacts``-shaped artifact dicts (as
    ``_artifact()`` below builds) and serves them one per call, chaining
    ``nextToken`` values until the last page. ``download_artifact`` serves
    canned content by id so tests can tell which id the index resolved to.
    """

    def __init__(self, pages: list[list[dict]], contents: dict[str, bytes]) -> None:
        self._pages = pages
        self._contents = contents
        self.client = self
        self.downloaded: list[str] = []

    def _create_request_context(self) -> dict:
        return {"jobMetadata": {"jobId": "job1", "workspaceId": "ws1"}}

    def list_artifacts(self, **kwargs):  # noqa: ANN003
        page_index = int(kwargs["nextToken"]) if kwargs.get("nextToken") else 0
        result: dict = {"artifacts": self._pages[page_index]}
        if page_index + 1 < len(self._pages):
            result["nextToken"] = str(page_index + 1)
        return result

    def download_artifact(self, artifact_id: str, destination_file_path: str) -> None:
        self.downloaded.append(artifact_id)
        with open(destination_file_path, "wb") as fh:
            fh.write(self._contents[artifact_id])


def _artifact(artifact_id: str, label: str, category: str = "STATE") -> dict:
    return {
        "artifactId": artifact_id,
        "artifactLabel": label,
        "artifactType": {"categoryType": category, "fileType": "JSON"},
    }


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
        aid = sdk.upload_artifact(
            json.dumps({"k": 9}).encode(),
            "d",
            category_type="STATE",
            file_type="JSON",
            label="whatever",
        )
        assert store.read_json(f"{ARTIFACT_SCHEME}{aid}") == {"k": 9}

    def test_exists_true_for_artifact_uri(self) -> None:
        store, _ = _store()
        assert store.exists(f"{ARTIFACT_SCHEME}art-anything") is True


class TestLoadIndexPaginationAndDedup:
    def test_pagination_merges_all_pages(self) -> None:
        sdk = _PagedFakeSdkStore(
            pages=[
                [_artifact("art-1", "db/job/a.json")],
                [_artifact("art-2", "db/job/b.json")],
            ],
            contents={"art-2": json.dumps({"page": 2}).encode()},
        )
        store = AtxArtifactStore(sdk_store=sdk, agent_instance_id="inst1")
        # The second page's key is only found if both pages were merged into the index.
        assert store.exists("db/job/b.json") is True
        assert store.read_json("db/job/b.json") == {"page": 2}

    def test_newest_first_dedup_keeps_first_seen_id(self) -> None:
        # Same label, both STATE, returned newest-first -- the index must keep
        # the first id it sees per label (setdefault), i.e. the newest one.
        sdk = _PagedFakeSdkStore(
            pages=[
                [
                    _artifact("art-new", "db/job/x.json"),
                    _artifact("art-old", "db/job/x.json"),
                ]
            ],
            contents={
                "art-new": json.dumps({"v": "new"}).encode(),
                "art-old": json.dumps({"v": "old"}).encode(),
            },
        )
        store = AtxArtifactStore(sdk_store=sdk, agent_instance_id="inst1")
        assert store.read_json("db/job/x.json") == {"v": "new"}
        assert sdk.downloaded == ["art-new"]


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


class TestTransformAtxStore:
    def test_write_text_raises(self) -> None:
        from src.atx_orchestrator.runtime.atx_store import TransformAtxStore

        store = TransformAtxStore(sdk_store=_FakeSdkStore(), agent_instance_id="inst1")
        with pytest.raises(NotImplementedError, match="publish"):
            store.write_text("db/job/report.md", "# hi", "text/markdown")


class TestUpgradeStoreAtxBranch:
    def test_rehomes_plain_atx_store_preserving_wiring(self) -> None:
        from src.atx_orchestrator.runtime.atx_store import TransformAtxStore
        from src.atx_orchestrator.runtime.store import upgrade_store

        fake = _FakeSdkStore()
        store = AtxArtifactStore(sdk_store=fake, agent_instance_id="inst1")
        result = upgrade_store(store)
        assert isinstance(result, TransformAtxStore)
        assert result._sdk is fake
        assert result._agent_instance_id == "inst1"

    def test_passthrough_for_existing_transform_atx_store(self) -> None:
        from src.atx_orchestrator.runtime.atx_store import TransformAtxStore
        from src.atx_orchestrator.runtime.store import upgrade_store

        fake = _FakeSdkStore()
        store = TransformAtxStore(sdk_store=fake, agent_instance_id="inst1")
        result = upgrade_store(store)
        assert result is store


class TestDownloadArtifactToById:
    def test_downloads_bytes_by_id_bypassing_index(self, tmp_path) -> None:
        # A binary deliverable (published EXTERNAL, not a STATE artifact) is read
        # back by its id, straight through the SDK download — no label-index lookup.
        store, sdk = _store()
        # Seed a non-JSON artifact directly in the fake SDK by id.
        sdk._by_id["graph-1"] = (
            "Assessment Context Graph",
            "CUSTOMER_OUTPUT",
            b"\x00LBUG\x01bytes",
        )
        dest = tmp_path / "context.lbug"

        store.download_artifact_to("graph-1", str(dest))

        assert dest.read_bytes() == b"\x00LBUG\x01bytes"

    def test_download_by_id_does_not_touch_state_index(self, tmp_path) -> None:
        # download_artifact_to must not require the artifact to be in the STATE
        # index (published deliverables are not STATE) — it resolves purely by id.
        store, sdk = _store()
        sdk._by_id["ext-9"] = ("x", "CUSTOMER_OUTPUT", b"payload")
        dest = tmp_path / "out.bin"
        store.download_artifact_to("ext-9", str(dest))
        assert dest.read_bytes() == b"payload"
        # The label index never learned about this id.
        assert "ext-9" not in store._load_index().values()
