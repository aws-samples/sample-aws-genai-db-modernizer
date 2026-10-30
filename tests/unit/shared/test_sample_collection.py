# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#331: a sample collection path works on a fresh clone with no manual unzip.

Only ``docs/examples/<name>/<name>.zip`` is tracked (the extracted ``.json`` is
gitignored), so the documented first command must extract it itself.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import zipfile
from pathlib import Path

import pytest

from src.shared.sample_collection import CollectionPathError, resolve_collection_path

REPO = Path(__file__).resolve().parents[3]
WORDPRESS_ZIP = REPO / "docs" / "examples" / "wordpress" / "wordpress.zip"


def _make_zip(path: Path, members: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)


def test_missing_json_with_sibling_zip_is_extracted(tmp_path):
    _make_zip(tmp_path / "demo.zip", {"demo-collection.json": '{"a": 1}'})
    target = tmp_path / "demo-collection.json"

    assert resolve_collection_path(target) == str(target)
    assert json.loads(target.read_text()) == {"a": 1}
    assert not list(tmp_path.glob(".*.tmp"))


def test_zip_passed_directly_is_extracted_next_to_it(tmp_path):
    _make_zip(tmp_path / "demo.zip", {"demo-collection.json": '{"a": 1}'})

    resolved = resolve_collection_path(tmp_path / "demo.zip")

    assert resolved == str(tmp_path / "demo-collection.json")
    assert json.loads(Path(resolved).read_text()) == {"a": 1}


def test_existing_json_is_untouched_even_with_a_zip_beside_it(tmp_path):
    _make_zip(tmp_path / "demo.zip", {"demo-collection.json": '{"from": "zip"}'})
    target = tmp_path / "demo-collection.json"
    target.write_text('{"from": "user"}')

    assert resolve_collection_path(target) == str(target)
    assert resolve_collection_path(tmp_path / "demo.zip") == str(target)
    assert json.loads(target.read_text()) == {"from": "user"}


def test_ordinary_json_without_collection_suffix_passes_through(tmp_path):
    f = tmp_path / "mine.json"
    f.write_text("{}")
    assert resolve_collection_path(f) == str(f)


def test_neither_json_nor_zip_gives_a_one_line_error(tmp_path):
    with pytest.raises(CollectionPathError) as exc:
        resolve_collection_path(tmp_path / "nope-collection.json")
    msg = str(exc.value)
    assert "\n" not in msg
    assert str(tmp_path / "nope-collection.json") in msg
    assert "collector output" in msg


def test_zip_slip_member_is_never_written(tmp_path):
    inner = tmp_path / "sub"
    inner.mkdir()
    _make_zip(
        inner / "evil.zip", {"../evil-collection.json": "{}", "/abs/evil-collection.json": "{}"}
    )

    with pytest.raises(CollectionPathError):
        resolve_collection_path(inner / "evil-collection.json")
    with pytest.raises(CollectionPathError):
        resolve_collection_path(inner / "evil.zip")

    assert not (tmp_path / "evil-collection.json").exists()
    assert not (inner / "evil-collection.json").exists()
    assert sorted(p.name for p in inner.iterdir()) == ["evil.zip"]


def test_nested_member_is_not_taken_for_the_expected_one(tmp_path):
    _make_zip(tmp_path / "demo.zip", {"dir/demo-collection.json": "{}"})
    with pytest.raises(CollectionPathError, match="zip archive"):
        resolve_collection_path(tmp_path / "demo.zip")
    assert not (tmp_path / "demo-collection.json").exists()


def test_zip_without_expected_member_is_a_clear_error(tmp_path):
    _make_zip(tmp_path / "demo.zip", {"other.txt": "x"})
    with pytest.raises(CollectionPathError, match="zip archive without demo-collection.json"):
        resolve_collection_path(tmp_path / "demo-collection.json")


def test_oversized_member_is_refused_and_leaves_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_BYTES", "10")
    _make_zip(tmp_path / "demo.zip", {"demo-collection.json": "x" * 100})
    with pytest.raises(CollectionPathError, match="ingestion limit"):
        resolve_collection_path(tmp_path / "demo-collection.json")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["demo.zip"]


def test_corrupt_zip_is_a_clear_error(tmp_path):
    (tmp_path / "demo.zip").write_bytes(b"PK\x03\x04 not really")
    with pytest.raises(CollectionPathError):
        resolve_collection_path(tmp_path / "demo-collection.json")


def test_extracted_json_gets_normal_permissions(tmp_path):
    _make_zip(tmp_path / "demo.zip", {"demo-collection.json": "{}"})
    old = os.umask(0o022)
    try:
        resolve_collection_path(tmp_path / "demo-collection.json")
    finally:
        os.umask(old)
    assert (tmp_path / "demo-collection.json").stat().st_mode & 0o777 == 0o644


