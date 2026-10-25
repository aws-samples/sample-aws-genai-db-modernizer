"""A digest of the artifacts a graph is built from (#847).

The effective assignment version says which assignment the graph was built
from, but a phase re-run on the same version (schema design, synthesis, a
load test, post-schema routing) or an in-place approval stamp rewrites an
artifact without a new version. The digest covers the content of every
artifact ``rebuild_graph`` reads, so a saved graph whose stamp differs from
the current digest is rebuilt.
"""

from __future__ import annotations

import hashlib
import re

from src.storage.artifact_store import ArtifactStore

# Keys relative to ``{db}/{job}`` of the artifacts ``rebuild_graph`` reads.
_INPUT_KEY = re.compile(
    r"^(?:"
    r"collector/output\.json"
    r"|referee-triage/triage\.json"
    r"|analysis-[^/]+/analysis\.json"
    r"|assignment/v\d+/(?:assignment|access_patterns)\.json"
    r"|reality-check/(?:v\d+/)?output\.json"
    r"|schema-[^/]+/v\d+/schema_output\.json"
    r"|post-schema-router/router_output\.json"
    r"|load-test-[^/]+/v\d+/results/summary\.json"
    r"|referee-synthesis/report\.json"
    r")$"
)


def graph_inputs_digest(store: ArtifactStore, db_name: str, job_id: str) -> str:
    """SHA-256 over the keys and bytes of the job's graph input artifacts.

    Raises when the store cannot be listed or an artifact cannot be read: the
    caller treats that as "freshness unknown".
    """
    prefix = f"{db_name}/{job_id}/"
    keys = sorted(
        key for key in store.list_prefix(prefix) if _INPUT_KEY.match(key.removeprefix(prefix))
    )
    digest = hashlib.sha256()
    for key in keys:
        digest.update(key.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(store.read_bytes(key)).digest())
    return digest.hexdigest()
