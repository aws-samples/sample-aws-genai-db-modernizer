# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A finished job's graph is built before its results page is opened (#1911).

The results page's first query-journeys read used to build the job's whole graph
inside the request: about 4 s for the discourse sample on a laptop, past the UI's
30 s request timeout on a slow runner. ``scripts/run_report.py``, the last step of a
run, now builds the command line's own copy (``graph/cli/context.lbug``, ADR-023)
through ``job_graph.prebuild``, and the API adopts a current copy instead of
building, so the API stays the only writer of the file it serves.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import subprocess  # nosec B404 -- runs this interpreter with a fixed snippet
import sys
import threading
import types
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import graph as graph_routes
from src.api.routes import query_journeys as journey_routes
from src.graph import GraphStoreCache, job_graph
from src.graph.persistence import GraphPersistence
from src.graph.store import GraphStore
from src.storage.local_store import LocalArtifactStore

REPO = Path(__file__).resolve().parents[2]
REAL_RUN = REPO / "tests" / "fixtures" / "real_runs" / "wordpress-both" / "job"
REPORT = REPO / "tests" / "unit" / "report" / "fixtures" / "e2e09_report.json"
DB, JOB = "wordpress", "job-1"
QUERIES = 107


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "artifacts"
    shutil.copytree(REAL_RUN, base / DB / JOB)
    return base


def _store(root: Path) -> LocalArtifactStore:
    return LocalArtifactStore(base_dir=str(root))


def _api(root: Path) -> str:
    return job_graph.api_graph_path(str(root), DB, JOB)


def _cli(root: Path) -> str:
    return job_graph.cli_graph_path(str(root), DB, JOB)


def _fresh(root: Path) -> tuple[int | None, str | None]:
    return job_graph.freshness(_store(root), DB, JOB)


def _restamp(path: str, version: int) -> None:
    store = GraphStore(path)
    store.execute("MATCH (m:GraphMeta) SET m.effective_version = $v", {"v": version})
    store.close()


# ---------------------------------------------------------------------------
# job_graph: prebuild and adopt
# ---------------------------------------------------------------------------


def test_prebuild_builds_the_command_lines_copy_never_the_apis_file(root):
    info = job_graph.prebuild(_store(root), str(root), DB, JOB)
    assert (info["source"], info["rebuilt"]) == ("cli", True)
    assert job_graph.is_current_graph_file(_cli(root), *_fresh(root))
    assert not Path(_api(root)).exists()


def test_prebuild_leaves_a_current_api_graph_alone(root):
    path = _api(root)
    Path(path).parent.mkdir(parents=True)
    job_graph.build_graph_file(DB, JOB, path, _store(root))
    before = os.stat(path)
    info = job_graph.prebuild(_store(root), str(root), DB, JOB)
    assert (info["source"], info["rebuilt"]) == ("api", False)
    after = os.stat(path)
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert not Path(_cli(root)).exists()


def test_a_current_copy_is_adopted_and_answers_the_same(root):
    job_graph.prebuild(_store(root), str(root), DB, JOB)
    version, digest = _fresh(root)
    target = _api(root)
    cli_inode = os.stat(_cli(root)).st_ino
    assert job_graph.adopt_cli_copy(target, version, digest) is True
    assert job_graph.is_current_graph_file(target, version, digest)
    assert os.stat(target).st_ino != cli_inode  # a copy, not a link
    store = GraphStore(target, read_only=True)
    try:
        assert store.query("MATCH (q:Query) RETURN count(q) AS n")[0]["n"] == QUERIES
    finally:
        store.close()
    assert _temporaries(root) == []


def _temporaries(root: Path) -> list[str]:
    graph_dir = Path(_api(root)).parent
    return sorted(
        str(p.relative_to(graph_dir))
        for p in graph_dir.rglob("*")
        if ".adopting" in p.name or p.name.startswith(".building")
    )


