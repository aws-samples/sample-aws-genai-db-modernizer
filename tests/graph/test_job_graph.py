# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The job graph's build and freshness logic, shared by the API and the CLI (#307).

``src/graph/job_graph.py`` holds what ``src/api/routes/graph.py`` used to keep to
itself: whether a saved graph is current, building into a temporary file and
moving it into place, and preparing a job's graph file (local, download, build).
``open_job_graph`` is the CLI's way in: it reads the API's graph read-only when
it is current and otherwise builds the CLI's own copy under a file lock, so it
never writes the file the API serves.
"""

from __future__ import annotations

import os
import shutil
import subprocess  # nosec B404 -- runs this interpreter with a fixed snippet
import sys
import types
from pathlib import Path

import pytest

from src.graph import job_graph
from src.graph.schema import GRAPH_SCHEMA_REVISION
from src.graph.store import GraphStore
from src.storage.assignment_versioning import resolve_effective_assignment_version
from src.storage.local_store import LocalArtifactStore

REAL_RUN = Path(__file__).resolve().parents[1] / "fixtures" / "real_runs" / "wordpress-both" / "job"
DB, JOB = "wordpress", "job-1"


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "artifacts"
    shutil.copytree(REAL_RUN, base / DB / JOB)
    return base


def _store(root: Path) -> LocalArtifactStore:
    return LocalArtifactStore(base_dir=str(root))


def _api_path(root: Path) -> str:
    return job_graph.api_graph_path(str(root), DB, JOB)


def _build_api_graph(root: Path) -> str:
    path = _api_path(root)
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    stats = job_graph.build_graph_file(DB, JOB, path, _store(root))
    assert stats.get("complete", True)
    return path


def _origin(info: dict) -> tuple[str, bool]:
    return info["source"], info["rebuilt"]


def _restamp(path: str, version: int) -> None:
    store = GraphStore(path)
    store.execute("MATCH (m:GraphMeta) SET m.effective_version = $v", {"v": version})
    store.close()


# ---------------------------------------------------------------------------
# Freshness
# ---------------------------------------------------------------------------


def test_a_missing_or_corrupt_file_is_not_current(tmp_path):
    assert job_graph.is_current_graph_file(str(tmp_path / "none.lbug")) is False
    junk = tmp_path / "junk.lbug"
    junk.write_bytes(b"not a graph")
    assert job_graph.is_current_graph_file(str(junk)) is False


def test_a_fresh_build_is_current_for_its_version_and_digest(root):
    path = _build_api_graph(root)
    store = _store(root)
    version = resolve_effective_assignment_version(store, DB, JOB)
    digest = job_graph.inputs_digest(store, DB, JOB)
    assert job_graph.is_current_graph_file(path)
    assert job_graph.is_current_graph_file(path, version, digest)
    assert not job_graph.is_current_graph_file(path, version + 1, digest)
    assert not job_graph.is_current_graph_file(path, version, "other")
    meta = GraphStore(path, read_only=True)
    try:
        assert meta.graph_meta()["schema_revision"] == GRAPH_SCHEMA_REVISION
    finally:
        meta.close()


# ---------------------------------------------------------------------------
# Building
# ---------------------------------------------------------------------------


def test_build_moves_the_new_graph_into_place_and_reports_completion(root):
    path = _api_path(root)
    Path(path).parent.mkdir(parents=True)
    seen = []
    job_graph.build_graph_file(DB, JOB, path, _store(root), on_built=seen.append)
    assert seen == [True]
    assert job_graph.is_current_graph_file(path)
    assert not Path(f"{path}.building").exists()


def test_an_incomplete_build_keeps_the_previous_graph(root, monkeypatch):
    path = _build_api_graph(root)
    before = os.stat(path).st_ino

    def partial(db, job, store, writer):
        return {"complete": False}

    seen = []
    job_graph.build_graph_file(DB, JOB, path, _store(root), rebuild=partial, on_built=seen.append)
    assert seen == [False]
    assert os.stat(path).st_ino == before
    assert job_graph.is_current_graph_file(path)


def test_build_uploads_only_a_complete_graph(root):
    uploads = []

    class Persistence:
        def upload(self, db, job, path):
            uploads.append((db, job, Path(path).name))

    path = _api_path(root)
    Path(path).parent.mkdir(parents=True)
    job_graph.build_graph_file(DB, JOB, path, _store(root), Persistence())
    assert uploads == [(DB, JOB, "context.lbug")]
    job_graph.build_graph_file(
        DB, JOB, path, _store(root), Persistence(), rebuild=lambda *a: {"complete": False}
    )
    assert len(uploads) == 1


# ---------------------------------------------------------------------------
# Preparing a job's file: local -> download -> build
# ---------------------------------------------------------------------------


def test_prepare_keeps_a_current_file_and_builds_otherwise(root):
    path = _build_api_graph(root)
    calls = []
    job_graph.prepare_graph_file(
        path,
        version=None,
        digest=None,
        download=lambda p: calls.append("download") or False,
        build=lambda: calls.append("build"),
    )
    assert calls == []
    _restamp(path, 99)
    job_graph.prepare_graph_file(
        path,
        version=2,
        digest=None,
        download=lambda p: calls.append("download") or False,
        build=lambda: calls.append("build"),
    )
    assert calls == ["download", "build"]


def test_prepare_uses_a_current_download(root, tmp_path):
    fresh = _build_api_graph(root)
    target = tmp_path / "other" / "context.lbug"
    target.parent.mkdir()

    def download(p):
        shutil.copy(fresh, p)
        return True

    calls = []
    job_graph.prepare_graph_file(
        str(target), version=None, digest=None, download=download, build=lambda: calls.append(1)
    )
    assert calls == [] and job_graph.is_current_graph_file(str(target))


# ---------------------------------------------------------------------------
# The CLI's way in
# ---------------------------------------------------------------------------


def test_open_reads_a_current_api_graph_without_writing_it(root):
    path = _build_api_graph(root)
    stat = os.stat(path)
    with job_graph.open_job_graph(_store(root), str(root), DB, JOB) as (store, info):
        assert store.read_only
        assert store.query("MATCH (q:Query) RETURN count(q) AS n")[0]["n"] == 107
    assert _origin(info) == ("api", False)
    assert os.stat(path).st_ino == stat.st_ino and os.stat(path).st_mtime_ns == stat.st_mtime_ns
    assert not Path(job_graph.cli_graph_path(str(root), DB, JOB)).exists()


def test_a_stale_api_graph_is_never_rewritten_the_cli_builds_its_own(root):
    path = _build_api_graph(root)
    _restamp(path, 99)
    stat = os.stat(path)
    with job_graph.open_job_graph(_store(root), str(root), DB, JOB) as (store, info):
        assert store.read_only
        assert store.graph_meta()["effective_version"] == 2
    assert _origin(info) == ("cli", True)
    assert os.stat(path).st_ino == stat.st_ino and os.stat(path).st_mtime_ns == stat.st_mtime_ns
    with job_graph.open_job_graph(_store(root), str(root), DB, JOB) as (_, again):
        pass
    assert _origin(again) == ("cli", False)


def test_no_api_graph_builds_the_cli_copy(root):
    with job_graph.open_job_graph(_store(root), str(root), DB, JOB) as (_, info):
        pass
    assert _origin(info) == ("cli", True)
    assert not Path(_api_path(root)).exists()


def test_paths_refuse_names_that_leave_the_root(root):
    with pytest.raises(ValueError):
        job_graph.api_graph_path(str(root), "../x", JOB)
    with pytest.raises(ValueError):
        job_graph.cli_graph_path(str(root), DB, "../../etc")


def test_the_api_and_this_module_import_without_fcntl():
    """Windows has no ``fcntl``: importing the module (as the API does) must not need it."""
    code = (
        "import sys; sys.modules['fcntl'] = None\n"
        "import src.graph.job_graph, src.api.routes.graph\n"
        "print('ok')"
    )
    repo = Path(__file__).resolve().parents[2]
    proc = subprocess.run(  # nosec B603 -- fixed argv
        [sys.executable, "-c", code], cwd=repo, capture_output=True, text=True, check=False
    )
    assert proc.returncode == 0 and proc.stdout.strip() == "ok", proc.stderr


def test_on_windows_the_cli_uses_its_copy_and_msvcrt(root, monkeypatch):
    """No POSIX rename semantics: never read the API's file; lock with msvcrt."""
    _build_api_graph(root)
    calls: list[int] = []
    fake = types.SimpleNamespace(
        LK_NBLCK=2, LK_UNLCK=0, locking=lambda fd, mode, n: calls.append(mode)
    )
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setitem(sys.modules, "fcntl", None)  # a POSIX lock here would fail
    monkeypatch.setattr(job_graph, "IS_WINDOWS", True)
    with job_graph.open_job_graph(_store(root), str(root), DB, JOB) as (_, info):
        pass
    assert _origin(info) == ("cli", True)
    assert calls and set(calls) <= {fake.LK_NBLCK, fake.LK_UNLCK}


