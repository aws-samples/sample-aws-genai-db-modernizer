"""Context graph layer — embedded LadybugDB for assessment relationship queries."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from src.graph.store import GraphStore


class GraphStoreCache:
    """LRU cache of open GraphStore instances, keyed by (db_name, job_id)."""

    def __init__(self, max_size: int = 5, base_dir: str = "./artifacts"):
        self._max_size = max_size
        self._base_dir = base_dir
        self._stores: OrderedDict[tuple[str, str], GraphStore] = OrderedDict()

    def get(self, db_name: str, job_id: str) -> GraphStore:
        """Return an open GraphStore, opening (and evicting) as needed."""
        key = (db_name, job_id)
        if key in self._stores:
            self._stores.move_to_end(key)
            return self._stores[key]

        # Evict LRU if at capacity
        if len(self._stores) >= self._max_size:
            _, evict_store = self._stores.popitem(last=False)
            evict_store.close()

        db_path = str(Path(self._base_dir) / db_name / job_id / "graph" / "context.lbug")
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        store = GraphStore(db_path)
        self._stores[key] = store
        return store

    def close_all(self) -> None:
        """Close all open stores. Called on API shutdown."""
        for store in self._stores.values():
            store.close()
        self._stores.clear()
