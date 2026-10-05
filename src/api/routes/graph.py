"""Graph query routes — curated read views, raw Cypher execution and rebuild trigger.

Handlers are plain ``def`` so FastAPI runs the synchronous graph calls in its
threadpool instead of on the event loop.
"""

import logging
import os
import threading
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
from src.api.services.local_execution import LocalExecutionService
from src.graph import GraphStoreCache
from src.graph import queries as graph_queries
from src.graph.cypher_guard import DisallowedStatementError, bound_result_rows
from src.graph.persistence import GraphPersistence
from src.graph.populators import rebuild_graph
from src.graph.raw_query_worker import RawQueryError, run_raw_query
from src.graph.schema import initialize_schema
from src.graph.store import MAX_RESULT_ROWS, READ_QUERY_TIMEOUT_MS, GraphStore
from src.storage.artifact_store import ArtifactStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/assessments", tags=["graph"])

artifact_store: ArtifactStore | None = None
graph_cache: GraphStoreCache | None = None
graph_persistence: GraphPersistence | None = None
sfn_service: LocalExecutionService | None = None

# POST /graph/query and POST /graph/rebuild are off unless this is set to "1";
# the curated GET endpoints are always available.
RAW_QUERY_ENV_FLAG = "MODERNIZER_ENABLE_RAW_GRAPH_QUERY"
DISABLED_DETAIL = "This endpoint is not enabled on this server."
# Largest accepted request body for POST /graph/query.
MAX_QUERY_REQUEST_BYTES = 64 * 1024
# Raw queries running at once (each in its own capped child process).
MAX_CONCURRENT_RAW_QUERIES = 2
_raw_query_slots = threading.BoundedSemaphore(MAX_CONCURRENT_RAW_QUERIES)
TIMEOUT_DETAIL = "The graph query did not finish within the time limit."
MEMORY_DETAIL = "The graph query needed more memory than is available; retry shortly."


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
    db_name: str = execution.get("input", {}).get("database_name", "")
    return db_name


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
    """Rebuild the graph into a temporary file, move it over ``path``, then upload it.

    The caller must hold exclusive access to ``path`` (see GraphStoreCache).
    Building next to the live file and renaming it into place means a failed
    or interrupted build leaves the previous graph intact, and the file never
    accumulates the history of earlier builds. Closing the writer before the
    rename checkpoints the new file.
    """
    _, store, persistence = _services()
    building = f"{path}.building"
    for leftover in _sidecars(building):
        Path(leftover).unlink(missing_ok=True)
    # Always build on a brand-new handle. Re-running the same parameterised
    # inserts on one connection after the schema is dropped and recreated makes
    # the engine silently insert nothing, so a reused writer would produce an
    # empty graph.
    writer = GraphStore(building)
    try:
        initialize_schema(writer)
        stats = rebuild_graph(db_name, job_id, store, writer)
    finally:
        writer.close()
    for leftover in _sidecars(path)[1:]:
        Path(leftover).unlink(missing_ok=True)
    os.replace(building, path)
    for leftover in _sidecars(building)[1:]:
        Path(leftover).unlink(missing_ok=True)
    try:
        persistence.upload(db_name, job_id, path)
    except Exception as exc:  # upload failure must not break the response
        logger.warning("graph upload failed for %s/%s: %s", db_name, job_id, exc)
    return stats


def _sidecars(path: str) -> tuple[str, str, str]:
    """The database file and the engine's write-ahead-log and shadow files."""
    return path, f"{path}.wal", f"{path}.shadow"


def _prepare_graph_file(db_name: str, job_id: str, path: str) -> None:
    """Make ``path`` hold this job's graph: local file -> store download -> build+upload."""
    if _is_populated_file(path):
        return
    _, _, persistence = _services()
    if persistence.download_if_exists(db_name, job_id, path) and _is_populated_file(path):
        return
    # Cache miss or unusable download: build fresh, then upload (self-healing).
    _build_graph_file(db_name, job_id, path)


def graph_error_to_http(exc: Exception) -> HTTPException | None:
    """Map an engine timeout or out-of-memory error to a neutral HTTP error."""
    message = str(exc)
    if "Interrupted" in message:
        return HTTPException(status_code=504, detail=TIMEOUT_DETAIL)
    if "Buffer manager exception" in message:
        return HTTPException(status_code=503, detail=MEMORY_DETAIL)
    return None


