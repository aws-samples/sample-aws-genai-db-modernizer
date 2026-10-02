"""The analysis report reads journeys from the context graph on any store."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.report import analysis_report as ar
from src.storage.local_store import LocalArtifactStore

DB, JOB = "wordpress", "job-001"


def test_injected_fetcher_returning_false_yields_none(tmp_path: Path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    got = ar._read_journeys_from_graph(store, DB, JOB, graph_fetcher=lambda *_: False)
    assert got is None


def test_injected_fetcher_that_raises_yields_none(tmp_path: Path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))

    def boom(*_):
        raise RuntimeError("network down")

    assert ar._read_journeys_from_graph(store, DB, JOB, graph_fetcher=boom) is None


def test_default_fetcher_rebuilds_when_nothing_is_persisted(tmp_path: Path, monkeypatch) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path / "store"))
    calls: list[tuple] = []

    def fake_rebuild(db, job, artifact_store, graph_store):
        calls.append((db, job, artifact_store))
        return {}

    monkeypatch.setattr("src.graph.populators.rebuild_graph", fake_rebuild)
    dest = tmp_path / "out" / "context.lbug"

    assert ar.default_graph_fetcher(store, DB, JOB, str(dest)) is True
    assert calls == [(DB, JOB, store)]


def test_default_fetcher_closes_the_store_when_rebuild_raises(tmp_path: Path, monkeypatch) -> None:
    """A rebuild failure must propagate, but the GraphStore must still be closed."""
    store = LocalArtifactStore(base_dir=str(tmp_path / "store"))
    closed: list[str] = []

    class FakeGraphStore:
        def __init__(self, db_path: str) -> None:
            self.db_path = db_path

        def close(self) -> None:
            closed.append(self.db_path)

    def raising_rebuild(db, job, artifact_store, graph_store):
        raise RuntimeError("rebuild failed")

    monkeypatch.setattr("src.graph.GraphStore", FakeGraphStore)
    monkeypatch.setattr("src.graph.populators.rebuild_graph", raising_rebuild)
    dest = tmp_path / "out" / "context.lbug"

    with pytest.raises(RuntimeError, match="rebuild failed"):
        ar.default_graph_fetcher(store, DB, JOB, str(dest))

    assert closed == [str(dest)]


def test_default_fetcher_ignores_a_stale_header_only_graph_and_rebuilds(tmp_path: Path) -> None:
    """Regression (real bug): a persisted ``context.lbug`` that was copied without its
    ``.wal`` is an empty, un-checkpointed header -- opening it for queries used to raise
    ``Binder exception: Table Query does not exist`` and silently fall back to the
    (now-nonexistent) per-query JSON artifacts, rendering an analysis report with zero
    journeys. The fetcher must not even attempt to read that file: it always rebuilds
    straight from the JSON contracts, which are the system of record.
    """
    store = LocalArtifactStore(base_dir=str(tmp_path / "store"))

    # Minimal set of JSON artifacts rebuild_graph needs to produce >= 1 Query node --
    # see src/graph/populators.py::rebuild_graph, step 1 (collector/output.json).
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "queries": {
                "query_patterns": [
                    {
                        "query_id": "q1",
                        "query_text": "SELECT * FROM posts WHERE id = ?",
                        "query_type": "SELECT",
                        "tables_accessed": ["posts"],
                        "calls_per_second": 12.0,
                    }
                ]
            }
        },
    )

    # A stale header-only .lbug, exactly like the un-checkpointed file the local API's
    # GraphStoreCache leaves behind: 16 bytes, no .wal alongside it.
    store.write_bytes(f"{DB}/{JOB}/graph/context.lbug", b"\x00" * 16)

    journeys = ar._read_journeys(store, DB, JOB)

    assert len(journeys) >= 1
    assert journeys[0]["query_id"] == "q1"


def test_synthesis_report_key_is_public_and_prefers_highest_version(tmp_path: Path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(f"{DB}/{JOB}/synthesis/v1/report.json", {})
    store.write_json(f"{DB}/{JOB}/synthesis/v2/report.json", {})
    assert ar.synthesis_report_key(store, DB, JOB, 0) == f"{DB}/{JOB}/synthesis/v2/report.json"
