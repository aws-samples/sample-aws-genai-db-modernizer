"""The collector output as every model-facing step reads it (#1180).

``QueryPattern.query_tags`` is the caller's join key to its own code: untrusted
customer text that no design, analysis or review needs. Every step that builds a
model input (a Bedrock prompt, or a request file for an external session) reads the
collector output here, and this one place removes the tags, so no builder can pass
them on, now or later. ``collector/output.json`` keeps them for the caller. The tag
fields go; ``query_text`` is untouched, so the representative row's leading comment
still reaches prompts inside it.

``tests/unit/storage/test_collector_for_models.py`` fails when a model-facing module
(``ci/needs_llm_run.py``'s lists, plus the modules that feed them) reads the collector
output any other way, and runs the pipeline's model inputs with a marker tag on every
query to show it reaches none of them.
"""

from __future__ import annotations

from src.storage.artifact_store import ArtifactStore
from src.tools.sql.query_tags import without_query_tags

__all__ = ["collector_output_key", "read_collector_for_models"]


def collector_output_key(database_name: str, job_id: str) -> str:
    """The store key of a job's collector output."""
    return f"{database_name}/{job_id}/collector/output.json"


def read_collector_for_models(
    store: ArtifactStore, database_name: str, job_id: str, *, missing_ok: bool = False
) -> dict:
    """The job's collector output with every query pattern's tag fields removed.

    ``missing_ok`` returns ``{}`` when the job has no collector output; otherwise the
    store's own error is raised, as is a contract version error on read.
    """
    key = collector_output_key(database_name, job_id)
    if missing_ok and not store.exists(key):
        return {}
    return without_query_tags(store.read_json(key))
