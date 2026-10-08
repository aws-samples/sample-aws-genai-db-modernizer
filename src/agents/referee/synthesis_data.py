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

from src.agents.referee.aurora_choice import source_database_engine
from src.agents.referee.cache_overlay import (
    CACHE_OVERLAY_ENGINES,
    apply_schema_safety_net,
    normalize_cache_owners,
    overlay_summary,
    safety_net_note,
)
from src.storage.artifact_store import ArtifactStore
from src.storage.assignment_versioning import resolve_reality_check_input_version

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
    # The assignment version Reality Check started from, when one ran for this
    # lineage (#335): lets a risk check tell "reality check moved this query"
    # apart from "this query was simply assigned here to begin with". ``None``
    # means no Reality Check run is on record for this lineage -- callers must
    # treat that as "nothing moved", not "everything moved".
    pre_reality_check_assignment: dict | None = None
    # Cache overlay safety net (#296): cached queries the cache's schema design does
    # not serve, and the notes recording that their overlay was dropped.
    cache_overlay_dropped: list[str] = field(default_factory=list)
    cache_overlay_notes: list[str] = field(default_factory=list)

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

        # Pre-Reality-Check assignment (#335): the version Reality Check started
        # from. Reading fails open to None (no run on record, or the read itself
        # fails) rather than raising -- a risk check using this is reporting-only
        # and must never block synthesis.
        try:
            input_version = resolve_reality_check_input_version(store, database_name, job_id)
        except Exception:
            logger.warning(
                "Synthesis: could not resolve the pre-Reality-Check assignment version "
                "for %s/%s",
                database_name,
                job_id,
                exc_info=True,
            )
            input_version = None
        if input_version is not None and input_version != assignment_version:
            data.pre_reality_check_assignment = _read_artifact(
                store,
                f"{database_name}/{job_id}/assignment/v{input_version}/assignment.json",
                required=False,
            )

    # Legacy ElastiCache owners (an assignment written before the cache overlay,
    # #296) move to their system-of-record engine, in memory only.
    if data.assignment:
        normalize_cache_owners(
            data.assignment,
            data.source_queries,
            data.selected_engines,
            source_database_engine(data.collector),
        )

    # Engines consolidation (or a customer re-route) eliminated — those with no
    # in-scope query in the effective assignment — are dropped here so they never
    # reach the ranking, table mappings, or schema summaries (ADR-029 Layer E).
    # Their rationale still lives in the reality-check consolidation summary. Only
    # filter when a non-empty surviving set is positively resolved; an empty set
    # means unknown/degenerate, so keep every selected engine (fail-open).
    surviving_engines: set[str] | None = None
    if data.assignment:
        surviving = {
            qa.get("assigned_engine")
            for qa in data.assignment.get("query_assignments", [])
            if qa.get("in_scope", True) and qa.get("assigned_engine")
        }
        # The cache layer owns no query; it is part of the architecture while it
        # fronts at least one in-scope query (#296).
        cache_engines = {
            qa.get("cache_engine")
            for qa in data.assignment.get("query_assignments", [])
            if qa.get("in_scope", True) and qa.get("cache_engine")
        }
        surviving_engines = (surviving | cache_engines) if surviving else None

    # Per-engine artifacts
    for agent_info in data.triage.get("selected_agents", []):
        engine = agent_info["agent_type"]
        if surviving_engines is not None and engine not in surviving_engines:
            logger.info(
                "Synthesis: skipping consolidated-away engine %s (no in-scope queries)", engine
            )
            continue
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

    _apply_cache_safety_net(data)
    return data


def _apply_cache_safety_net(data: SynthesisData) -> None:
    """Drop the overlay of cached queries the cache's design does not serve (#296).

    Only after the cache engine's schema design ran: without a design there is
    nothing to check against, and the overlay stands as assigned. The owner of a
    dropped query is unchanged. A cache left fronting nothing leaves the
    architecture.
    """
    if not data.assignment:
        return
    for engine in sorted(CACHE_OVERLAY_ENGINES & set(data.engines)):
        schema = data.engines[engine].schema_design
        # Captured before the drop so the note can say how many hot reads
        # were assigned at the assignment gate, not only how many were
        # dropped (#424): the gate's cache_overlay and this one are the same
        # shape, computed the same way, just before and after schema design.
        before = overlay_summary(
            data.assignment.get("query_assignments") or [], data.source_queries
        )
        dropped = apply_schema_safety_net(data.assignment, schema, data.source_queries)
        if not dropped:
            continue
        # Captured after the drop too, so the note's "covers N of them" / "N
        # remain" numbers are the real post-drop overlay, not before_count
        # minus len(dropped) recomputed here (and possibly wrong if something
        # else also changed the overlay in between). overlay_summary returns
        # None when nothing is cached any more (#459 round 2: dropping every
        # cached read is exactly that case) -- the zero-value fallback keeps
        # safety_net_note on its before/after branch instead of its no-snapshot
        # one, so a full drop still states the gate share and says none remain.
        after = overlay_summary(
            data.assignment.get("query_assignments") or [], data.source_queries
        ) or {"query_count": 0, "call_share_percent": 0.0}
        data.cache_overlay_dropped.extend(dropped)
        note = safety_net_note(engine, dropped, before, after)
        data.cache_overlay_notes.append(note)
        # #459 review: the full ``cache_notes`` audit trail also carries
        # customer-edit notes (full query hashes) and the legacy-migration
        # note, so it is not customer-facing -- ``cache_safety_net_notes`` is
        # the safety-net-only list every customer deliverable reads.
        notes = data.assignment.setdefault("cache_notes", [])
        if note not in notes:
            notes.append(note)
        safety_net_notes = data.assignment.setdefault("cache_safety_net_notes", [])
        if note not in safety_net_notes:
            safety_net_notes.append(note)
        logger.info("Synthesis: %s overlay dropped for %d queries", engine, len(dropped))
        if not any(
            qa.get("cache_engine") == engine and qa.get("in_scope", True)
            for qa in data.assignment.get("query_assignments", [])
        ) and not any(
            qa.get("assigned_engine") == engine
            for qa in data.assignment.get("query_assignments", [])
        ):
            del data.engines[engine]


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