def test_an_open_api_handle_keeps_answering_while_the_api_swaps_the_file_twice(root):
    """POSIX rename semantics, the reason reading the API's file in place is safe."""
    path = _build_api_graph(root)
    count = "MATCH (q:Query) RETURN count(q) AS n"
    with job_graph.open_job_graph(_store(root), str(root), DB, JOB) as (store, info):
        assert info["source"] == "api" and info["path"] == path
        before = os.stat(path).st_ino
        for _ in range(2):
            job_graph.build_graph_file(DB, JOB, path, _store(root))
        assert os.stat(path).st_ino != before  # the file was really replaced
        assert store.query(count)[0]["n"] == 107
    assert job_graph.is_current_graph_file(path)


def test_the_copy_is_removed_once_the_api_graph_is_current_again(root):
    with job_graph.open_job_graph(_store(root), str(root), DB, JOB) as (_, info):
        assert info["source"] == "cli"
    cli = job_graph.cli_graph_path(str(root), DB, JOB)
    assert Path(cli).exists()
    _build_api_graph(root)
    with job_graph.open_job_graph(_store(root), str(root), DB, JOB) as (_, info):
        assert info["source"] == "api"
    assert not Path(cli).exists()


def test_freshness_follows_the_route_rule(root, tmp_path, monkeypatch):
    store = _store(root)
    version, digest = job_graph.freshness(store, DB, JOB)
    assert version == 2 and digest == job_graph.inputs_digest(store, DB, JOB)
    bare = tmp_path / "bare"
    (bare / DB / "job-2" / "collector").mkdir(parents=True)
    shutil.copy(root / DB / JOB / "collector" / "output.json", bare / DB / "job-2" / "collector")
    version, digest = job_graph.freshness(_store(bare), DB, "job-2")
    assert version == 0 and digest  # no assignment yet: version 0, digest still checked