def test_the_adopted_copy_is_removed_so_the_job_keeps_one_graph(root):
    job_graph.prebuild(_store(root), str(root), DB, JOB)
    assert job_graph.adopt_cli_copy(_api(root), *_fresh(root)) is True
    assert not Path(_cli(root)).exists()
    # The CLI now reads the API's file, without building its copy again.
    info = job_graph.prebuild(_store(root), str(root), DB, JOB)
    assert (info["source"], info["rebuilt"]) == ("api", False)
    assert not Path(_cli(root)).exists()


def test_a_copy_a_cli_run_is_reading_stays(root):
    """A CLI run holds the copy's shared lock while it reads: the copy is not cut off."""
    job_graph.prebuild(_store(root), str(root), DB, JOB)
    with job_graph._file_lock(_cli(root)) as reader:
        reader.acquire("shared")
        assert job_graph.adopt_cli_copy(_api(root), *_fresh(root)) is True
        assert Path(_cli(root)).exists()


def test_on_windows_the_adopted_copy_stays_for_the_cli(root, monkeypatch):
    """The CLI never reads the API's file on Windows, so it still needs its copy."""
    job_graph.prebuild(_store(root), str(root), DB, JOB)
    fake = types.SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0, locking=lambda fd, mode, n: None)
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    monkeypatch.setattr(job_graph, "IS_WINDOWS", True)
    assert job_graph.adopt_cli_copy(_api(root), *_fresh(root)) is True
    assert Path(_cli(root)).exists()


def test_adopt_waits_for_a_cli_build_holding_the_copy(root):
    """A CLI rebuild (or ``remove_cli_copy``) holds the exclusive lock: adopt waits for it."""
    job_graph.prebuild(_store(root), str(root), DB, JOB)
    results: list[bool] = []
    with job_graph._file_lock(_cli(root)) as builder:
        builder.acquire("exclusive")
        worker = threading.Thread(
            target=lambda: results.append(job_graph.adopt_cli_copy(_api(root), *_fresh(root)))
        )
        worker.start()
        worker.join(timeout=1.0)
        assert worker.is_alive() and results == [] and not Path(_api(root)).exists()
    worker.join(timeout=30)
    assert results == [True]
    assert job_graph.is_current_graph_file(_api(root), *_fresh(root))


@pytest.mark.parametrize(
    "broken",
    ["lock", "copy", "replace"],
)
def test_a_copy_that_cannot_be_read_or_written_is_not_adopted(root, monkeypatch, caplog, broken):
    """A lock file another user owns, a full disk, a file Windows will not replace."""
    job_graph.prebuild(_store(root), str(root), DB, JOB)
    error = OSError(errno.ENOSPC if broken == "copy" else errno.EACCES, f"{broken} refused")

    def fail(*args, **kwargs):
        raise error

    target = {"lock": (job_graph, "_FileLock"), "copy": (shutil, "copyfile")}
    if broken == "replace":
        real_replace = os.replace

        def replace(src, dst):
            if ".adopting" in str(src):
                raise error
            return real_replace(src, dst)

        monkeypatch.setattr(job_graph.os, "replace", replace)
    else:
        monkeypatch.setattr(*target[broken], fail)
    assert job_graph.adopt_cli_copy(_api(root), *_fresh(root)) is False
    assert not Path(_api(root)).exists()
    assert _temporaries(root) == []
    assert f"{broken} refused" in caplog.text


