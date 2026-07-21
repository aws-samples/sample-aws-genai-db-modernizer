"""Tests for GraphPersistence — S3/local .lbug caching via the artifact store."""

from pathlib import Path

from src.graph.persistence import GraphPersistence
from src.storage.local_store import LocalArtifactStore


def _make(tmp_path):
    store = LocalArtifactStore(str(tmp_path / "store"))
    return GraphPersistence(store), store


def test_download_returns_false_when_absent(tmp_path):
    gp, _ = _make(tmp_path)
    local = str(tmp_path / "work" / "context.lbug")
    assert gp.download_if_exists("db", "job", local) is False


def test_upload_then_download_roundtrip(tmp_path):
    gp, _ = _make(tmp_path)
    src = tmp_path / "work" / "context.lbug"
    src.parent.mkdir(parents=True)
    src.write_bytes(b"lbug-binary-\x00\xff")

    gp.upload("db", "job", str(src))

    dst = str(tmp_path / "work2" / "context.lbug")
    assert gp.download_if_exists("db", "job", dst) is True
    assert Path(dst).read_bytes() == b"lbug-binary-\x00\xff"


def test_graph_key_is_deterministic(tmp_path):
    gp, _ = _make(tmp_path)
    assert gp.graph_key("mydb", "job-1") == "mydb/job-1/graph/context.lbug"
