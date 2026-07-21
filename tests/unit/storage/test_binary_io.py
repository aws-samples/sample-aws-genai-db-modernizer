"""Binary read/write for the artifact store abstraction."""

from src.storage.local_store import LocalArtifactStore


def test_local_write_then_read_bytes_roundtrip(tmp_path):
    store = LocalArtifactStore(str(tmp_path))
    payload = b"\x00\x01\x02lbug-bytes\xff"
    store.write_bytes("db/job/graph/context.lbug", payload)
    assert store.read_bytes("db/job/graph/context.lbug") == payload


def test_local_exists_false_for_missing_binary(tmp_path):
    store = LocalArtifactStore(str(tmp_path))
    assert store.exists("db/job/graph/context.lbug") is False


def test_local_write_bytes_creates_parent_dirs(tmp_path):
    store = LocalArtifactStore(str(tmp_path))
    store.write_bytes("a/b/c/file.bin", b"x")
    assert store.exists("a/b/c/file.bin") is True
