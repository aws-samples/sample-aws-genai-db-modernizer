"""Graph query routes — Cypher execution and rebuild trigger."""

import logging
import os
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from src.api.models.graph_responses import (
    EngineDetailResponse,
    LoadTestResultsResponse,
    QueryProvenanceResponse,
    RiskHotspotsResponse,
    TableImpactResponse,
)
from src.graph import GraphStoreCache
from src.graph import queries as graph_queries
from src.graph.cypher_guard import DisallowedStatementError, validate_read_only_cypher
from src.graph.persistence import GraphPersistence
from src.graph.populators import rebuild_graph
from src.graph.schema import initialize_schema
from src.graph.store import MAX_RESULT_ROWS
from src.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/assessments", tags=["graph"])

artifact_store: ArtifactStore | None = None
graph_cache: GraphStoreCache | None = None
graph_persistence: GraphPersistence | None = None
sfn_service = None

# The raw Cypher endpoint is off unless this is set to "1"; the curated GET
# endpoints are always available.
RAW_QUERY_ENV_FLAG = "MODERNIZER_ENABLE_RAW_GRAPH_QUERY"


class CypherRequest(BaseModel):
    cypher: str
    params: dict | None = None


def _get_database_name(job_id: str) -> str:
    """Resolve database_name from Step Functions execution input."""
    if not sfn_service:
        raise HTTPException(status_code=503, detail="Services not configured")
    execution = sfn_service.describe_execution(job_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Assessment not found")
    return execution.get("input", {}).get("database_name", "")


def _resolve_db_name(job_id: str) -> str:
    """Resolve the database name for a job (patchable indirection for tests)."""
    return _get_database_name(job_id)


def _services() -> tuple[GraphStoreCache, ArtifactStore, GraphPersistence]:
    """Return the wired graph services, or raise 503 when they are not configured."""
    if not graph_cache or not artifact_store or not graph_persistence:
        raise HTTPException(status_code=503, detail="Services not configured")
    return graph_cache, artifact_store, graph_persistence


def _open_reader(db_name: str, job_id: str):
    """Open the read-only handle for an existing local graph, or None if unusable."""
    graph_cache, _, _ = _services()
    if not Path(graph_cache.local_path(db_name, job_id)).exists():
        return None
    try:
        store = graph_cache.reopen(db_name, job_id, read_only=True)
        if store.is_populated():
            return store
    except Exception as exc:  # corrupt/unreadable file -> caller rebuilds
        logger.warning("graph unusable for %s/%s: %s", db_name, job_id, exc)
    graph_cache.release(db_name, job_id)
    return None


def _build_graph(db_name: str, job_id: str) -> dict:
    """Rebuild the graph on a read-write handle, close it, then upload it.

    Closing the writer before the upload checkpoints the database file, and
    frees the file so reads can reopen it read-only.
    """
    graph_cache, artifact_store, graph_persistence = _services()
    writer = graph_cache.reopen(db_name, job_id, read_only=False)
    try:
        initialize_schema(writer)
        stats = rebuild_graph(db_name, job_id, artifact_store, writer)
    finally:
        graph_cache.release(db_name, job_id)
    try:
        graph_persistence.upload(db_name, job_id, graph_cache.local_path(db_name, job_id))
    except Exception as exc:  # upload failure must not break the response
        logger.warning("graph upload failed for %s/%s: %s", db_name, job_id, exc)
    return stats


def _get_graph(job_id: str):
    """Return a populated read-only graph store: local -> store download -> build+upload.

    Every handle returned here is read-only and carries the read query
    timeout; building happens on a separate, short-lived read-write handle.
    """
    graph_cache, _, graph_persistence = _services()

    db_name = _resolve_db_name(job_id)
    store = graph_cache.peek(db_name, job_id)
    if store is None or not store.read_only:
        store = _open_reader(db_name, job_id)
    if store is not None:
        return store, db_name

    # Try the persisted copy from the store before rebuilding.
    local_path = graph_cache.local_path(db_name, job_id)
    graph_cache.release(db_name, job_id)
    if graph_persistence.download_if_exists(db_name, job_id, local_path):
        store = _open_reader(db_name, job_id)
        if store is not None:
            return store, db_name

    # Cache miss or unusable download: build fresh, then upload (self-healing).
    _build_graph(db_name, job_id)
    store = graph_cache.get(db_name, job_id, read_only=True)
    return store, db_name


def get_graph_for_job(job_id: str):
    """FastAPI dependency wrapper around _get_graph.

    Exposed as a dependency so tests can override it via
    app.dependency_overrides without patching module globals.
    """
    return _get_graph(job_id)


def require_raw_query_enabled() -> None:
    """Dependency: refuse the raw Cypher endpoint unless explicitly enabled."""
    if os.environ.get(RAW_QUERY_ENV_FLAG) != "1":
        raise HTTPException(
            status_code=403,
            detail=(
                "Raw graph queries are disabled on this server. Use the curated "
                f"/graph endpoints, or set {RAW_QUERY_ENV_FLAG}=1 to enable."
            ),
        )


@router.post("/{job_id}/graph/query", dependencies=[Depends(require_raw_query_enabled)])
async def query_graph(job_id: str, request: CypherRequest, graph: Any = Depends(get_graph_for_job)):
    """Execute a single read-only Cypher query against the assessment's graph.

    Disabled unless MODERNIZER_ENABLE_RAW_GRAPH_QUERY=1. Only read statements
    are accepted; results are capped at MAX_RESULT_ROWS rows (``truncated`` is
    true when more were available).
    """
    store, _ = graph

    try:
        validate_read_only_cypher(request.cypher)
    except DisallowedStatementError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        results, truncated = store.query_bounded(request.cypher, request.params, MAX_RESULT_ROWS)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cypher error: {exc}") from exc

    columns = list(results[0].keys()) if results else []
    return {
        "columns": columns,
        "rows": results,
        "row_count": len(results),
        "truncated": truncated,
    }


@router.post("/{job_id}/graph/rebuild")
async def rebuild_assessment_graph(job_id: str):
    """Force rebuild the graph from S3 artifacts and persist it to the store."""
    _services()
    db_name = _get_database_name(job_id)
    stats = _build_graph(db_name, job_id)
    return {"status": "rebuilt", **stats}


@router.get("/{job_id}/load-test-results", response_model=LoadTestResultsResponse)
async def load_test_results(
    job_id: str,
    engine: str | None = Query(default=None),
    version: int | None = Query(default=None),
    prefix: str | None = Query(default=None),
    graph: Any = Depends(get_graph_for_job),
):
    """Load test results grouped by the solution-generated access-pattern id.

    Each pattern lists the queries it consolidates, with their source and
    target latency percentiles. When version is omitted, every populated
    version is returned (the graph holds only the latest per engine).
    """
    store, _ = graph
    return graph_queries.load_test_results(
        store, job_id, engine=engine, version=version, prefix=prefix
    )


@router.get("/{job_id}/graph/tables/{table_id}/impact", response_model=TableImpactResponse)
async def graph_table_impact(job_id: str, table_id: str, graph: Any = Depends(get_graph_for_job)):
    """Queries affected if the given source table changes."""
    store, _ = graph
    return graph_queries.table_impact(store, table_id)


@router.get(
    "/{job_id}/graph/queries/{query_id}/provenance",
    response_model=QueryProvenanceResponse,
)
async def graph_query_provenance(
    job_id: str, query_id: str, graph: Any = Depends(get_graph_for_job)
):
    """Why a query migrated where it did, and which agent decided it."""
    store, _ = graph
    return graph_queries.query_provenance(store, query_id)


@router.get("/{job_id}/graph/engines/{engine}", response_model=EngineDetailResponse)
async def graph_engine_detail(job_id: str, engine: str, graph: Any = Depends(get_graph_for_job)):
    """Destinations and source tables migrating to a given engine."""
    store, _ = graph
    return graph_queries.engine_detail(store, engine)


@router.get("/{job_id}/graph/risks", response_model=RiskHotspotsResponse)
async def graph_risks(job_id: str, graph: Any = Depends(get_graph_for_job)):
    """Tables carrying risk and anti-patterns, weighted by traffic."""
    store, _ = graph
    return graph_queries.risk_hotspots(store)
