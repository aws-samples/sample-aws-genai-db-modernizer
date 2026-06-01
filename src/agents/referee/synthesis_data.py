"""
Synthesis data loader — reads all pipeline artifacts via ArtifactStore.

Provides a unified view of all upstream outputs for the synthesis handler:
- Triage decisions (which engines were selected)
- Collector output (source schema, queries, metrics)
- Analysis outputs per engine (patterns, anti-patterns, costs, recommendations)
- Schema design outputs per engine (table definitions, access patterns, trade-offs)
"""

import logging
from dataclasses import dataclass, field

from src.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)


@dataclass
class EngineArtifacts:
    """All artifacts for a single target engine."""

    engine: str
    analysis: dict | None = None
    schema_design: dict | None = None
    design_trace: dict | None = None


@dataclass
class SynthesisData:
    """Unified view of all pipeline artifacts."""

    job_id: str
    database_name: str
    triage: dict = field(default_factory=dict)
    collector: dict = field(default_factory=dict)
    engines: dict[str, EngineArtifacts] = field(default_factory=dict)
    assignment: dict | None = None

    @property
    def selected_engines(self) -> list[str]:
        return [a["agent_type"] for a in self.triage.get("selected_agents", [])]

    @property
    def source_tables(self) -> list[dict]:  # type: ignore[type-arg]
        schema = self.collector.get("database_schema", {})
        return schema.get("tables", [])  # type: ignore[no-any-return]

    @property
    def source_queries(self) -> list[dict]:  # type: ignore[type-arg]
        return self.collector.get("queries", {}).get("query_patterns", [])  # type: ignore[no-any-return]


def load_synthesis_data(
    store: ArtifactStore,
    job_id: str,
    database_name: str,
    assignment_version: int = 0,
) -> SynthesisData:
    """Load all pipeline artifacts from ArtifactStore into a unified structure.

    Reads:
      - {db}/{job}/referee-triage/triage.json
      - {db}/{job}/collector/output.json
      - {db}/{job}/analysis-{engine}/analysis.json (per selected engine)
      - {db}/{job}/schema-{engine}/schema_output.json (per selected engine, unversioned)
      - {db}/{job}/schema-{engine}/v{N}/schema_output.json (per selected engine, versioned)
      - {db}/{job}/schema-{engine}/design_trace.json (per selected engine)
      - {db}/{job}/assignment/v{N}/assignment.json (when assignment_version > 0)

    Missing artifacts are logged as warnings, not errors — the synthesis
    handler must be resilient to partial data (e.g., schema design not
    implemented for all engines).
    """
    data = SynthesisData(job_id=job_id, database_name=database_name)

    # Triage
    data.triage = _read_artifact(
        store,
        f"{database_name}/{job_id}/referee-triage/triage.json",
        required=True,
    )

    # Collector
    data.collector = _read_artifact(
        store,
        f"{database_name}/{job_id}/collector/output.json",
        required=False,
    )

    # Assignment (when versioned)
    if assignment_version > 0:
        data.assignment = _read_artifact(
            store,
            f"{database_name}/{job_id}/assignment/v{assignment_version}/assignment.json",
            required=False,
        )

    # Per-engine artifacts
    for agent_info in data.triage.get("selected_agents", []):
        engine = agent_info["agent_type"]
        artifacts = EngineArtifacts(engine=engine)

        artifacts.analysis = _read_artifact(
            store,
            f"{database_name}/{job_id}/analysis-{engine}/analysis.json",
            required=False,
        )

        # Schema design: versioned path when assignment_version > 0, else unversioned
        if assignment_version > 0:
            schema_key = (
                f"{database_name}/{job_id}/schema-{engine}/v{assignment_version}/schema_output.json"
            )
        else:
            schema_key = f"{database_name}/{job_id}/schema-{engine}/schema_output.json"

        artifacts.schema_design = _read_artifact(
            store,
            schema_key,
            required=False,
        )

        # Design trace: versioned path when assignment_version > 0, else unversioned
        if assignment_version > 0:
            trace_key = (
                f"{database_name}/{job_id}/schema-{engine}/v{assignment_version}/design_trace.json"
            )
        else:
            trace_key = f"{database_name}/{job_id}/schema-{engine}/design_trace.json"

        artifacts.design_trace = _read_artifact(
            store,
            trace_key,
            required=False,
        )

        data.engines[engine] = artifacts
        logger.info(
            "Engine %s: analysis=%s schema=%s trace=%s",
            engine,
            "yes" if artifacts.analysis else "no",
            "yes" if artifacts.schema_design else "no",
            "yes" if artifacts.design_trace else "no",
        )

    return data


def _read_artifact(store: ArtifactStore, path: str, required: bool = False) -> dict:  # type: ignore[type-arg]
    """Read a JSON artifact via ArtifactStore. Returns empty dict on failure."""
    try:
        if not store.exists(path):
            if required:
                raise RuntimeError(f"Required artifact missing: {path}")
            logger.warning("Optional artifact missing: %s", path)
            return {}
        data: dict = store.read_json(path)  # type: ignore[type-arg]
        print(f"[synthesis] Read {path}")
        return data
    except RuntimeError:
        raise
    except Exception as e:
        if required:
            raise RuntimeError(f"Required artifact missing: {path}") from e
        logger.warning("Optional artifact missing: %s — %s", path, e)
        return {}
