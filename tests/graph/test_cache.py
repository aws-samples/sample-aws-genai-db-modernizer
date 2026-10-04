"""Tests for GraphStoreCache — leased read-only handles, exclusivity and LRU eviction."""

import threading
from pathlib import Path

import pytest

from src.graph import GraphStoreCache
from src.graph.store import GraphStore


def _write(path: str, node_id: str = "a") -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    store = GraphStore(path)
    store.execute("CREATE NODE TABLE IF NOT EXISTS Foo(id STRING PRIMARY KEY)")
    store.execute("MERGE (:Foo {id: $id})", {"id": node_id})
    store.close()


def _ids(store: GraphStore) -> list[str]:
    return [r["id"] for r in store.query("MATCH (f:Foo) RETURN f.id AS id ORDER BY id")]


def test_reader_prepares_once_and_reuses_the_handle(tmp_path):
    cache = GraphStoreCache(max_size=3, base_dir=str(tmp_path))
    prepared = []

    def prepare(path):
        prepared.append(path)
        _write(path)

    with cache.reader("db", "job-1", prepare) as first:
        assert first.read_only is True
    with cache.reader("db", "job-1", prepare) as second:
        assert second is first
    assert prepared == [cache.local_path("db", "job-1")]
    cache.close_all()


def test_reader_handle_rejects_writes(tmp_path):
    cache = GraphStoreCache(base_dir=str(tmp_path))
    with cache.reader("db", "job-1", _write) as store:
        with pytest.raises(RuntimeError, match="read-only"):
            store.execute("CREATE (:Foo {id: 'x'})")
    cache.close_all()


def test_different_jobs_get_different_handles(tmp_path):
    cache = GraphStoreCache(max_size=3, base_dir=str(tmp_path))
    with cache.reader("db", "job-1", _write) as one, cache.reader("db", "job-2", _write) as two:
        assert one is not two
    cache.close_all()


def test_cache_evicts_lru_when_full(tmp_path):
    cache = GraphStoreCache(max_size=2, base_dir=str(tmp_path))
    for job in ("job-1", "job-2", "job-1", "job-3"):
        with cache.reader("db", job, _write):
            pass
    assert cache.is_open("db", "job-1")
    assert not cache.is_open("db", "job-2")
    assert cache.is_open("db", "job-3")
    cache.close_all()


def test_eviction_never_closes_a_leased_handle(tmp_path):
    cache = GraphStoreCache(max_size=1, base_dir=str(tmp_path))
    with cache.reader("db", "job-1", _write) as held:
        with cache.reader("db", "job-2", _write):
            pass
        # job-1 is still leased: still open and usable despite max_size=1.
        assert _ids(held) == ["a"]
    cache.close_all()


def test_exclusive_waits_for_readers_and_closes_the_handle(tmp_path):
    cache = GraphStoreCache(base_dir=str(tmp_path))
    entered = threading.Event()
    done = threading.Event()

    def rebuild():
        with cache.exclusive("db", "job-1") as path:
            entered.set()
            _write(path, "b")  # a read-write handle: only possible with no reader open
        done.set()

    with cache.reader("db", "job-1", _write) as store:
        worker = threading.Thread(target=rebuild)
        worker.start()
        assert not entered.wait(0.3)  # blocked while the lease is active
        assert _ids(store) == ["a"]
    worker.join(10)
    assert done.is_set()
    assert not cache.is_open("db", "job-1")
    with cache.reader("db", "job-1", _write) as store:
        assert _ids(store) == ["a", "b"]
    cache.close_all()


def test_parallel_reads_and_rebuilds_do_not_deadlock(tmp_path):
    cache = GraphStoreCache(max_size=2, base_dir=str(tmp_path))
    errors: list[BaseException] = []

    def reads(job):
        try:
            for _ in range(20):
                with cache.reader("db", job, _write) as store:
                    assert store.read_only
                    assert "a" in _ids(store)
        except BaseException as exc:  # noqa: BLE001 - surfaced by the assert below
            errors.append(exc)

    def rebuilds():
        try:
            for i in range(5):
                with cache.exclusive("db", "job-1") as path:
                    _write(path, "a")
                    _write(path, f"r{i}")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [
        threading.Thread(target=reads, args=(job,)) for job in ("job-1",) * 4 + ("job-2", "job-3")
    ]
    threads.append(threading.Thread(target=rebuilds))
    for t in threads:
        t.start()
    for t in threads:
        t.join(60)
    assert not any(t.is_alive() for t in threads), "deadlock"
    assert errors == []
    cache.close_all()
