"""Graph query routes — Cypher execution and rebuild trigger."""

import logging
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
from src.graph.persistence import GraphPersistence
from src.graph.populators import rebuild_graph
from src.graph.schema import initialize_schema
from src.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/assessments", tags=["graph"])

artifact_store: ArtifactStore | None = None
graph_cache: GraphStoreCache | None = None
graph_persistence: GraphPersistence | None = None
sfn_service = None


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


def _get_graph(job_id: str):
    """Return a populated graph store: local cache -> store download -> build+upload."""
    if not graph_cache or not artifact_store or not graph_persistence:
        raise HTTPException(status_code=503, detail="Services not configured")

    db_name = _resolve_db_name(job_id)
    store = graph_cache.get(db_name, job_id)

    if store.is_populated():
        return store, db_name

    # Try the persisted copy from the store before rebuilding.
    local_path = graph_cache.local_path(db_name, job_id)
    if graph_persistence.download_if_exists(db_name, job_id, local_path):
        try:
            store = graph_cache.reopen(db_name, job_id)
            if store.is_populated():
                return store, db_name
        except Exception as exc:  # corrupt/unreadable download → fall through to rebuild
            logger.warning("downloaded graph unusable for %s/%s: %s", db_name, job_id, exc)
            store = graph_cache.get(db_name, job_id)

    # Cache miss or unusable download: build fresh, then upload (self-healing).
    initialize_schema(store)
    rebuild_graph(db_name, job_id, artifact_store, store)
    try:
        graph_persistence.upload(db_name, job_id, local_path)
    except Exception as exc:  # upload failure must not break the response
        logger.warning("graph upload failed for %s/%s: %s", db_name, job_id, exc)

    return store, db_name


def get_graph_for_job(job_id: str):
    """FastAPI dependency wrapper around _get_graph.

    Exposed as a dependency so tests can override it via
    app.dependency_overrides without patching module globals.
    """
    return _get_graph(job_id)


@router.post("/{job_id}/graph/query")
async def query_graph(job_id: str, request: CypherRequest, graph: Any = Depends(get_graph_for_job)):
    """Execute a Cypher query against the assessment's graph."""
    store, _ = graph

    try:
        results = store.query(request.cypher, request.params)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cypher error: {exc}") from exc

    columns = list(results[0].keys()) if results else []
    return {
        "columns": columns,
        "rows": results,
        "row_count": len(results),
    }


@router.post("/{job_id}/graph/rebuild")
async def rebuild_assessment_graph(job_id: str):
    """Force rebuild the graph from S3 artifacts and persist it to the store."""
    if not graph_cache or not artifact_store or not graph_persistence:
        raise HTTPException(status_code=503, detail="Services not configured")

    db_name = _get_database_name(job_id)
    store = graph_cache.get(db_name, job_id)
    initialize_schema(store)
    stats = rebuild_graph(db_name, job_id, artifact_store, store)

    try:
        graph_persistence.upload(db_name, job_id, graph_cache.local_path(db_name, job_id))
    except Exception as exc:  # upload failure must not break the response
        logger.warning("graph upload failed for %s/%s: %s", db_name, job_id, exc)

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
