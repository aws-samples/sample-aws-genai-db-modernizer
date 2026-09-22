"""Publish and retrieve the LadybugDB ``.lbug`` graph across ATX phase processes.

The graph is the per-query read-model (replacing the ~1,654 per-query journey
artifacts). Each pipeline phase runs as a separate ATX AgentCore process, so the
graph cannot be handed over in memory or on a shared disk — it has to round-trip
through the artifact store. But the ATX store is JSON-only for its own STATE
writes (``write_bytes`` raises), and a binary ``.lbug`` is not JSON.

The write side that DOES take bytes is ``runtime.artifacts.publish`` — it uploads
a binary artifact and flips it INTERNAL -> EXTERNAL, which is also exactly what we
want for a customer-downloadable graph. The gap it leaves is discovery: a later
process cannot find that published artifact, because ``publish`` returns the
``{label: artifact_id}`` map only in-process and the store's label index lists
STATE artifacts, not EXTERNAL deliverables.

This module bridges that gap with a two-part convention:

1. ``publish_graph`` uploads ``context.lbug`` as an EXTERNAL ``OTHER`` deliverable
   (customer-downloadable) AND writes a tiny STATE JSON *pointer* recording the
   returned artifact id.
2. ``download_graph`` reads that STATE pointer (through the normal JSON store) and
   fetches the bytes by id via ``AtxArtifactStore.download_artifact_to``.

The pointer is plain STATE JSON, so it is discoverable through the store's
existing label index by any later phase or the read-side API — no new discovery
mechanism, no change to how the store lists artifacts.
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from src.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)

# STATE JSON pointer recording the published .lbug's artifact id. Lives next to
# the graph key GraphPersistence uses on S3, so the two backends read parallel
# layouts. The pointer is JSON (STATE-index discoverable); the .lbug it points to
# is an EXTERNAL binary deliverable.
_POINTER_SUFFIX = "graph/context.lbug.pointer.json"

# Customer-facing label + download name for the published graph. Kept stable per
# job so it is recognizable in the Artifacts panel and overwrites cleanly.
_GRAPH_LABEL = "Assessment Context Graph"


def pointer_key(db_name: str, job_id: str) -> str:
    """STATE JSON key holding the published .lbug's artifact id."""
    return f"{db_name}/{job_id}/{_POINTER_SUFFIX}"


def build_and_publish_graph(store: ArtifactStore, db_name: str, job_id: str) -> str | None:
    """Rebuild the graph from the job's contract artifacts, then publish it.

    Called at a single-writer pipeline boundary (end of assessment-core, and at
    synthesis after the concurrent schema fan-out has joined). ``rebuild_graph``
    reads whatever contract JSON exists so far and populates a fresh local
    ``.lbug``; :func:`publish_graph` then uploads it EXTERNAL and records the
    STATE pointer. Idempotent across boundaries — a later call rebuilds from the
    now-richer artifact set and republishes, overwriting the pointer.

    Entirely best-effort: any failure (graph deps unavailable, an unreadable
    artifact, publishing unavailable outside the ATX runtime) is logged and
    swallowed. Building the read-model must never fail the phase whose real work
    (the contract JSON) already succeeded. Returns the published artifact id, or
    ``None`` when nothing was published.
    """
    import tempfile

    try:
        from src.graph import GraphStore
        from src.graph.populators import rebuild_graph
    except Exception:  # noqa: BLE001 - graph deps unavailable; skip silently
        logger.debug("graph module unavailable; skipping build_and_publish_graph", exc_info=True)
        return None

    # Build into a throwaway job-scoped dir. LadybugDB is single-file + single
    # writer; this process is the only writer at this boundary, and the dir is
    # discarded once the bytes are published.
    with tempfile.TemporaryDirectory(prefix=f"graph-{job_id}-") as tmpdir:
        local_path = str(Path(tmpdir) / "context.lbug")
        graph_store = None
        try:
            graph_store = GraphStore(local_path)
            stats = rebuild_graph(db_name, job_id, store, graph_store)
            logger.info(
                "Built context graph for %s/%s: %s nodes, %s edges (%sms)",
                db_name,
                job_id,
                stats.get("nodes_created"),
                stats.get("edges_created"),
                stats.get("duration_ms"),
            )
        except Exception:  # noqa: BLE001 - a build failure must not fail the phase
            logger.warning("context graph build failed for %s/%s", db_name, job_id, exc_info=True)
            return None
        finally:
            # Release the file lock before reading the bytes to publish.
            if graph_store is not None:
                with contextlib.suppress(Exception):
                    graph_store.close()

        return publish_graph(store, db_name, job_id, local_path)


