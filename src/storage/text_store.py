# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Stores that can also write a plain-text artifact.

The shared ``ArtifactStore`` contract has no call for writing a plain-text
artifact with an explicit content type (the per-engine Mermaid ER diagrams,
``*.mmd``). The pipeline core (``src/orchestrator/core.py``) needs it, so these
subclasses add ``write_text``.

Subclasses rather than a wrapper: these stores are passed into handlers that
type their parameter as ``ArtifactStore``. A delegating wrapper would have to
re-declare every method and would lack any method added to the base class later.
A subclass stays a true ``ArtifactStore`` and inherits additions.
"""

from __future__ import annotations

from src.storage.artifact_store import ArtifactStore
from src.storage.local_store import LocalArtifactStore
from src.storage.s3_store import S3ArtifactStore


class TextS3Store(S3ArtifactStore):
    """S3 store plus text-artifact support."""

    def write_text(self, path: str, content: str, content_type: str = "text/plain") -> None:
        self.s3.put_object(
            Bucket=self.bucket,
            Key=path,
            Body=content.encode("utf-8"),
            ContentType=content_type,
        )


class TextLocalStore(LocalArtifactStore):
    """Filesystem store plus text-artifact support."""

    def write_text(self, path: str, content: str, content_type: str = "text/plain") -> None:
        # content_type has no filesystem equivalent — accepted for call-site parity.
        del content_type
        full_path = self.base_dir / path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)


def upgrade_store(inner: ArtifactStore) -> ArtifactStore:
    """Return a text-capable equivalent of a store.

    ``create_artifact_store()`` stays authoritative on *which* backend to use and
    how it is configured; this only re-homes that choice onto the matching
    subclass.

    Raises on an unrecognised store type rather than returning it unchanged, so a
    new backend surfaces here instead of failing later at the first
    ``write_text`` call.
    """
    if isinstance(inner, S3ArtifactStore):
        return TextS3Store(inner.bucket, inner.s3)
    if isinstance(inner, LocalArtifactStore):
        return TextLocalStore(str(inner.base_dir))
    raise TypeError(
        f"No text store for {type(inner).__name__}. Add a subclass in "
        "src/storage/text_store.py that provides write_text()."
    )
