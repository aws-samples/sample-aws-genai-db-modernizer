"""Graph query routes — curated read views, raw Cypher execution and rebuild trigger.

Handlers are plain ``def`` so FastAPI runs the synchronous graph calls in its
threadpool instead of on the event loop.
"""

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from src.api.models.graph_responses import (
    EngineDetailResponse,
    LoadTestResultsResponse,
    QueryProvenanceResponse,
    RiskHotspotsResponse,
    TableImpactResponse,
)
from src.graph import GraphStoreCache
from src.graph import queries as graph_queries
from src.graph.cypher_guard import DisallowedStatementError, bound_result_rows
from src.graph.persistence import GraphPersistence
from src.graph.populators import rebuild_graph
from src.graph.schema import initialize_schema
from src.graph.store import MAX_RESULT_ROWS, GraphStore
from src.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/assessments", tags=["graph"])

artifact_store: ArtifactStore | None = None
graph_cache: GraphStoreCache | None = None
graph_persistence: GraphPersistence | None = None
sfn_service = None

# POST /graph/query and POST /graph/rebuild are off unless this is set to "1";
# the curated GET endpoints are always available.
RAW_QUERY_ENV_FLAG = "MODERNIZER_ENABLE_RAW_GRAPH_QUERY"
DISABLED_DETAIL = "This endpoint is not enabled on this server."
# Largest accepted request body for POST /graph/query.
MAX_QUERY_REQUEST_BYTES = 64 * 1024


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


def _is_populated_file(path: str) -> bool:
    """True when ``path`` holds a readable graph with at least one node."""
    if not Path(path).exists():
        return False
    try:
        probe = GraphStore(path, read_only=True)
    except Exception as exc:  # corrupt/unreadable file -> caller rebuilds
        logger.warning("graph at %s unusable: %s", path, exc)
        return False
    try:
        return probe.is_populated()
    finally:
        probe.close()


def _build_graph_file(db_name: str, job_id: str, path: str) -> dict:
    """Rebuild the graph at ``path`` on a read-write handle, close it, then upload it.

    The caller must hold exclusive access to ``path`` (see GraphStoreCache).
    Closing the writer before the upload checkpoints the database file.
    """
    _, store, persistence = _services()
    writer = GraphStore(path)
    try:
        initialize_schema(writer)
        stats = rebuild_graph(db_name, job_id, store, writer)
    finally:
        writer.close()
    try:
        persistence.upload(db_name, job_id, path)
    except Exception as exc:  # upload failure must not break the response
        logger.warning("graph upload failed for %s/%s: %s", db_name, job_id, exc)
    return stats


def _prepare_graph_file(db_name: str, job_id: str, path: str) -> None:
    """Make ``path`` hold this job's graph: local file -> store download -> build+upload."""
    if _is_populated_file(path):
        return
    _, _, persistence = _services()
    if persistence.download_if_exists(db_name, job_id, path) and _is_populated_file(path):
        return
    # Cache miss or unusable download: build fresh, then upload (self-healing).
    # The local file is only a cache of the artifacts, so an unreadable copy is
    # discarded first.
    for leftover in (path, f"{path}.wal", f"{path}.shadow"):
        Path(leftover).unlink(missing_ok=True)
    _build_graph_file(db_name, job_id, path)


@contextmanager
def graph_lease(job_id: str) -> Iterator[tuple[GraphStore, str]]:
    """Lease the job's read-only graph handle for the duration of the block.

    The handle is read-only, carries the read query timeout, and is never
    closed while leased.
    """
    cache, _, _ = _services()
    db_name = _resolve_db_name(job_id)
    with cache.reader(
        db_name, job_id, lambda path: _prepare_graph_file(db_name, job_id, path)
    ) as store:
        yield store, db_name


def get_graph_for_job(job_id: str) -> Iterator[tuple[GraphStore, str]]:
    """FastAPI dependency: lease the job's graph for the request.

    Exposed as a dependency so tests can override it via
    app.dependency_overrides without patching module globals.
    """
    with graph_lease(job_id) as graph:
        yield graph