def test_two_processes_adopting_one_job_each_use_their_own_temporary_file(root):
    """Two API processes on one artifact root: neither renames the other's half-written copy."""
    job_graph.prebuild(_store(root), str(root), DB, JOB)
    code = (
        "import sys\n"
        "from src.graph import job_graph\n"
        "job_graph._drop_adopted_copy = lambda cli: None  # keep the copy for every round\n"
        "path, version, digest = sys.argv[1], int(sys.argv[2]), sys.argv[3]\n"
        "ok = [job_graph.adopt_cli_copy(path, version, digest) for _ in range(25)]\n"
        "print(sum(ok))\n"
    )
    version, digest = _fresh(root)
    argv = [sys.executable, "-c", code, _api(root), str(version), str(digest)]
    procs = [
        subprocess.Popen(  # nosec B603 -- fixed argv, this interpreter
            argv, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for _ in range(2)
    ]
    outs = [proc.communicate(timeout=300) for proc in procs]
    assert [proc.returncode for proc in procs] == [0, 0], [err for _, err in outs]
    assert [out.strip() for out, _ in outs] == ["25", "25"], [err for _, err in outs]
    assert job_graph.is_current_graph_file(_api(root), version, digest)
    assert _temporaries(root) == []


def test_two_processes_building_one_job_each_use_their_own_folder(root):
    """The build's temporary file is per build too, so two builders never share one."""
    code = (
        "import sys\n"
        "from src.graph import job_graph\n"
        "from src.storage.local_store import LocalArtifactStore\n"
        "root, path = sys.argv[1], sys.argv[2]\n"
        "for _ in range(3):\n"
        "    stats = job_graph.build_graph_file(\n"
        f"        {DB!r}, {JOB!r}, path, LocalArtifactStore(base_dir=root)\n"
        "    )\n"
        "    assert stats.get('complete', True), stats\n"
        "print('ok')\n"
    )
    path = _api(root)
    Path(path).parent.mkdir(parents=True)
    argv = [sys.executable, "-c", code, str(root), path]
    procs = [
        subprocess.Popen(  # nosec B603 -- fixed argv, this interpreter
            argv, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        for _ in range(2)
    ]
    outs = [proc.communicate(timeout=600) for proc in procs]
    assert [proc.returncode for proc in procs] == [0, 0], [err[-2000:] for _, err in outs]
    assert job_graph.is_current_graph_file(path, *_fresh(root))
    assert _temporaries(root) == []


def test_adopting_drops_the_old_files_leftover_log(root):
    """A write-ahead log left next to the API's old file would be replayed onto the copy."""
    job_graph.prebuild(_store(root), str(root), DB, JOB)
    target = _api(root)
    for leftover in job_graph.sidecars(target)[1:]:
        Path(leftover).write_bytes(b"left by an interrupted writer")
    assert job_graph.adopt_cli_copy(target, *_fresh(root)) is True
    assert not any(Path(p).exists() for p in job_graph.sidecars(target)[1:])
    assert job_graph.is_current_graph_file(target, *_fresh(root))


def test_a_stale_copy_is_not_adopted(root):
    job_graph.prebuild(_store(root), str(root), DB, JOB)
    version, digest = _fresh(root)
    _restamp(_cli(root), 99)
    assert job_graph.adopt_cli_copy(_api(root), version, digest) is False
    assert not Path(_api(root)).exists()


def test_a_copy_built_from_older_artifacts_is_not_adopted(root):
    job_graph.prebuild(_store(root), str(root), DB, JOB)
    version, _ = _fresh(root)
    assert job_graph.adopt_cli_copy(_api(root), version, "another digest") is False
    assert not Path(_api(root)).exists()


def test_no_copy_means_nothing_to_adopt(root):
    target = _api(root)
    Path(target).parent.mkdir(parents=True)
    assert job_graph.adopt_cli_copy(target, *_fresh(root)) is False
    assert not Path(target).exists()


def test_a_symlinked_cli_folder_is_never_adopted(root, tmp_path):
    job_graph.prebuild(_store(root), str(root), DB, JOB)
    graph_dir = Path(_api(root)).parent
    elsewhere = tmp_path / "elsewhere"
    shutil.move(str(graph_dir / job_graph.CLI_GRAPH_DIR), elsewhere)
    os.symlink(elsewhere, graph_dir / job_graph.CLI_GRAPH_DIR)
    assert job_graph.adopt_cli_copy(_api(root), *_fresh(root)) is False
    assert not Path(_api(root)).exists()


def test_prepare_adopts_before_it_builds_and_builds_when_it_cannot(root):
    path = _api(root)
    Path(path).parent.mkdir(parents=True)
    calls: list[str] = []

    def prepare(adopted: bool) -> None:
        def step(name: str, answer: bool) -> bool:
            calls.append(name)
            return answer

        job_graph.prepare_graph_file(
            path,
            version=None,
            digest=None,
            download=lambda p: step("download", False),
            adopt=lambda p: step("adopt", adopted),
            build=lambda: step("build", True),
        )

    prepare(adopted=False)
    assert calls == ["download", "adopt", "build"]
    calls.clear()
    # An adopt that reports success without leaving a current file is not trusted.
    prepare(adopted=True)
    assert calls == ["download", "adopt", "build"]
    calls.clear()
    job_graph.prebuild(_store(root), str(root), DB, JOB)
    job_graph.adopt_cli_copy(path, None, None)
    prepare(adopted=True)
    assert calls == []  # current already: nothing to do


# ---------------------------------------------------------------------------
# The API: the first query-journeys read opens a prebuilt graph
# ---------------------------------------------------------------------------


@pytest.fixture
def api(root, monkeypatch):
    store = _store(root)
    cache = GraphStoreCache(base_dir=str(root))
    monkeypatch.setattr(graph_routes, "graph_cache", cache)
    monkeypatch.setattr(graph_routes, "artifact_store", store)
    monkeypatch.setattr(graph_routes, "graph_persistence", GraphPersistence(store))
    monkeypatch.setattr(graph_routes, "_resolve_db_name", lambda job_id: DB)
    graph_routes._rebuild_failures.clear()
    graph_routes._inputs_digests.clear()
    graph_routes._effective_versions.clear()
    builds: list[str] = []
    real_build = graph_routes._build_graph_file

    def counted(db_name, job_id, path):
        builds.append(job_id)
        return real_build(db_name, job_id, path)

    monkeypatch.setattr(graph_routes, "_build_graph_file", counted)
    app = FastAPI()
    app.include_router(journey_routes.router)
    yield TestClient(app), builds
    cache.close_all()
    graph_routes._inputs_digests.clear()
    graph_routes._effective_versions.clear()


def _first_page(client: TestClient) -> dict:
    resp = client.get(f"/api/v1/assessments/{JOB}/query-journeys?page=1&page_size=200")
    assert resp.status_code == 200, resp.text
    body: dict = resp.json()
    return body


def test_the_first_read_of_a_prebuilt_job_opens_it_without_building(root, api):
    client, builds = api
    job_graph.prebuild(_store(root), str(root), DB, JOB)
    body = _first_page(client)
    assert body["total"] == QUERIES and len({j["query_id"] for j in body["items"]}) == QUERIES
    assert builds == []
    assert job_graph.is_current_graph_file(_api(root), *_fresh(root))


@pytest.mark.passes_on_main
def test_the_first_read_of_a_job_with_no_copy_still_builds(root, api):
    client, builds = api
    body = _first_page(client)
    assert body["total"] == QUERIES
    assert builds == [JOB]


def test_a_copy_that_cannot_be_locked_falls_back_to_a_build(root, api, monkeypatch, caplog):
    """A lock file the API cannot open (the CLI ran as another user) never breaks a read."""
    client, builds = api
    job_graph.prebuild(_store(root), str(root), DB, JOB)

    def unopenable(path):
        raise PermissionError(errno.EACCES, "Permission denied", f"{path}.lock")

    monkeypatch.setattr(job_graph, "_FileLock", unopenable)
    body = _first_page(client)
    assert body["total"] == QUERIES and body["items"]
    assert builds == [JOB]
    assert "Permission denied" in caplog.text


def test_a_stale_copy_is_rebuilt_not_served(root, api):
    client, builds = api
    job_graph.prebuild(_store(root), str(root), DB, JOB)
    _restamp(_cli(root), 99)
    assert _first_page(client)["total"] == QUERIES
    assert builds == [JOB]
    store = GraphStore(_api(root), read_only=True)
    try:
        assert store.graph_meta()["effective_version"] == _fresh(root)[0]
    finally:
        store.close()


# ---------------------------------------------------------------------------
# run_report.py prebuilds the graph
# ---------------------------------------------------------------------------


@pytest.fixture
def reported(root: Path) -> Path:
    _store(root).write_json(f"{DB}/{JOB}/synthesis/v2/report.json", json.loads(REPORT.read_text()))
    return root


def test_run_report_prebuilds_the_jobs_graph(reported):
    from scripts import run_report

    result = run_report.run(JOB, DB, str(reported), None)
    assert result["status"] == "complete", result
    assert result["graph"] == "built"
    assert job_graph.is_current_graph_file(_cli(reported), *_fresh(reported))
    assert not Path(_api(reported)).exists()


def test_a_second_report_skips_a_current_graph(reported):
    from scripts import run_report

    run_report.run(JOB, DB, str(reported), None)
    before = os.stat(_cli(reported))
    result = run_report.run(JOB, DB, str(reported), None)
    assert result["graph"] == "skipped"
    after = os.stat(_cli(reported))
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)


def test_an_incomplete_prebuild_is_reported_and_logged(reported, monkeypatch, caplog):
    from scripts import run_report

    monkeypatch.setattr(
        job_graph, "prebuild", lambda *a: {"source": "cli", "rebuilt": True, "complete": False}
    )
    result = run_report.run(JOB, DB, str(reported), None)
    assert result["status"] == "complete" and result["graph"] == "failed", result
    assert "incomplete" in caplog.text


def test_a_graph_failure_never_fails_the_report(reported, monkeypatch, caplog):
    from scripts import run_report

    def broken(*args, **kwargs):
        raise RuntimeError("graph engine unavailable")

    monkeypatch.setattr(job_graph, "prebuild", broken)
    result = run_report.run(JOB, DB, str(reported), None)
    assert result["status"] == "complete", result
    assert result["graph"] == "failed"
    assert (reported / DB / JOB / "deliverables" / "index.md").exists()
    assert "graph engine unavailable" in caplog.text
    assert "Traceback" in caplog.text  # the stack, not just the message


def test_a_report_asked_for_no_graph_builds_none(reported):
    """The help site's sample render throws its artifact store away (build_docs_sample.py)."""
    from scripts import run_report

    result = run_report.run(JOB, DB, str(reported), None, graph=False)
    assert result["status"] == "complete", result
    assert result["graph"] == "skipped"
    assert not (reported / DB / JOB / "graph").exists()


@pytest.mark.passes_on_main
def test_a_report_that_errors_builds_no_graph(root):
    from scripts import run_report

    result = run_report.run(JOB, DB, str(root), None)  # no synthesis report
    assert result["status"] == "error"
    assert not Path(_cli(root)).exists()


def test_an_open_graph_left_behind_by_a_rerun_takes_the_new_copy(root, api):
    """The UI is open while a phase re-runs: the API's open graph is behind the new
    artifacts, and the copy the re-run's report step built replaces it, unbuilt."""
    client, builds = api
    assert _first_page(client)["total"] == QUERIES
    assert builds == [JOB]
    collector = root / DB / JOB / "collector" / "output.json"
    collector.write_text(json.dumps(json.loads(collector.read_text()), indent=1))
    graph_routes._inputs_digests.clear()
    job_graph.prebuild(_store(root), str(root), DB, JOB)
    assert _first_page(client)["total"] == QUERIES
    assert builds == [JOB]
    store = GraphStore(_api(root), read_only=True)
    try:
        assert store.graph_meta()["inputs_digest"] == _fresh(root)[1]
    finally:
        store.close()