def _graph_download_name(db_name: str, job_id: str) -> str:
    from src.atx_orchestrator.runtime.artifacts import artifact_stem

    return f"{artifact_stem(db_name, 'context-graph', job_id)}.lbug"


def publish_graph(store: ArtifactStore, db_name: str, job_id: str, local_path: str) -> str | None:
    """Publish the local ``.lbug`` as an EXTERNAL deliverable and record a pointer.

    Reads the built graph file at ``local_path``, publishes it (bytes, EXTERNAL,
    customer-downloadable) via ``runtime.artifacts.publish``, then writes a STATE
    JSON pointer through ``store`` so a later process can find it. Returns the
    published artifact id, or ``None`` when publishing was unavailable (outside
    the ATX runtime) or failed — the caller must treat the graph as best-effort
    and never fail a phase over it.
    """
    from src.atx_orchestrator.runtime.artifacts import publish

    data = Path(local_path).read_bytes()
    published = publish(
        [
            (
                data,
                "OTHER",
                _GRAPH_LABEL,
                "CUSTOMER_OUTPUT",
                _graph_download_name(db_name, job_id),
            )
        ]
    )
    artifact_id = published.get(_GRAPH_LABEL)
    if not artifact_id:
        # publish() never raises; an empty map means no ATX runtime or a rejected
        # upload. Nothing to point at — the read side falls back to a rebuild.
        logger.info(
            "graph publish unavailable/failed for %s/%s; no pointer written", db_name, job_id
        )
        return None

    try:
        store.write_json(
            pointer_key(db_name, job_id),
            {"artifact_id": artifact_id, "label": _GRAPH_LABEL, "bytes": len(data)},
        )
    except Exception:  # noqa: BLE001 - the artifact is published; a pointer write miss is non-fatal
        logger.warning(
            "graph published (id=%s) but pointer write failed for %s/%s",
            artifact_id,
            db_name,
            job_id,
            exc_info=True,
        )
        return artifact_id

    logger.info(
        "Published context graph: id=%s bytes=%d pointer=%s",
        artifact_id,
        len(data),
        pointer_key(db_name, job_id),
    )
    return artifact_id


def download_graph(store: ArtifactStore, db_name: str, job_id: str, local_path: str) -> bool:
    """Download the published ``.lbug`` to ``local_path`` using the STATE pointer.

    Returns ``False`` when no pointer exists (graph never published for this job),
    the pointer is unreadable, or the store cannot download by id — the caller then
    rebuilds from the JSON contracts. Returns ``True`` when the bytes were written.
    """
    key = pointer_key(db_name, job_id)
    try:
        if not store.exists(key):
            return False
        pointer = store.read_json(key)
    except Exception:  # noqa: BLE001 - a missing/unreadable pointer means "not available"
        logger.debug("no readable graph pointer at %s", key, exc_info=True)
        return False

    artifact_id = (pointer or {}).get("artifact_id")
    if not artifact_id:
        return False

    download = getattr(store, "download_artifact_to", None)
    if download is None:
        # Non-ATX store: it either supports read_bytes (GraphPersistence handles
        # that path) or cannot serve the graph. Either way, not this module's job.
        return False

    dest = Path(local_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        download(artifact_id, str(dest))
    except Exception:  # noqa: BLE001 - a failed fetch means "rebuild instead"
        logger.warning(
            "graph pointer %s -> id=%s but download failed for %s/%s",
            key,
            artifact_id,
            db_name,
            job_id,
            exc_info=True,
        )
        return False
    return True
