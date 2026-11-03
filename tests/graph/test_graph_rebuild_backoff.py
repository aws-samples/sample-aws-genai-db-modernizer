# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The API route's rebuild back-off follows each build's outcome (#848, #307).

``_build_graph_file`` hands ``job_graph.build_graph_file`` an ``on_built``
callback: an incomplete rebuild (an artifact could not be read) starts this
job's back-off, so reads keep serving the previous graph instead of
rebuilding on every request; a later complete rebuild clears it.

Both tests guard behaviour main already has (the back-off predates #307), now
wired through ``on_built``: dropping that wiring fails them, so they are marked
``passes_on_main``.
"""

from __future__ import annotations

import hashlib
from unittest.mock import MagicMock

import pytest

from src.api.routes import graph as graph_routes
from src.graph import GraphStoreCache
from src.graph.schema import NODE_TABLES, stamp_graph_meta

DB, JOB = "db", "job-1"


def _rebuild(complete: bool):
    def rebuild(db_name, job_id, artifact_store, store):
        store.execute(next(ddl for ddl in NODE_TABLES if "GraphMeta" in ddl))
        stamp_graph_meta(store, inputs_digest=hashlib.sha256().hexdigest())
        return {"complete": complete}

    return rebuild


@pytest.fixture
def wired(monkeypatch, tmp_path):
    cache = GraphStoreCache(max_size=3, base_dir=str(tmp_path))
    persistence = MagicMock()
    persistence.download_if_exists.return_value = False
    monkeypatch.setattr(graph_routes, "graph_cache", cache)
    monkeypatch.setattr(graph_routes, "artifact_store", MagicMock())
    monkeypatch.setattr(graph_routes, "graph_persistence", persistence)
    monkeypatch.setattr(graph_routes, "initialize_schema", lambda store: None)
    graph_routes._rebuild_failures.clear()
    graph_routes._inputs_digests.clear()
    yield cache, persistence
    graph_routes._rebuild_failures.clear()
    cache.close_all()


@pytest.mark.passes_on_main
def test_an_incomplete_rebuild_starts_the_back_off_and_a_complete_one_clears_it(wired, monkeypatch):
    cache, persistence = wired
    assert not graph_routes._in_rebuild_backoff(DB, JOB)

    monkeypatch.setattr(graph_routes, "rebuild_graph", _rebuild(complete=False))
    with cache.exclusive(DB, JOB) as path:
        graph_routes._build_graph_file(DB, JOB, path)
    assert graph_routes._in_rebuild_backoff(DB, JOB)
    persistence.upload.assert_not_called()

    monkeypatch.setattr(graph_routes, "rebuild_graph", _rebuild(complete=True))
    with cache.exclusive(DB, JOB) as path:
        graph_routes._build_graph_file(DB, JOB, path)
    assert not graph_routes._in_rebuild_backoff(DB, JOB)
    persistence.upload.assert_called_once()


@pytest.mark.passes_on_main
def test_a_rebuild_drops_the_cached_inputs_digest(wired, monkeypatch):
    """The build digests the inputs itself: a digest cached before it must go."""
    cache, _ = wired
    graph_routes._inputs_digests[(DB, JOB)] = (0.0, "old")
    monkeypatch.setattr(graph_routes, "rebuild_graph", _rebuild(complete=True))
    with cache.exclusive(DB, JOB) as path:
        graph_routes._build_graph_file(DB, JOB, path)
    assert (DB, JOB) not in graph_routes._inputs_digests
