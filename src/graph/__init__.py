"""Context graph layer — embedded LadybugDB for assessment relationship queries."""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from src.graph.store import READ_BUFFER_POOL_BYTES, READ_QUERY_TIMEOUT_MS, GraphStore


class _Entry:
    """Per-(db_name, job_id) state. ``cond`` guards store/readers/exclusive."""

    __slots__ = ("cond", "store", "readers", "exclusive", "pins")

    def __init__(self) -> None:
        self.cond = threading.Condition()
        self.store: GraphStore | None = None
        self.readers = 0
        self.exclusive = False
        # Threads currently holding this entry; guarded by the cache-wide lock.
        self.pins = 0


class GraphStoreCache:
    """Thread-safe LRU cache of read-only GraphStore handles, keyed by (db_name, job_id).

    LadybugDB allows only one handle per database file per process, so for
    each key the cache guarantees that at most one handle is open at a time:

    - ``reader()`` leases the shared read-only handle (opened with the read
      query timeout and buffer pool limit). When none is open it first calls
      ``prepare(local_path)`` with exclusive access to the file, so the caller
      can download or build the database there.
    - ``exclusive()`` waits until no lease is active, closes the read-only
      handle, and gives the caller sole access to the file (for a rebuild).

    A leased handle is never closed or switched while in use; LRU eviction
    only closes handles of keys that no thread currently holds.
    """

    def __init__(self, max_size: int = 5, base_dir: str = "./artifacts"):
        self._max_size = max_size
        self._base_dir = base_dir
        self._lock = threading.Lock()
        self._entries: OrderedDict[tuple[str, str], _Entry] = OrderedDict()

    def local_path(self, db_name: str, job_id: str) -> str:
        """The on-disk path where this job's .lbug lives."""
        return str(Path(self._base_dir) / db_name / job_id / "graph" / "context.lbug")

    def _pin(self, key: tuple[str, str]) -> _Entry:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                entry = _Entry()
                self._entries[key] = entry
            self._entries.move_to_end(key)
            entry.pins += 1
            return entry

    def _unpin(self, entry: _Entry) -> None:
        with self._lock:
            entry.pins -= 1
            self._evict_idle_locked()

    def _evict_idle_locked(self) -> None:
        """Close least-recently-used handles beyond max_size that nobody holds."""
        excess = len(self._entries) - self._max_size
        if excess <= 0:
            return
        for key, entry in list(self._entries.items()):
            if excess <= 0:
                break
            if entry.pins:
                continue
            if entry.store is not None:
                entry.store.close()
                entry.store = None
            del self._entries[key]
            excess -= 1

    @contextmanager
    def reader(
        self, db_name: str, job_id: str, prepare: Callable[[str], None]
    ) -> Iterator[GraphStore]:
        """Lease the read-only handle for this job, preparing the file if none is open."""
        entry = self._pin((db_name, job_id))
        try:
            with entry.cond:
                while entry.exclusive:
                    entry.cond.wait()
                if entry.store is None:
                    path = self.local_path(db_name, job_id)
                    Path(path).parent.mkdir(parents=True, exist_ok=True)
                    prepare(path)
                    entry.store = GraphStore(
                        path,
                        read_only=True,
                        query_timeout_ms=READ_QUERY_TIMEOUT_MS,
                        buffer_pool_size=READ_BUFFER_POOL_BYTES,
                    )
                entry.readers += 1
                store = entry.store
            try:
                yield store
            finally:
                with entry.cond:
                    entry.readers -= 1
                    entry.cond.notify_all()
        finally:
            self._unpin(entry)

    @contextmanager
    def exclusive(self, db_name: str, job_id: str) -> Iterator[str]:
        """Give the caller sole access to this job's database file; yields its path."""
        entry = self._pin((db_name, job_id))
        try:
            with entry.cond:
                while entry.exclusive:
                    entry.cond.wait()
                entry.exclusive = True
                while entry.readers:
                    entry.cond.wait()
                if entry.store is not None:
                    entry.store.close()
                    entry.store = None
            try:
                path = self.local_path(db_name, job_id)
                Path(path).parent.mkdir(parents=True, exist_ok=True)
                yield path
            finally:
                with entry.cond:
                    entry.exclusive = False
                    entry.cond.notify_all()
        finally:
            self._unpin(entry)

    def is_open(self, db_name: str, job_id: str) -> bool:
        """True when a read-only handle for this job is currently open."""
        with self._lock:
            entry = self._entries.get((db_name, job_id))
            return entry is not None and entry.store is not None

    def close_all(self) -> None:
        """Close all open stores. Called on API shutdown."""
        with self._lock:
            for entry in self._entries.values():
                if entry.store is not None:
                    entry.store.close()
                    entry.store = None
            self._entries.clear()
