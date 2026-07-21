"""Persist the built graph (.lbug) through the artifact store abstraction.

The graph is a materialized view of S3 artifacts. Persisting the built file
lets both API tasks (and restarts) reuse it instead of each rebuilding.
"""

from __future__ import annotations

import logging
from pathlib import Path

from src.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)


class GraphPersistence:
    """Uploads/downloads the single-file .lbug graph via an ArtifactStore."""

    def __init__(self, store: ArtifactStore):
        self._store = store

    def graph_key(self, db_name: str, job_id: str) -> str:
        """Deterministic store key for an assessment's graph file."""
        return f"{db_name}/{job_id}/graph/context.lbug"

    def download_if_exists(self, db_name: str, job_id: str, local_path: str) -> bool:
        """Download the graph to local_path. Return False if it isn't stored yet."""
        key = self.graph_key(db_name, job_id)
        if not self._store.exists(key):
            return False
        data = self._store.read_bytes(key)
        dest = Path(local_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        return True

    def upload(self, db_name: str, job_id: str, local_path: str) -> None:
        """Upload the freshly built graph file to the store."""
        data = Path(local_path).read_bytes()
        self._store.write_bytes(self.graph_key(db_name, job_id), data)