@contextmanager
def _curated_errors() -> Iterator[None]:
    try:
        yield
    except RuntimeError as exc:
        mapped = graph_error_to_http(exc)
        if mapped is None:
            raise
        raise mapped from exc


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
        logger.warning("graph endpoint refused: %s is not set to 1", RAW_QUERY_ENV_FLAG)
        raise HTTPException(status_code=403, detail=DISABLED_DETAIL)


def require_json_content_type(request: Request) -> None:
    """Dependency: only accept requests that declare a JSON body.

    Browsers cannot send this content type cross-origin without a CORS
    preflight, which the API only grants to its own UI origin.
    """
    media_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if media_type != "application/json":
        raise HTTPException(status_code=415, detail="Content-Type must be application/json.")


def raw_query_slot() -> Iterator[None]:
    """Dependency: hold one of the few raw-query slots for the request, or 429."""
    if not _raw_query_slots.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail="Too many graph queries are running; retry shortly.",
            headers={"Retry-After": "1"},
        )
    try:
        yield
    finally:
        _raw_query_slots.release()


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
    dependencies=[
        Depends(require_raw_query_enabled),
        Depends(require_json_content_type),
        Depends(raw_query_slot),
    ],
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
    than MAX_RESULT_ROWS were available. The query runs in a separate
    process with memory, time and result-size caps (see raw_query_worker).
    """
    store, _ = graph

    try:
        bounded = bound_result_rows(request.cypher, MAX_RESULT_ROWS)
    except DisallowedStatementError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    try:
        results, truncated = run_raw_query(
            store.path, bounded, request.params, MAX_RESULT_ROWS, READ_QUERY_TIMEOUT_MS
        )
    except RawQueryError as exc:
        if exc.kind == "timeout":
            raise HTTPException(status_code=504, detail=TIMEOUT_DETAIL) from exc
        if exc.kind == "memory":
            raise HTTPException(
                status_code=400, detail="The query needed more memory than allowed."
            ) from exc
        if exc.kind == "too_large":
            raise HTTPException(status_code=400, detail="The query result is too large.") from exc
        raise HTTPException(status_code=400, detail=f"Cypher error: {exc}") from exc

    columns = list(results[0].keys()) if results else []
    return {
        "columns": columns,
        "rows": results,
        "row_count": len(results),
        "truncated": truncated,
    }


@router.post(
    "/{job_id}/graph/rebuild",
    dependencies=[Depends(require_raw_query_enabled), Depends(require_json_content_type)],
)
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
    with _curated_errors():
        return graph_queries.load_test_results(
            store, job_id, engine=engine, version=version, prefix=prefix
        )


@router.get("/{job_id}/graph/tables/{table_id}/impact", response_model=TableImpactResponse)
def graph_table_impact(job_id: str, table_id: str, graph: Any = Depends(get_graph_for_job)):
    """Queries affected if the given source table changes."""
    store, _ = graph
    with _curated_errors():
        return graph_queries.table_impact(store, table_id)


@router.get(
    "/{job_id}/graph/queries/{query_id}/provenance",
    response_model=QueryProvenanceResponse,
)
def graph_query_provenance(job_id: str, query_id: str, graph: Any = Depends(get_graph_for_job)):
    """Why a query migrated where it did, and which agent decided it."""
    store, _ = graph
    with _curated_errors():
        return graph_queries.query_provenance(store, query_id)


@router.get("/{job_id}/graph/engines/{engine}", response_model=EngineDetailResponse)
def graph_engine_detail(job_id: str, engine: str, graph: Any = Depends(get_graph_for_job)):
    """Destinations and source tables migrating to a given engine."""
    store, _ = graph
    with _curated_errors():
        return graph_queries.engine_detail(store, engine)


@router.get("/{job_id}/graph/risks", response_model=RiskHotspotsResponse)
def graph_risks(job_id: str, graph: Any = Depends(get_graph_for_job)):
    """Tables carrying risk and anti-patterns, weighted by traffic."""
    store, _ = graph
    with _curated_errors():
        return graph_queries.risk_hotspots(store)