def test_an_unreadable_version_skips_the_digest_as_the_route_does(root, monkeypatch):
    def fail(*args):
        raise RuntimeError("throttled")

    digests = []
    monkeypatch.setattr(job_graph, "resolve_effective_assignment_version", fail)
    monkeypatch.setattr(job_graph, "inputs_digest", lambda *a: digests.append(1) or "d")
    assert job_graph.freshness(_store(root), DB, JOB) == (None, None)
    assert digests == []


def test_a_symlinked_lock_file_is_refused(root, tmp_path):
    cli = job_graph.cli_graph_path(str(root), DB, JOB)
    Path(cli).parent.mkdir(parents=True)
    target = tmp_path / "elsewhere"
    target.write_text("")
    os.symlink(target, f"{cli}.lock")
    with pytest.raises(OSError):
        with job_graph.open_job_graph(_store(root), str(root), DB, JOB):
            pass
    assert target.read_text() == ""


def test_a_symlinked_cli_folder_is_refused(root, tmp_path):
    graph_dir = Path(job_graph.api_graph_path(str(root), DB, JOB)).parent
    graph_dir.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    os.symlink(elsewhere, graph_dir / job_graph.CLI_GRAPH_DIR)
    with pytest.raises(ValueError, match="escapes"):
        job_graph.cli_graph_path(str(root), DB, JOB)
