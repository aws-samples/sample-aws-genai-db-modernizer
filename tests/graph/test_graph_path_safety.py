"""The saved graph's path never leaves the artifact root (#842, CodeQL py/path-injection).

``GraphStoreCache.local_path`` builds ``{base}/{db}/{job}/graph/context.lbug``
from the job id in the URL and the database name in the job's stored input.
Both parts must pass the repo's database-name and job-id rules, and the
resolved path must be exactly that literal path under the resolved root (no
symlinked database or job directory). The graph routes turn a rejected path
into a 404 (an invalid job id in the URL is the API-wide 422 from
``job_id_param``) and never open, download or build a file for it.
"""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api.routes import graph as graph_routes
from src.graph import GraphStoreCache

BAD_NAMES = [
    "..",
    "../outside",
    "../../etc",
    "/etc",
    "/tmp/outside",
    "a/b",
    "a\\b",
    "..%2Foutside",
    "%2e%2e",
    "a\x00b",
    "",
]


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_a_bad_database_name_is_refused(tmp_path, bad):
    cache = GraphStoreCache(base_dir=str(tmp_path / "root"))
    with pytest.raises(ValueError):
        cache.local_path(bad, "job-1")


@pytest.mark.parametrize("bad", BAD_NAMES)
def test_a_bad_job_id_is_refused(tmp_path, bad):
    cache = GraphStoreCache(base_dir=str(tmp_path / "root"))
    with pytest.raises(ValueError):
        cache.local_path("db", bad)


def test_a_valid_path_is_the_literal_path_under_the_resolved_root(tmp_path):
    cache = GraphStoreCache(base_dir=str(tmp_path / "root"))
    path = cache.local_path("wordpress", "job-1")
    root = os.path.realpath(tmp_path / "root")
    assert path == os.path.join(root, "wordpress", "job-1", "graph", "context.lbug")


@pytest.mark.parametrize("linked", ["db", "job"])
def test_a_symlink_out_of_the_root_is_refused(tmp_path, linked):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "db").mkdir(parents=True)
    if linked == "db":
        (root / "db").rmdir()
        (root / "db").symlink_to(outside, target_is_directory=True)
    else:
        (root / "db" / "job-1").symlink_to(outside, target_is_directory=True)
    cache = GraphStoreCache(base_dir=str(root))
    with pytest.raises(ValueError):
        cache.local_path("db", "job-1")


def test_a_symlink_inside_the_root_is_refused_too(tmp_path):
    """A link that lands back inside the root still points at the wrong job."""
    root = tmp_path / "root"
    (root / "db" / "other-job").mkdir(parents=True)
    (root / "db" / "job-1").symlink_to(root / "db" / "other-job", target_is_directory=True)
    with pytest.raises(ValueError):
        GraphStoreCache(base_dir=str(root)).local_path("db", "job-1")


@pytest.fixture
def served(monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    cache = GraphStoreCache(base_dir=str(root))
    persistence = MagicMock()
    persistence.download_if_exists.return_value = False
    rebuilds: list[str] = []
    opened: list[str] = []
    monkeypatch.setattr(graph_routes, "graph_cache", cache)
    monkeypatch.setattr(graph_routes, "artifact_store", MagicMock())
    monkeypatch.setattr(graph_routes, "graph_persistence", persistence)
    monkeypatch.setattr(graph_routes, "rebuild_graph", lambda *a: rebuilds.append(a[1]) or {})
    monkeypatch.setattr(
        graph_routes, "GraphStore", lambda path, **kw: opened.append(path) or MagicMock()
    )
    db_name = {"value": "db"}
    monkeypatch.setattr(graph_routes, "_resolve_db_name", lambda job_id: db_name["value"])
    monkeypatch.setattr(graph_routes, "_get_database_name", lambda job_id: db_name["value"])
    monkeypatch.setenv(graph_routes.RAW_QUERY_ENV_FLAG, "1")
    app = FastAPI()
    app.include_router(graph_routes.router)
    yield TestClient(app), db_name, tmp_path, (rebuilds, opened, persistence)
    cache.close_all()


def _untouched(spies, tmp_path: Path) -> None:
    rebuilds, opened, persistence = spies
    assert rebuilds == [] and opened == []
    persistence.download_if_exists.assert_not_called()
    assert not (tmp_path / "outside").exists() or not any((tmp_path / "outside").iterdir())


@pytest.mark.parametrize("bad", ["../outside", "/tmp/outside", "a/b", "..", "%2e%2e%2f"])
def test_a_bad_stored_database_name_is_a_404_and_reads_nothing(served, bad):
    client, db_name, tmp_path, spies = served
    db_name["value"] = bad
    resp = client.get("/api/v1/assessments/job-1/graph/queries/q1/trace")
    assert resp.status_code == 404
    resp = client.post(
        "/api/v1/assessments/job-1/graph/rebuild", headers={"content-type": "application/json"}
    )
    assert resp.status_code == 404
    _untouched(spies, tmp_path)


@pytest.mark.parametrize(
    "job",
    ["..%2F..%2Fetc", "%2e%2e", "..%5Coutside", "%2Ftmp%2Foutside", "job%00x", "..", "a.b"],
)
def test_a_bad_job_id_in_the_url_is_refused_and_reads_nothing(served, job):
    client, _, tmp_path, spies = served
    resp = client.get(f"/api/v1/assessments/{job}/graph/queries/q1/trace")
    assert resp.status_code in (404, 422), resp.status_code
    _untouched(spies, tmp_path)


def test_a_symlinked_job_directory_is_a_404_and_reads_nothing(served):
    client, _, tmp_path, spies = served
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "root" / "db").mkdir()
    (tmp_path / "root" / "db" / "job-1").symlink_to(outside, target_is_directory=True)
    resp = client.get("/api/v1/assessments/job-1/graph/queries/q1/trace")
    assert resp.status_code == 404
    _untouched(spies, tmp_path)
