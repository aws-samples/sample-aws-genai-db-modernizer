# tests/unit/graph/test_graph_persistence_capability.py
"""GraphPersistence must not touch a backend that does not support bytes (ATX)."""

from __future__ import annotations

from src.graph.persistence import GraphPersistence
from src.storage.artifact_store import ArtifactStore


class _NoBytesStore(ArtifactStore):
    """Minimal ATX-like store: JSON-only, no byte support."""

    supports_bytes = False

    def read_json(self, path: str) -> dict:  # pragma: no cover - unused in these tests
        raise NotImplementedError

    def write_json(self, path: str, data: dict) -> None:  # pragma: no cover
        raise NotImplementedError

    def list_prefix(self, prefix: str) -> list[str]:  # pragma: no cover
        raise NotImplementedError

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
