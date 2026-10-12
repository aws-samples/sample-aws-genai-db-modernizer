"""LocalArtifactStore must refuse to read, write, or list anything outside
its own base_dir, as defence in depth behind the route-level
database_name/job_id/filename validators (#420/#505/#508). CodeQL's
py/path-injection query flagged list_prefix (it doesn't model a caller's
validator as a sanitizer); _contained_path closes the same gap in every
method, not just the one the scanner happened to point at.
"""

from __future__ import annotations

import os

import pytest

from src.storage.local_store import LocalArtifactStore

_TRAVERSAL_PATHS = ["../escape.json", "../../etc/passwd", "a/../../escape.json"]


@pytest.fixture
def store(tmp_path):
    return LocalArtifactStore(str(tmp_path))


# ---------------------------------------------------------------------------
# Normal use still works
# ---------------------------------------------------------------------------


def test_write_then_read_json_roundtrip(store) -> None:
    store.write_json("db/job/collector/output.json", {"a": 1})
    assert store.read_json("db/job/collector/output.json") == {"a": 1}


def test_write_then_read_bytes_roundtrip(store) -> None:
    store.write_bytes("db/job/graph/context.lbug", b"\x00\x01lbug\xff")
    assert store.read_bytes("db/job/graph/context.lbug") == b"\x00\x01lbug\xff"


def test_exists_false_for_an_ordinary_missing_path(store) -> None:
    assert store.exists("db/job/nope.json") is False


def test_list_prefix_returns_matching_json_keys(store) -> None:
    store.write_json("db/job/collector/output.json", {})
    store.write_json("db/job/referee-triage/triage.json", {})
    assert sorted(store.list_prefix("db/job")) == [
        "db/job/collector/output.json",
        "db/job/referee-triage/triage.json",
    ]


# ---------------------------------------------------------------------------
# Traversal: ".."
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad_path", _TRAVERSAL_PATHS)
def test_read_json_rejects_dot_dot_traversal(store, bad_path: str) -> None:
    with pytest.raises(ValueError, match="escapes the artifact root"):
        store.read_json(bad_path)


@pytest.mark.parametrize("bad_path", _TRAVERSAL_PATHS)
def test_write_json_rejects_dot_dot_traversal(store, bad_path: str, tmp_path) -> None:
    with pytest.raises(ValueError, match="escapes the artifact root"):
        store.write_json(bad_path, {"leaked": True})
    # Nothing was written outside the store's own tmp_path.
    assert not any(tmp_path.parent.glob("escape.json"))


@pytest.mark.parametrize("bad_path", _TRAVERSAL_PATHS)
def test_read_bytes_rejects_dot_dot_traversal(store, bad_path: str) -> None:
    with pytest.raises(ValueError, match="escapes the artifact root"):
        store.read_bytes(bad_path)


@pytest.mark.parametrize("bad_path", _TRAVERSAL_PATHS)
def test_write_bytes_rejects_dot_dot_traversal(store, bad_path: str) -> None:
    with pytest.raises(ValueError, match="escapes the artifact root"):
        store.write_bytes(bad_path, b"leaked")


@pytest.mark.parametrize("bad_path", _TRAVERSAL_PATHS)
def test_exists_rejects_dot_dot_traversal(store, bad_path: str) -> None:
    with pytest.raises(ValueError, match="escapes the artifact root"):
        store.exists(bad_path)


@pytest.mark.parametrize("bad_path", ["../", "../etc", "a/../../etc"])
def test_list_prefix_rejects_dot_dot_traversal(store, bad_path: str) -> None:
    with pytest.raises(ValueError, match="escapes the artifact root"):
        store.list_prefix(bad_path)


# ---------------------------------------------------------------------------
# Absolute paths
# ---------------------------------------------------------------------------


def test_read_json_rejects_an_absolute_path(store) -> None:
    with pytest.raises(ValueError, match="escapes the artifact root"):
        store.read_json("/etc/passwd")


def test_write_bytes_rejects_an_absolute_path(store) -> None:
    with pytest.raises(ValueError, match="escapes the artifact root"):
        store.write_bytes("/tmp/evil.bin", b"leaked")  # nosec B108 -- never reached


def test_exists_rejects_an_absolute_path(store) -> None:
    with pytest.raises(ValueError, match="escapes the artifact root"):
        store.exists("/etc/passwd")


def test_list_prefix_rejects_an_absolute_path(store) -> None:
    with pytest.raises(ValueError, match="escapes the artifact root"):
        store.list_prefix("/etc")


# ---------------------------------------------------------------------------
# A sibling directory whose name merely starts with the same prefix must
# not be treated as "inside" base_dir (the off-by-one startswith bug this
# idiom has to avoid: "/base-evil".startswith("/base") is True but is not
# contained in "/base").
# ---------------------------------------------------------------------------


def test_rejects_a_sibling_directory_with_a_name_prefix_collision(tmp_path) -> None:
    base = tmp_path / "artifacts"
    base.mkdir()
    sibling = tmp_path / "artifacts-evil"
    sibling.mkdir()
    (sibling / "secret.json").write_text('{"leaked": true}')

    store = LocalArtifactStore(str(base))
    with pytest.raises(ValueError, match="escapes the artifact root"):
        store.read_json("../artifacts-evil/secret.json")


# ---------------------------------------------------------------------------
# Symlink escape
# ---------------------------------------------------------------------------


def test_read_json_rejects_a_symlink_escaping_the_root(tmp_path) -> None:
    base = tmp_path / "artifacts"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.json").write_text('{"leaked": true}')

    link_dir = base / "db" / "job"
    link_dir.mkdir(parents=True)
    os.symlink(outside, link_dir / "linked")

    store = LocalArtifactStore(str(base))
    with pytest.raises(ValueError, match="escapes the artifact root"):
        store.read_json("db/job/linked/secret.json")


def test_list_prefix_rejects_a_symlink_escaping_the_root(tmp_path) -> None:
    base = tmp_path / "artifacts"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.json").write_text("{}")

    link_dir = base / "db" / "job"
    link_dir.mkdir(parents=True)
    os.symlink(outside, link_dir / "linked")

    store = LocalArtifactStore(str(base))
    with pytest.raises(ValueError, match="escapes the artifact root"):
        store.list_prefix("db/job/linked")


def test_write_bytes_rejects_a_symlink_escaping_the_root(tmp_path) -> None:
    base = tmp_path / "artifacts"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    link_dir = base / "db" / "job"
    link_dir.mkdir(parents=True)
    os.symlink(outside, link_dir / "linked")

    store = LocalArtifactStore(str(base))
    with pytest.raises(ValueError, match="escapes the artifact root"):
        store.write_bytes("db/job/linked/evil.bin", b"leaked")
    assert not (outside / "evil.bin").exists()


# ---------------------------------------------------------------------------
# A path that resolves to exactly base_dir itself is contained, not an escape
# ---------------------------------------------------------------------------


def test_list_prefix_accepts_the_root_itself(store) -> None:
    store.write_json("db/job/collector/output.json", {})
    assert store.list_prefix(".") == ["db/job/collector/output.json"]
