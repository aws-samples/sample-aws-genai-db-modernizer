"""LocalArtifactStore.write_json writes atomically (#517 round-1 review,
I5): a status marker a job's own failure handler writes right as the
process is dying must never leave a reader observing a half-written file.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from src.storage.local_store import LocalArtifactStore


def test_write_json_round_trips(tmp_path: Path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))

    store.write_json("db/job/_status.json", {"status": "FAILED"})

    assert store.read_json("db/job/_status.json") == {"status": "FAILED"}


def test_write_json_changes_the_destination_inode(tmp_path: Path) -> None:
    """The old implementation (``full_path.write_text(...)``) truncates and
    rewrites the same file in place -- same inode, with a window where a
    concurrent reader sees a partial write. This must fail against that
    code: a rename-based write always swaps in a brand new inode."""
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json("db/job/_status.json", {"status": "CANCELLED", "stopped_at": "t" * 500})
    first_inode = (tmp_path / "db" / "job" / "_status.json").stat().st_ino

    store.write_json("db/job/_status.json", {"status": "FAILED"})

    second_inode = (tmp_path / "db" / "job" / "_status.json").stat().st_ino
    assert second_inode != first_inode
    assert store.read_json("db/job/_status.json") == {"status": "FAILED"}


def test_write_json_replaces_via_os_replace(tmp_path: Path) -> None:
    """Must fail against the old ``write_text``-in-place implementation,
    which never calls ``os.replace`` at all."""
    store = LocalArtifactStore(base_dir=str(tmp_path))
    full_path = tmp_path / "db" / "job" / "_status.json"

    with patch("src.storage.local_store.os.replace", wraps=os.replace) as mock_replace:
        store.write_json("db/job/_status.json", {"status": "FAILED"})

    mock_replace.assert_called_once()
    src, dst = mock_replace.call_args[0]
    assert Path(dst) == full_path
    assert Path(src).parent == full_path.parent
    assert Path(src) != full_path


def test_write_json_preserves_an_existing_files_permissions(tmp_path: Path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json("db/job/_status.json", {"status": "CANCELLED"})
    full_path = tmp_path / "db" / "job" / "_status.json"
    os.chmod(full_path, 0o640)

    store.write_json("db/job/_status.json", {"status": "FAILED"})

    assert (full_path.stat().st_mode & 0o777) == 0o640


def test_write_json_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))

    store.write_json("db/job/_status.json", {"status": "FAILED"})

    leftovers = [p for p in (tmp_path / "db" / "job").iterdir() if ".tmp" in p.name]
    assert leftovers == []


def test_write_json_cleans_up_the_temp_file_if_replace_fails(tmp_path: Path) -> None:
    """try/finally unlink: a failure between creating the temp file and the
    rename must not leave it behind, and must not touch the destination."""
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json("db/job/_status.json", {"status": "CANCELLED"})

    with patch("src.storage.local_store.os.replace", side_effect=OSError("disk full")):
        with pytest.raises(OSError, match="disk full"):
            store.write_json("db/job/_status.json", {"status": "FAILED"})

    assert store.read_json("db/job/_status.json") == {"status": "CANCELLED"}
    leftovers = [p for p in (tmp_path / "db" / "job").iterdir() if ".tmp" in p.name]
    assert leftovers == []


def test_write_json_respects_the_process_umask_for_a_new_file(tmp_path: Path) -> None:
    """#517 round-2 review: a new file's mode is 0o666 reduced by the real
    process umask, not a hardcoded constant that ignores it."""
    store = LocalArtifactStore(base_dir=str(tmp_path))
    old_umask = os.umask(0o077)
    try:
        store.write_json("db/job/_status.json", {"status": "FAILED"})
    finally:
        os.umask(old_umask)

    full_path = tmp_path / "db" / "job" / "_status.json"
    assert (full_path.stat().st_mode & 0o777) == 0o600


def test_write_json_to_different_paths_does_not_collide(tmp_path: Path) -> None:
    """Each write's temp file is named after its own destination, so two
    different artifacts under the same job never share a temp file."""
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json("db/job/a.json", {"n": 1})
    store.write_json("db/job/b.json", {"n": 2})

    assert store.read_json("db/job/a.json") == {"n": 1}
    assert store.read_json("db/job/b.json") == {"n": 2}


def test_write_json_from_multiple_threads_does_not_collide(tmp_path: Path) -> None:
    """#517 round-3 review: each writer's temp file gets its own random
    name (``secrets.token_hex(8)``) and is created with ``O_EXCL``, so two
    threads racing to write the *same* destination never collide on the
    same temp file the way sharing a PID-based name could."""
    import threading

    store = LocalArtifactStore(base_dir=str(tmp_path))
    errors: list[BaseException] = []

    def _write(n: int) -> None:
        try:
            store.write_json("db/job/_status.json", {"n": n})
        except BaseException as exc:  # noqa: BLE001 - captured for the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=_write, args=(n,)) for n in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    result = store.read_json("db/job/_status.json")
    assert result["n"] in range(32)
    leftovers = [p for p in (tmp_path / "db" / "job").iterdir() if ".tmp" in p.name]
    assert leftovers == []


def test_write_json_runs_containment_before_the_atomic_write(tmp_path: Path) -> None:
    """#512's containment check and the atomic write must compose: a path
    that escapes the root (here, via a symlink) must be rejected before
    any temp file is created, and nothing ends up written outside the
    root -- the same guarantee test_local_store_containment.py's other
    write_bytes/read_json/list_prefix symlink-escape tests already cover,
    exercised against write_json's own, now-atomic implementation."""
    base = tmp_path / "artifacts"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    link_dir = base / "db" / "job"
    link_dir.mkdir(parents=True)
    os.symlink(outside, link_dir / "linked")

    store = LocalArtifactStore(str(base))
    with pytest.raises(ValueError, match="escapes the artifact root"):
        store.write_json("db/job/linked/evil.json", {"leaked": True})

    assert list(outside.iterdir()) == []