def test_newer_zip_replaces_an_older_extract(tmp_path):
    _make_zip(tmp_path / "demo.zip", {"demo-collection.json": '{"v": 2}'})
    target = tmp_path / "demo-collection.json"
    target.write_text('{"v": 1}')
    now = time.time()
    os.utime(target, (now - 100, now - 100))
    os.utime(tmp_path / "demo.zip", (now, now))

    assert resolve_collection_path(target) == str(target)
    assert json.loads(target.read_text()) == {"v": 2}


def test_newer_unrelated_zip_leaves_the_file_alone(tmp_path):
    _make_zip(tmp_path / "demo.zip", {"other.txt": "x"})
    target = tmp_path / "demo-collection.json"
    target.write_text('{"v": 1}')
    now = time.time()
    os.utime(target, (now - 100, now - 100))

    assert resolve_collection_path(target) == str(target)
    assert json.loads(target.read_text()) == {"v": 1}


def test_encrypted_zip_is_a_one_line_error(tmp_path):
    z = tmp_path / "demo.zip"
    _make_zip(z, {"demo-collection.json": "{}"})
    data = bytearray(z.read_bytes())
    data[6] |= 0x1  # local header: encrypted flag
    cd = data.index(b"PK\x01\x02")
    data[cd + 8] |= 0x1
    z.write_bytes(bytes(data))
    with pytest.raises(CollectionPathError, match="encrypted") as exc:
        resolve_collection_path(tmp_path / "demo-collection.json")
    assert "\n" not in str(exc.value)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["demo.zip"]


def test_unsupported_compression_is_a_one_line_error(tmp_path):
    z = tmp_path / "demo.zip"
    _make_zip(z, {"demo-collection.json": "{}"})
    data = bytearray(z.read_bytes())
    data[8:10] = (99).to_bytes(2, "little")  # local header compress_type
    cd = data.index(b"PK\x01\x02")
    data[cd + 10 : cd + 12] = (99).to_bytes(2, "little")
    z.write_bytes(bytes(data))
    with pytest.raises(CollectionPathError, match="compression") as exc:
        resolve_collection_path(tmp_path / "demo-collection.json")
    assert "\n" not in str(exc.value)


def test_unwritable_folder_is_a_one_line_error(tmp_path, monkeypatch):
    """Simulated, not chmod-based: root ignores directory permissions."""
    _make_zip(tmp_path / "demo.zip", {"demo-collection.json": "{}"})

    def deny(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr("src.shared.sample_collection.tempfile.mkstemp", deny)
    with pytest.raises(CollectionPathError, match="cannot write") as exc:
        resolve_collection_path(tmp_path / "demo-collection.json")
    assert "\n" not in str(exc.value)
    assert "Permission denied" in str(exc.value)
    assert not (tmp_path / "demo-collection.json").exists()


def test_test_local_phased_extracts_a_sample_on_a_fresh_clone(tmp_path):
    """Wiring: scripts/test_local_phased.py resolves the path before opening it."""
    d = tmp_path / "docs" / "examples" / "demo"
    d.mkdir(parents=True)
    _make_zip(d / "demo.zip", {"demo-collection.json": "{}"})
    proc = subprocess.run(
        ["uv", "run", "python", "scripts/test_local_phased.py", str(d / "demo-collection.json")],
        cwd=REPO,
        env={**os.environ, "ARTIFACT_DIR": str(tmp_path / "artifacts")},
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert (d / "demo-collection.json").is_file(), proc.stdout + proc.stderr
    assert "File not found" not in proc.stdout


def _fresh_clone(tmp_path: Path) -> Path:
    """A copy of the tracked sample layout only: the zip, no extracted json."""
    d = tmp_path / "docs" / "examples" / "wordpress"
    d.mkdir(parents=True)
    shutil.copy(WORDPRESS_ZIP, d / "wordpress.zip")
    assert not list(d.glob("*.json"))
    return d


def test_run_collect_on_a_fresh_clone_layout(tmp_path):
    d = _fresh_clone(tmp_path)
    env = {**os.environ, "ARTIFACT_DIR": str(tmp_path / "artifacts")}
    proc = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "scripts/run_collect.py",
            "--file",
            str(d / "wordpress-collection.json"),
            "--artifact-root",
            str(tmp_path / "artifacts"),
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    last = json.loads(proc.stdout.strip().splitlines()[-1])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert last.get("status") != "error", last
    assert (d / "wordpress-collection.json").is_file()


def test_run_collect_missing_everything_is_one_line_not_a_traceback(tmp_path):
    proc = subprocess.run(
        [
            "uv",
            "run",
            "python",
            "scripts/run_collect.py",
            "--file",
            str(tmp_path / "x-collection.json"),
            "--artifact-root",
            str(tmp_path / "artifacts"),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode != 0
    assert "Traceback" not in proc.stderr + proc.stdout
    assert "File not found" in proc.stdout


def test_phase_collect_on_a_fresh_clone_layout(tmp_path):
    from scripts import run_assessment
    from src.storage.local_store import LocalArtifactStore

    d = _fresh_clone(tmp_path)
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))
    job_id, db = run_assessment.phase_collect(str(d / "wordpress-collection.json"), None, store)
    assert db == "wordpress" and job_id
    assert (d / "wordpress-collection.json").is_file()
