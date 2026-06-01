"""Filesystem-backed artifact store for local development."""

from __future__ import annotations

import json
from pathlib import Path

from src.storage.artifact_store import ArtifactStore


class LocalArtifactStore(ArtifactStore):
    """Filesystem-backed artifact store for local development."""

    def __init__(self, base_dir: str = "./artifacts"):
        self.base_dir = Path(base_dir)

    def read_json(self, path: str) -> dict:
        return json.loads((self.base_dir / path).read_text())  # type: ignore[no-any-return]

    def write_json(self, path: str, data: dict) -> None:
        full_path = self.base_dir / path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(json.dumps(data, indent=2, default=str))

    def exists(self, path: str) -> bool:
        return (self.base_dir / path).exists()

    def list_prefix(self, prefix: str) -> list[str]:
        prefix_path = self.base_dir / prefix
        if not prefix_path.exists():
            return []
        return [str(p.relative_to(self.base_dir)) for p in prefix_path.rglob("*.json")]
