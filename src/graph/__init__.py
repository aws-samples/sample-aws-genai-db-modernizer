"""Context graph layer — embedded LadybugDB for assessment relationship queries."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from src.graph.store import READ_QUERY_TIMEOUT_MS, GraphStore


class GraphStoreCache:
    """LRU cache of open GraphStore instances, keyed by (db_name, job_id).

    Each key holds one handle at a time: read-only (with a query timeout) for
    serving reads, or read-write while the graph is being built. LadybugDB
    does not allow both on the same file in one process, so switching mode
    closes the existing handle first.
    """

    def __init__(self, max_size: int = 5, base_dir: str = "./artifacts"):
        self._max_size = max_size
        self._base_dir = base_dir
        self._stores: OrderedDict[tuple[str, str], GraphStore] = OrderedDict()

    def local_path(self, db_name: str, job_id: str) -> str:
        """The on-disk path where this job's .lbug lives."""
        return str(Path(self._base_dir) / db_name / job_id / "graph" / "context.lbug")

    def get(self, db_name: str, job_id: str, *, read_only: bool = False) -> GraphStore:
        """Return an open GraphStore in the requested mode, opening (and evicting) as needed.

        A read-only handle requires the database file to exist already (the
        engine raises otherwise) and carries the read query timeout.
        """
        key = (db_name, job_id)
        if key in self._stores:
            cached = self._stores[key]
            if cached.read_only == read_only:
                self._stores.move_to_end(key)
                return cached
            self.release(db_name, job_id)

        # Evict LRU if at capacity
        if len(self._stores) >= self._max_size:
            _, evict_store = self._stores.popitem(last=False)
            evict_store.close()

        db_path = self.local_path(db_name, job_id)
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        if read_only:
            store = GraphStore(db_path, read_only=True, query_timeout_ms=READ_QUERY_TIMEOUT_MS)
        else:
            store = GraphStore(db_path)
        self._stores[key] = store
        return store

    def peek(self, db_name: str, job_id: str) -> GraphStore | None:
        """Return the cached handle for this key (marking it recently used), or None."""
        key = (db_name, job_id)
        store = self._stores.get(key)
        if store is not None:
            self._stores.move_to_end(key)
        return store

    def release(self, db_name: str, job_id: str) -> None:
        """Close and forget the cached handle for this key, if any."""
        existing = self._stores.pop((db_name, job_id), None)
        if existing is not None:
            existing.close()

    def reopen(self, db_name: str, job_id: str, *, read_only: bool = False) -> GraphStore:
        """Close any cached handle and open a fresh GraphStore at local_path.

        Used after downloading a .lbug from the store so the reopened file is
        picked up instead of a stale open handle.
        """
        self.release(db_name, job_id)
        return self.get(db_name, job_id, read_only=read_only)

    def close_all(self) -> None:
        """Close all open stores. Called on API shutdown."""
        for store in self._stores.values():
            store.close()
        self._stores.clear()
