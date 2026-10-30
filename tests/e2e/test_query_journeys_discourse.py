# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#998: the query-journey pages stay within the graph engine's memory on discourse.

The results page reads every journey page at once (page 1, then pages 2..N in
parallel, see ``src/ui/src/pages/AnalysisResults.js``). On discourse (1,654
queries, 9 pages of 200) those requests used to run out of the graph's buffer
pool and answer 503. These tests drive the same burst against the real
discourse graph.
"""

from __future__ import annotations

import http.client
import json
import shutil
import socket
import subprocess  # nosec B404 -- launches this repo's own API server with fixed argv
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

from src.api.routes import graph as graph_route
from src.api.routes import query_journeys as journeys_route
from src.graph.populators import rebuild_graph
from src.graph.schema import initialize_schema
from src.graph.store import GraphStore
from src.storage.local_store import LocalArtifactStore
from tests.e2e.conftest import _terminate, _wait_for
from tests.e2e.pipeline import REPO, PipelineResult, _env

PAGE_SIZE = 200
PARALLEL = 8
ROUNDS = 10


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _copy_job(run: PipelineResult, dest: Path) -> Path:
    """A private copy of the discourse job, so the graph built here never lands in
    the shared session run other e2e tests read."""
    root = dest / "artifacts"
    shutil.copytree(run.job_dir(), root / run.db / run.job_id)
    return root


def _query_ids(run: PipelineResult) -> list[str]:
    collector = json.loads((run.job_dir() / "collector" / "output.json").read_text())
    return sorted(p["query_id"] for p in collector["queries"]["query_patterns"])


def _get(port: int, path: str) -> tuple[int, dict]:
    """The status and JSON body, or status 0 when the server dropped the connection."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=120)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read())
    except (ConnectionError, http.client.HTTPException) as exc:
        return 0, {"error": repr(exc)}
    finally:
        conn.close()


def _check_pages(pages: dict[int, dict], expected_ids: list[str]) -> None:
    """Every page answered, and together they list each query once, in id order."""
    total_pages = pages[1]["total_pages"]
    assert pages[1]["total"] == len(expected_ids)
    assert sorted(pages) == list(range(1, total_pages + 1))
    listed = [item["query_id"] for page in sorted(pages) for item in pages[page]["items"]]
    assert listed == expected_ids


def test_parallel_journey_pages_never_503_on_discourse(discourse_run, tmp_path):
    """The live API (its real wiring and default graph settings) serves the
    results page's burst of page requests, round after round, without a 503."""
    expected_ids = _query_ids(discourse_run)
    root = _copy_job(discourse_run, tmp_path)
    port = _free_port()
    env = _env()
    env["ARTIFACT_DIR"] = str(root)
    log_path = tmp_path / "api.log"
    log_file = log_path.open("w")
    proc = subprocess.Popen(  # nosec B603 -- fixed argv, this repo's own API server
        [sys.executable, "-m", "uvicorn", "src.api.main:app", "--port", str(port)],
        cwd=REPO,
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_for(port, proc, log_path)
        url = f"/api/v1/assessments/{discourse_run.job_id}/query-journeys?page_size={PAGE_SIZE}"
        status, first = _get(port, f"{url}&page=1")
        assert status == 200, first
        total_pages = first["total_pages"]
        assert total_pages > PARALLEL, "discourse should need more pages than the burst"
        failures: list[tuple[int, int, dict]] = []
        for _ in range(ROUNDS):
            with ThreadPoolExecutor(PARALLEL) as pool:
                replies = list(
                    pool.map(
                        lambda p: (p, *_get(port, f"{url}&page={p}")), range(1, total_pages + 1)
                    )
                )
            failures += [(p, s, b) for p, s, b in replies if s != 200]
            if not failures:
                _check_pages({p: b for p, _s, b in replies}, expected_ids)
        assert not failures, f"{len(failures)} failed page requests, first: {failures[0]}"
        assert proc.poll() is None, "the API process died"
    finally:
        _terminate(proc, log_file)


# A quarter of the default read buffer pool. The old all-journeys query needed
# more than this on its own (on an 18-core machine), so this checks the
# per-request work is bounded, not just that the default pool is big enough.
SMALL_POOL_BYTES = 64 * 1024 * 1024


def test_journey_pages_fit_a_small_buffer_pool(discourse_run, tmp_path, monkeypatch):
    """Parallel page requests against a read handle with a 64 MiB buffer pool all
    succeed: each request's graph work is bounded by its page, not the job size."""
    expected_ids = _query_ids(discourse_run)
    root = _copy_job(discourse_run, tmp_path)
    path = str(tmp_path / "context.lbug")
    writer = GraphStore(path)
    try:
        initialize_schema(writer)
        rebuild_graph(discourse_run.db, discourse_run.job_id, LocalArtifactStore(str(root)), writer)
    finally:
        writer.close()
    store = GraphStore(path, read_only=True, buffer_pool_size=SMALL_POOL_BYTES)

    @contextmanager
    def _lease(job_id):
        yield store, discourse_run.db

    monkeypatch.setattr(graph_route, "graph_lease", _lease)
    try:
        first = journeys_route.list_query_journeys(
            job_id=discourse_run.job_id, page=1, page_size=PAGE_SIZE
        )
        total_pages = first["total_pages"]

        def _page(page: int) -> tuple[int, dict | str]:
            try:
                return page, journeys_route.list_query_journeys(
                    job_id=discourse_run.job_id, page=page, page_size=PAGE_SIZE
                )
            except Exception as exc:  # noqa: BLE001 - reported below
                return page, repr(exc)

        for _ in range(3):
            with ThreadPoolExecutor(PARALLEL) as pool:
                replies = dict(pool.map(_page, range(1, total_pages + 1)))
            errors = {p: r for p, r in replies.items() if not isinstance(r, dict)}
            assert not errors, errors
            _check_pages(replies, expected_ids)  # type: ignore[arg-type]
    finally:
        store.close()
