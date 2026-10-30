# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#998: the read handle's memory budget, engine threads and concurrent statements."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from src.graph import GraphStoreCache
from src.graph import store as store_module
from src.graph.store import (
    DEFAULT_MAX_CONCURRENT_READS,
    DEFAULT_READ_BUFFER_POOL_MB,
    DEFAULT_READ_QUERY_THREADS,
    ENV_BUFFER_POOL_MB,
    ENV_MAX_CONCURRENT_READS,
    ENV_QUERY_THREADS,
    GraphBusyError,
    GraphStore,
    ReadLimits,
)


def _make_graph(path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    store = GraphStore(path)
    store.execute("CREATE NODE TABLE Foo(id STRING PRIMARY KEY, val INT64)")
    store.execute("UNWIND range(1, 3) AS i CREATE (:Foo {id: 'f' + string(i), val: i})")
    store.close()


@pytest.fixture
def graph_path(tmp_path):
    path = str(tmp_path / "limits.lbug")
    _make_graph(path)
    return path


class TestReadLimitsFromEnv:
    @pytest.fixture(autouse=True)
    def _clean_env(self, monkeypatch):
        for name in (ENV_BUFFER_POOL_MB, ENV_QUERY_THREADS, ENV_MAX_CONCURRENT_READS):
            monkeypatch.delenv(name, raising=False)

    def test_defaults(self):
        limits = ReadLimits.from_env()
        assert limits.buffer_pool_bytes == DEFAULT_READ_BUFFER_POOL_MB * 1024 * 1024
        assert limits.query_threads == DEFAULT_READ_QUERY_THREADS
        assert limits.max_concurrent_reads == DEFAULT_MAX_CONCURRENT_READS

    def test_values_from_env(self, monkeypatch):
        monkeypatch.setenv(ENV_BUFFER_POOL_MB, "512")
        monkeypatch.setenv(ENV_QUERY_THREADS, "1")
        monkeypatch.setenv(ENV_MAX_CONCURRENT_READS, "8")
        assert ReadLimits.from_env() == ReadLimits(512 * 1024 * 1024, 1, 8)

    @pytest.mark.parametrize("raw", ["", "abc", "0", "-4", "1.5"])
    def test_invalid_values_fall_back_to_the_default(self, monkeypatch, raw):
        monkeypatch.setenv(ENV_BUFFER_POOL_MB, raw)
        monkeypatch.setenv(ENV_QUERY_THREADS, raw)
        monkeypatch.setenv(ENV_MAX_CONCURRENT_READS, raw)
        assert ReadLimits.from_env() == ReadLimits(
            DEFAULT_READ_BUFFER_POOL_MB * 1024 * 1024,
            DEFAULT_READ_QUERY_THREADS,
            DEFAULT_MAX_CONCURRENT_READS,
        )

    def test_cache_opens_the_reader_with_the_configured_limits(self, monkeypatch, tmp_path):
        monkeypatch.setenv(ENV_BUFFER_POOL_MB, "96")
        monkeypatch.setenv(ENV_QUERY_THREADS, "3")
        monkeypatch.setenv(ENV_MAX_CONCURRENT_READS, "5")
        cache = GraphStoreCache(base_dir=str(tmp_path))
        try:
            with cache.reader("db", "job-1", _make_graph) as store:
                assert store.read_limits == ReadLimits(96 * 1024 * 1024, 3, 5)
                assert store.query("MATCH (f:Foo) RETURN count(f) AS c")[0]["c"] == 3
        finally:
            cache.close_all()


class TestReadHandle:
    def test_read_connections_use_the_configured_engine_threads(self, graph_path, monkeypatch):
        threads: list[int] = []
        real = store_module.lb.Connection

        def _spy(db, num_threads=0):
            threads.append(num_threads)
            return real(db, num_threads=num_threads)

        monkeypatch.setattr(store_module.lb, "Connection", _spy)
        store = GraphStore(graph_path, read_only=True, read_limits=ReadLimits(64 << 20, 2, 4))
        try:
            store.query("MATCH (f:Foo) RETURN f.id AS id")
            store.query_bounded("MATCH (f:Foo) RETURN f.id AS id", max_rows=1)
        finally:
            store.close()
        assert threads == [2, 2]

    def test_a_read_only_handle_is_bounded_by_default(self, graph_path):
        store = GraphStore(graph_path, read_only=True)
        try:
            assert store.read_limits == ReadLimits.from_env()
        finally:
            store.close()

    def test_statements_beyond_the_limit_wait_for_a_slot(self, graph_path):
        store = GraphStore(graph_path, read_only=True, read_limits=ReadLimits(64 << 20, 1, 2))
        held = threading.Barrier(3)
        release = threading.Event()
        running = 0
        peak = 0
        lock = threading.Lock()
        try:

            def _hold():
                nonlocal running, peak
                with store._connection():
                    with lock:
                        running += 1
                        peak = max(peak, running)
                    held.wait(timeout=10)
                    release.wait(timeout=10)
                    with lock:
                        running -= 1

            holders = [threading.Thread(target=_hold) for _ in range(2)]
            for t in holders:
                t.start()
            held.wait(timeout=10)  # both slots are now taken
            done = threading.Event()
            waiter = threading.Thread(
                target=lambda: (store.query("MATCH (f:Foo) RETURN count(f) AS c"), done.set())
            )
            waiter.start()
            assert not done.wait(0.3), "a third statement ran while two held every slot"
            release.set()
            assert done.wait(10), "the waiting statement never ran once a slot freed"
            for t in [*holders, waiter]:
                t.join(10)
            assert peak == 2
        finally:
            release.set()
            store.close()

    def test_waiting_too_long_for_a_slot_is_a_busy_error(self, graph_path, monkeypatch):
        monkeypatch.setattr(store_module, "READ_SLOT_WAIT_S", 0.05)
        store = GraphStore(graph_path, read_only=True, read_limits=ReadLimits(64 << 20, 1, 1))
        try:
            with store._connection():
                with pytest.raises(GraphBusyError):
                    store.query("MATCH (f:Foo) RETURN count(f) AS c")
            # The slot is back once the holder finishes.
            assert store.query("MATCH (f:Foo) RETURN count(f) AS c")[0]["c"] == 3
        finally:
            store.close()

    def test_a_failed_statement_gives_its_slot_back(self, graph_path):
        store = GraphStore(graph_path, read_only=True, read_limits=ReadLimits(64 << 20, 1, 1))
        try:
            for _ in range(3):
                with pytest.raises(RuntimeError):
                    store.query("MATCH (n:Missing) RETURN n")
            assert store.query("MATCH (f:Foo) RETURN count(f) AS c")[0]["c"] == 3
        finally:
            store.close()

    def test_the_writer_is_not_limited(self, tmp_path):
        writer = GraphStore(str(tmp_path / "w.lbug"))
        try:
            assert writer.read_limits is None
            writer.execute("CREATE NODE TABLE Foo(id STRING PRIMARY KEY)")
            assert writer.query("MATCH (f:Foo) RETURN count(f) AS c")[0]["c"] == 0
        finally:
            writer.close()