def require_raw_query_enabled() -> None:
    """Dependency: refuse the raw query and rebuild endpoints unless explicitly enabled."""
    if os.environ.get(RAW_QUERY_ENV_FLAG) != "1":
        logger.info("graph endpoint refused: %s is not set to 1", RAW_QUERY_ENV_FLAG)
        raise HTTPException(status_code=403, detail=DISABLED_DETAIL)


async def read_cypher_request(request: Request) -> CypherRequest:
    """Dependency: read the JSON body with a size cap, then validate it."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > MAX_QUERY_REQUEST_BYTES:
        raise HTTPException(status_code=413, detail="Request body is too large.")
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_QUERY_REQUEST_BYTES:
            raise HTTPException(status_code=413, detail="Request body is too large.")
    try:
        return CypherRequest.model_validate_json(bytes(body))
    except ValidationError as exc:
        raise RequestValidationError(exc.errors(include_url=False, include_input=False)) from exc


@router.post(
    "/{job_id}/graph/query",
    dependencies=[Depends(require_raw_query_enabled)],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": CypherRequest.model_json_schema()}},
        }
    },
)
def query_graph(
    job_id: str,
    request: CypherRequest = Depends(read_cypher_request),
    graph: Any = Depends(get_graph_for_job),
):
    """Execute a single read-only Cypher query against the assessment's graph.

    Disabled unless MODERNIZER_ENABLE_RAW_GRAPH_QUERY=1. Only single read
    statements are accepted. The statement is given a LIMIT so the engine
    returns at most MAX_RESULT_ROWS + 1 rows; ``truncated`` is true when more
    than MAX_RESULT_ROWS were available.
    """
    store, _ = graph

    try:
        bounded = bound_result_rows(request.cypher, MAX_RESULT_ROWS)
    except DisallowedStatementError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        results, truncated = store.query_bounded(bounded, request.params, MAX_RESULT_ROWS)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Cypher error: {exc}") from exc

    columns = list(results[0].keys()) if results else []
    return {
        "columns": columns,
        "rows": results,
        "row_count": len(results),
        "truncated": truncated,
    }


@router.post("/{job_id}/graph/rebuild", dependencies=[Depends(require_raw_query_enabled)])
def rebuild_assessment_graph(job_id: str):
    """Force rebuild the graph from the artifacts and persist it to the store.

    Disabled unless MODERNIZER_ENABLE_RAW_GRAPH_QUERY=1. Reads rebuild the
    graph on demand when it is missing, so this is only needed to refresh it.
    """
    cache, _, _ = _services()
    db_name = _get_database_name(job_id)
    with cache.exclusive(db_name, job_id) as path:
        stats = _build_graph_file(db_name, job_id, path)
    return {"status": "rebuilt", **stats}


@router.get("/{job_id}/load-test-results", response_model=LoadTestResultsResponse)
def load_test_results(
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
def graph_table_impact(job_id: str, table_id: str, graph: Any = Depends(get_graph_for_job)):
    """Queries affected if the given source table changes."""
    store, _ = graph
    return graph_queries.table_impact(store, table_id)


@router.get(
    "/{job_id}/graph/queries/{query_id}/provenance",
    response_model=QueryProvenanceResponse,
)
def graph_query_provenance(job_id: str, query_id: str, graph: Any = Depends(get_graph_for_job)):
    """Why a query migrated where it did, and which agent decided it."""
    store, _ = graph
    return graph_queries.query_provenance(store, query_id)


@router.get("/{job_id}/graph/engines/{engine}", response_model=EngineDetailResponse)
def graph_engine_detail(job_id: str, engine: str, graph: Any = Depends(get_graph_for_job)):
    """Destinations and source tables migrating to a given engine."""
    store, _ = graph
    return graph_queries.engine_detail(store, engine)


@router.get("/{job_id}/graph/risks", response_model=RiskHotspotsResponse)
def graph_risks(job_id: str, graph: Any = Depends(get_graph_for_job)):
    """Tables carrying risk and anti-patterns, weighted by traffic."""
    store, _ = graph
    return graph_queries.risk_hotspots(store)
