"""Filesystem-backed artifact store for local development."""

from __future__ import annotations

import json
import os
from pathlib import Path

from src.storage.artifact_store import ArtifactStore


class LocalArtifactStore(ArtifactStore):
    """Filesystem-backed artifact store for local development.

    Every method below resolves its path with ``os.path.realpath`` (follows
    symlinks, so a link inside the root that points elsewhere is caught
    too) and checks it with a single ``startswith`` call against the
    resolved base_dir (with a trailing separator baked into both sides, so
    a sibling directory whose name merely starts with the same prefix --
    e.g. ``artifacts-evil`` next to ``artifacts`` -- isn't mistaken for
    "inside" it) before touching the filesystem, then raises
    ``ValueError`` if it escapes. Callers already validate
    database_name/job_id/filename before building a key (#420/#505/#508),
    but CodeQL's py/path-injection query doesn't model a caller's
    validator as a sanitizer for this file; this is the
    ``os.path.realpath`` + ``startswith`` shape its own query help
    documents as the recognized sanitizer for exactly this query, applied
    inline at every sink rather than through a shared helper (every call
    site still got flagged behind one).
    """

    def __init__(self, base_dir: str = "./artifacts"):
        self.base_dir = Path(base_dir)

    def read_json(self, path: str) -> dict:
        base = os.path.realpath(str(self.base_dir)) + os.sep
        full_path = os.path.realpath(os.path.join(self.base_dir, path))
        if not (full_path + os.sep).startswith(base):
            raise ValueError(f"path escapes the artifact root: {path!r}")
        with open(full_path, encoding="utf-8") as f:
            return json.load(f)  # type: ignore[no-any-return]

    def write_json(self, path: str, data: dict) -> None:
        base = os.path.realpath(str(self.base_dir)) + os.sep
        full_path = os.path.realpath(os.path.join(self.base_dir, path))
        if not (full_path + os.sep).startswith(base):
            raise ValueError(f"path escapes the artifact root: {path!r}")
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2, default=str))

    def read_bytes(self, path: str) -> bytes:
        base = os.path.realpath(str(self.base_dir)) + os.sep
        full_path = os.path.realpath(os.path.join(self.base_dir, path))
        if not (full_path + os.sep).startswith(base):
            raise ValueError(f"path escapes the artifact root: {path!r}")
        with open(full_path, "rb") as f:
            return f.read()

    def write_bytes(self, path: str, data: bytes) -> None:
        base = os.path.realpath(str(self.base_dir)) + os.sep
        full_path = os.path.realpath(os.path.join(self.base_dir, path))
        if not (full_path + os.sep).startswith(base):
            raise ValueError(f"path escapes the artifact root: {path!r}")
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "wb") as f:
            f.write(data)

    def exists(self, path: str) -> bool:
        base = os.path.realpath(str(self.base_dir)) + os.sep
        full_path = os.path.realpath(os.path.join(self.base_dir, path))
        if not (full_path + os.sep).startswith(base):
            raise ValueError(f"path escapes the artifact root: {path!r}")
        return os.path.exists(full_path)

    def list_prefix(self, prefix: str) -> list[str]:
        base_dir = os.path.realpath(str(self.base_dir))
        base = base_dir + os.sep
        full_path = os.path.realpath(os.path.join(self.base_dir, prefix))
        if not (full_path + os.sep).startswith(base):
            raise ValueError(f"path escapes the artifact root: {prefix!r}")
        if not os.path.exists(full_path):
            return []
        keys = []
        for root, _dirs, files in os.walk(full_path):
            for name in files:
                if name.endswith(".json"):
                    keys.append(os.path.relpath(os.path.join(root, name), base_dir))
        return keys
