"""Transform-side storage extensions.

core-modernizer's ``ArtifactStore`` contract is used unchanged. The Transform
integration needs one capability the shared contract does not offer: writing a
plain-text artifact with an explicit content type (the per-engine Mermaid ER
diagrams, ``*.mmd``). That capability lives here rather than on the shared ABC,
so ``src/storage/`` stays byte-identical to core-modernizer's ``main``.

Why subclasses rather than a wrapper: these store instances are passed *into*
core-modernizer handlers, which type their parameter as ``ArtifactStore``. A
delegating wrapper would have to re-declare every method, and would silently
lack any method added upstream later. A subclass stays a true ``ArtifactStore``
and inherits upstream additions for free.

``main`` has since grown ``write_bytes(path, data)``. Once this branch picks
that up, ``write_text`` here can delegate to it instead of reaching for the
underlying primitives, and this module shrinks further.
"""

from __future__ import annotations

from src.storage.artifact_store import ArtifactStore
from src.storage.local_store import LocalArtifactStore
from src.storage.s3_store import S3ArtifactStore


class TransformS3Store(S3ArtifactStore):
    """S3 store plus text-artifact support for the Transform layer."""

    def write_text(self, path: str, content: str, content_type: str = "text/plain") -> None:
        self.s3.put_object(
            Bucket=self.bucket,
            Key=path,
            Body=content.encode("utf-8"),
            ContentType=content_type,
        )


class TransformLocalStore(LocalArtifactStore):
    """Filesystem store plus text-artifact support for the Transform layer."""

    def write_text(self, path: str, content: str, content_type: str = "text/plain") -> None:
        # content_type has no filesystem equivalent — accepted for call-site parity.
        del content_type
        full_path = self.base_dir / path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_text(content)


def upgrade_store(inner: ArtifactStore) -> ArtifactStore:
    """Return a text-capable equivalent of a core-modernizer store.

    core-modernizer's ``create_artifact_store()`` stays authoritative on *which*
    backend to use and how it is configured; this only re-homes that choice onto
    the matching Transform subclass.

    Raises on an unrecognised store type rather than returning it unchanged, so a
    new upstream backend surfaces here instead of failing later at the first
    ``write_text`` call.
    """
    if isinstance(inner, S3ArtifactStore):
        return TransformS3Store(inner.bucket, inner.s3)
    if isinstance(inner, LocalArtifactStore):
        return TransformLocalStore(str(inner.base_dir))
    raise TypeError(
        f"No Transform store for {type(inner).__name__}. Add a subclass in "
        "src/atx_orchestrator/store.py that provides write_text()."
    )
