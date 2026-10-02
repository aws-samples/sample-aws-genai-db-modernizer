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


def test_default_fetcher_copies_the_persisted_graph(tmp_path: Path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path / "store"))
    store.write_bytes(f"{DB}/{JOB}/graph/context.lbug", b"graph-bytes")
    dest = tmp_path / "out" / "context.lbug"

    assert ar.default_graph_fetcher(store, DB, JOB, str(dest)) is True
    assert dest.read_bytes() == b"graph-bytes"


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


def test_default_fetcher_persists_the_rebuilt_graph(tmp_path: Path, monkeypatch) -> None:
    """Self-healing: a rebuild uploads the graph so the next read is a copy."""
    store = LocalArtifactStore(base_dir=str(tmp_path / "store"))

    def fake_rebuild(db, job, artifact_store, graph_store):
        return {}

    uploads: list[tuple] = []

    def fake_upload(self, db, job, local_path):
        uploads.append((db, job, local_path))

    monkeypatch.setattr("src.graph.populators.rebuild_graph", fake_rebuild)
    monkeypatch.setattr("src.graph.persistence.GraphPersistence.upload", fake_upload)
    dest = tmp_path / "out" / "context.lbug"

    assert ar.default_graph_fetcher(store, DB, JOB, str(dest)) is True
    assert uploads == [(DB, JOB, str(dest))]


def test_default_fetcher_tolerates_an_upload_failure(tmp_path: Path, monkeypatch) -> None:
    """A failed self-heal upload must not fail the fetch -- the rebuild already succeeded."""
    store = LocalArtifactStore(base_dir=str(tmp_path / "store"))

    def fake_rebuild(db, job, artifact_store, graph_store):
        return {}

    def failing_upload(self, db, job, local_path):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr("src.graph.populators.rebuild_graph", fake_rebuild)
    monkeypatch.setattr("src.graph.persistence.GraphPersistence.upload", failing_upload)
    dest = tmp_path / "out" / "context.lbug"

    assert ar.default_graph_fetcher(store, DB, JOB, str(dest)) is True


def test_default_fetcher_closes_the_store_and_skips_upload_when_rebuild_raises(
    tmp_path: Path, monkeypatch
) -> None:
    """A rebuild failure must propagate, but the GraphStore must still be closed and no
    upload attempted (there is nothing valid to persist)."""
    store = LocalArtifactStore(base_dir=str(tmp_path / "store"))
    closed: list[str] = []

    class FakeGraphStore:
        def __init__(self, db_path: str) -> None:
            self.db_path = db_path

        def close(self) -> None:
            closed.append(self.db_path)

    def raising_rebuild(db, job, artifact_store, graph_store):
        raise RuntimeError("rebuild failed")

    uploads: list[tuple] = []

    def fake_upload(self, db, job, local_path):
        uploads.append((db, job, local_path))

    monkeypatch.setattr("src.graph.GraphStore", FakeGraphStore)
    monkeypatch.setattr("src.graph.populators.rebuild_graph", raising_rebuild)
    monkeypatch.setattr("src.graph.persistence.GraphPersistence.upload", fake_upload)
    dest = tmp_path / "out" / "context.lbug"

    with pytest.raises(RuntimeError, match="rebuild failed"):
        ar.default_graph_fetcher(store, DB, JOB, str(dest))

    assert closed == [str(dest)]
    assert uploads == []


def test_synthesis_report_key_is_public_and_prefers_highest_version(tmp_path: Path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(f"{DB}/{JOB}/synthesis/v1/report.json", {})
    store.write_json(f"{DB}/{JOB}/synthesis/v2/report.json", {})
    assert ar.synthesis_report_key(store, DB, JOB, 0) == f"{DB}/{JOB}/synthesis/v2/report.json"
