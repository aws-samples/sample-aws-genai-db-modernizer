"""Storage abstraction layer for artifact read/write operations.

Provides a storage-agnostic interface backed by S3 (cloud) or filesystem (local).
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.storage.artifact_store import ArtifactStore


def create_artifact_store() -> ArtifactStore:
    """Factory: S3_BUCKET env var set → S3ArtifactStore, else LocalArtifactStore.

    Uses ARTIFACT_DIR env var for local store base directory (default: ./artifacts).
    """
    bucket = os.environ.get("S3_BUCKET")
    if bucket:
        from src.storage.s3_store import S3ArtifactStore

        return S3ArtifactStore(bucket)

    from src.storage.local_store import LocalArtifactStore

    return LocalArtifactStore(os.environ.get("ARTIFACT_DIR", "./artifacts"))
