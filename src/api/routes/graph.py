"""Graph query routes — curated read views, raw Cypher execution and rebuild trigger.

Handlers are plain ``def`` so FastAPI runs the synchronous graph calls in its
threadpool instead of on the event loop.
"""

import logging
import os
import threading
import time
from collections import OrderedDict
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ValidationError

from src.api.dependencies import job_id_param
from src.api.models.graph_responses import (
    EngineDetailResponse,
    LoadTestResultsResponse,
    QueryProvenanceResponse,
    QueryTraceResponse,
    RiskHotspotsResponse,
    TableImpactResponse,
)
from src.api.services.local_execution import LocalExecutionService
from src.graph import GraphStoreCache
from src.graph import queries as graph_queries
from src.graph.cypher_guard import DisallowedStatementError, bound_result_rows
from src.graph.inputs_digest import graph_inputs_digest
from src.graph.persistence import GraphPersistence
from src.graph.populators import rebuild_graph
from src.graph.raw_query_worker import RawQueryError, run_raw_query
from src.graph.schema import GRAPH_SCHEMA_REVISION, initialize_schema
from src.graph.store import MAX_RESULT_ROWS, READ_QUERY_TIMEOUT_MS, GraphBusyError, GraphStore
from src.storage.artifact_store import ArtifactStore
from src.storage.assignment_versioning import resolve_effective_assignment_version

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
BUSY_DETAIL = "Too many graph reads are running; retry shortly."
# Both are transient: the client may retry after this many seconds.
RETRY_AFTER_S = "1"


# The job's effective assignment version is cached this long (seconds) per job,
# so one results page load (several graph requests) lists the artifact store
# once rather than on every request (#819). A new version shows up on the
# first read after the entry expires.
EFFECTIVE_VERSION_TTL_S = 2.0
# A failed rebuild backs off this long (seconds) per job (#848), so reads while
# the artifact store is failing keep serving the previous graph instead of
# rebuilding on every request.
REBUILD_BACKOFF_S = 2.0
# Cap on the per-job version, failure-backoff and refresh-lock maps (#848).
MAX_TRACKED_JOBS = 128
_effective_versions: OrderedDict[tuple[str, str], tuple[float, int]] = OrderedDict()
_effective_versions_lock = threading.Lock()
# The digest of the job's graph input artifacts, cached like the version (#847).
_inputs_digests: OrderedDict[tuple[str, str], tuple[float, str]] = OrderedDict()
_inputs_digests_lock = threading.Lock()
_rebuild_failures: OrderedDict[tuple[str, str], float] = OrderedDict()
_rebuild_failures_lock = threading.Lock()
# One refresh of an open graph per job at a time, so concurrent reads after a
# new assignment version wait for one rebuild instead of each taking the
# exclusive lock to re-check.
_refresh_locks: OrderedDict[tuple[str, str], threading.Lock] = OrderedDict()
_refresh_locks_guard = threading.Lock()


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


def _is_populated_file(
    path: str, effective_version: int | None = None, inputs_digest: str | None = None
) -> bool:
    """True when ``path`` holds a readable, current graph with at least one node.

    Current means built with this code's schema revision (``GraphMeta``): a
    graph saved by an earlier release (no MOVED table, say) is stale, so the
    caller rebuilds it from the artifacts instead of serving it (ADR-023).
    When ``effective_version`` is given, the graph must also have been built
    from that assignment version: a graph saved before a customer edit or any
    later assignment version is stale too (#819). When ``inputs_digest`` is
    given, it must match the digest of the artifacts the graph was built from:
    a phase re-run on the same version rewrites artifacts without a new
    version (#847).
    """
    if not Path(path).exists():
        return False
    try:
        probe = GraphStore(path, read_only=True)
    except Exception as exc:  # corrupt/unreadable file -> caller rebuilds
        logger.warning("graph at %s unusable: %s", path, exc)
        return False
    try:
        if not probe.is_populated():
            return False
        meta = probe.graph_meta()
        revision = meta["schema_revision"]
        if revision != GRAPH_SCHEMA_REVISION:
            logger.info(
                "graph at %s has schema revision %s, not %s: rebuilding",
                path,
                revision,
                GRAPH_SCHEMA_REVISION,
            )
            return False
        if effective_version is not None and meta["effective_version"] != effective_version:
            logger.info(
                "graph at %s was built from assignment v%s, not v%s: rebuilding",
                path,
                meta["effective_version"],
                effective_version,
            )
            return False
        if inputs_digest is not None and meta["inputs_digest"] != inputs_digest:
            logger.info("graph at %s was built from older artifacts: rebuilding", path)
            return False
        return True
    finally:
        probe.close()


def _inputs_digest(db_name: str, job_id: str) -> str | None:
    """Digest of the job's graph input artifacts (#847), cached like the effective version.

    None when the store cannot be read: the caller serves the graph it has.
    """
    key = (db_name, job_id)
    now = time.monotonic()
    with _inputs_digests_lock:
        hit = _inputs_digests.get(key)
        if hit is not None:
            if 0 <= now - hit[0] < EFFECTIVE_VERSION_TTL_S:
                _inputs_digests.move_to_end(key)
                return hit[1]
            del _inputs_digests[key]
    _, store, _ = _services()
    try:
        digest = graph_inputs_digest(store, db_name, job_id)
    except Exception as exc:  # noqa: BLE001 - never fail a read on the freshness check
        logger.warning(
            "could not digest the graph inputs of %s/%s, serving the current graph: %s",
            db_name,
            job_id,
            exc,
        )
        return None
    with _inputs_digests_lock:
        for old_key, (ts, _) in list(_inputs_digests.items()):
            if not (0 <= now - ts < EFFECTIVE_VERSION_TTL_S):
                del _inputs_digests[old_key]
        _inputs_digests[key] = (now, digest)
        _inputs_digests.move_to_end(key)
        while len(_inputs_digests) > MAX_TRACKED_JOBS:
            _inputs_digests.popitem(last=False)
    return digest


def _effective_assignment_version(db_name: str, job_id: str) -> int | None:
    """The job's effective assignment version (ADR-028; 0 when there is none).

    Cached per job for ``EFFECTIVE_VERSION_TTL_S``. None when the artifact
    store cannot be listed (a throttled or failing store): the caller serves
    the graph it has, and the next read checks again (a failure is not cached).
    """
    key = (db_name, job_id)
    now = time.monotonic()
    with _effective_versions_lock:
        hit = _effective_versions.get(key)
        if hit is not None:
            if 0 <= now - hit[0] < EFFECTIVE_VERSION_TTL_S:
                _effective_versions.move_to_end(key)
                return hit[1]
            del _effective_versions[key]
    _, store, _ = _services()
    try:
        version = resolve_effective_assignment_version(store, db_name, job_id)
    except Exception as exc:  # noqa: BLE001 - never fail a read on the freshness check
        logger.warning(
            "could not read the effective assignment version of %s/%s, serving the "
            "current graph: %s",
            db_name,
            job_id,
            exc,
        )
        return None
    with _effective_versions_lock:
        for old_key, (ts, _) in list(_effective_versions.items()):
            if not (0 <= now - ts < EFFECTIVE_VERSION_TTL_S):
                del _effective_versions[old_key]
        _effective_versions[key] = (now, version)
        _effective_versions.move_to_end(key)
        while len(_effective_versions) > MAX_TRACKED_JOBS:
            _effective_versions.popitem(last=False)
    return version


def _in_rebuild_backoff(db_name: str, job_id: str) -> bool:
    """True when a rebuild for this job failed within ``REBUILD_BACKOFF_S``."""
    key = (db_name, job_id)
    now = time.monotonic()
    with _rebuild_failures_lock:
        failed_at = _rebuild_failures.get(key)
        if failed_at is None:
            return False
        if 0 <= now - failed_at < REBUILD_BACKOFF_S:
            return True
        del _rebuild_failures[key]
        return False


def _record_rebuild_failure(db_name: str, job_id: str) -> None:
    key = (db_name, job_id)
    now = time.monotonic()
    with _rebuild_failures_lock:
        for old_key, ts in list(_rebuild_failures.items()):
            if not (0 <= now - ts < REBUILD_BACKOFF_S):
                del _rebuild_failures[old_key]
        _rebuild_failures[key] = now
        _rebuild_failures.move_to_end(key)
        while len(_rebuild_failures) > MAX_TRACKED_JOBS:
            _rebuild_failures.popitem(last=False)


def _clear_rebuild_failure(db_name: str, job_id: str) -> None:
    with _rebuild_failures_lock:
        _rebuild_failures.pop((db_name, job_id), None)


def _build_graph_file(db_name: str, job_id: str, path: str) -> dict:
    """Rebuild the graph into a temporary file, move it over ``path``, then upload it.

    The caller must hold exclusive access to ``path`` (see GraphStoreCache).
    Building next to the live file and renaming it into place means a failed
    or interrupted build leaves the previous graph intact, and the file never
    accumulates the history of earlier builds. Closing the writer before the
    rename checkpoints the new file.
    """
    _, store, persistence = _services()
    # The build digests the inputs itself: a digest cached before it must not
    # judge the new graph stale.
    with _inputs_digests_lock:
        _inputs_digests.pop((db_name, job_id), None)
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
    complete = stats.get("complete", True)
    if complete:
        _clear_rebuild_failure(db_name, job_id)
    else:
        _record_rebuild_failure(db_name, job_id)
    if not complete and _is_populated_file(path):
        # An artifact could not be read: keep the previous graph (and its
        # stamp), so a later read retries instead of serving a partial one.
        logger.warning(
            "graph rebuild for %s/%s was incomplete; keeping the previous graph", db_name, job_id
        )
        for leftover in _sidecars(building):
            Path(leftover).unlink(missing_ok=True)
        return stats
    for leftover in _sidecars(path)[1:]:
        Path(leftover).unlink(missing_ok=True)
    os.replace(building, path)
    for leftover in _sidecars(building)[1:]:
        Path(leftover).unlink(missing_ok=True)
    if not complete:
        # No previous graph: serve this unstamped one, which a later read
        # rebuilds; never upload it for other API tasks to reuse.
        return stats
    try:
        persistence.upload(db_name, job_id, path)
    except Exception as exc:  # upload failure must not break the response
        logger.warning("graph upload failed for %s/%s: %s", db_name, job_id, exc)
    return stats


def _sidecars(path: str) -> tuple[str, str, str]:
    """The database file and the engine's write-ahead-log and shadow files."""
    return path, f"{path}.wal", f"{path}.shadow"


def _prepare_graph_file(db_name: str, job_id: str, path: str, *, refreshed: bool = False) -> None:
    """Make ``path`` hold this job's graph: local file -> store download -> build+upload.

    A local or downloaded graph is used only when it was built from the job's
    current effective assignment version; otherwise it is rebuilt (#819).
    When that version can't be read, or a rebuild for this job is in back-off
    (#848), a current-revision graph is used as it is.
    """
    if refreshed and Path(path).exists():
        return
    version = (
        None
        if _in_rebuild_backoff(db_name, job_id)
        else _effective_assignment_version(db_name, job_id)
    )
    digest = None if version is None else _inputs_digest(db_name, job_id)
    if _is_populated_file(path, version, digest):
        return
    _, _, persistence = _services()
    if persistence.download_if_exists(db_name, job_id, path) and _is_populated_file(
        path, version, digest
    ):
        return
    # Cache miss or unusable download: build fresh, then upload (self-healing).
    _build_graph_file(db_name, job_id, path)


def graph_error_to_http(exc: Exception) -> HTTPException | None:
    """Map an engine timeout, out-of-memory or busy error to a neutral HTTP error.

    Out of memory and busy are transient (other reads finish and free the
    buffer pool or a read slot), so they answer 503 with ``Retry-After``.
    """
    if isinstance(exc, GraphBusyError):
        return HTTPException(
            status_code=503, detail=BUSY_DETAIL, headers={"Retry-After": RETRY_AFTER_S}
        )
    message = str(exc)
    if "Interrupted" in message:
        return HTTPException(status_code=504, detail=TIMEOUT_DETAIL)
    if "Buffer manager exception" in message:
        logger.warning("graph read ran out of buffer pool memory: %s", message)
        return HTTPException(
            status_code=503, detail=MEMORY_DETAIL, headers={"Retry-After": RETRY_AFTER_S}
        )
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
    _require_graph_path(cache, db_name, job_id)
    refreshed = _refresh_if_behind(cache, db_name, job_id)
    with cache.reader(
        db_name,
        job_id,
        lambda path: _prepare_graph_file(db_name, job_id, path, refreshed=refreshed),
    ) as store:
        yield store, db_name


def _require_graph_path(cache: GraphStoreCache, db_name: str, job_id: str) -> str:
    """The job's graph path, or 404 when the stored database name or the job id
    would put it outside the artifact root (``GraphStoreCache.local_path``)."""
    try:
        return cache.local_path(db_name, job_id)
    except ValueError as exc:
        logger.warning("graph path refused: %s", exc)
        raise HTTPException(status_code=404, detail="Assessment not found") from exc


def _refresh_if_behind(cache: GraphStoreCache, db_name: str, job_id: str) -> bool:
    """Rebuild an open graph built from an earlier assignment version (#819).

    A customer edit, a reality check or a post-schema reroute writes a new
    assignment version without touching the graph, so an already-open handle
    can lag behind. A graph that is not open yet is checked by
    ``_prepare_graph_file`` when the reader opens it. When the version can't
    be read, or a failed rebuild is in back-off (#848), the open graph is
    served as it is. Returns True when ``cache.exclusive`` was taken so the
    following ``cache.reader`` does not repeat the rebuild on the same read.
    """
    if not cache.is_open(db_name, job_id) or _in_rebuild_backoff(db_name, job_id):
        return False
    version = _effective_assignment_version(db_name, job_id)
    if version is None:
        return False
    digest = _inputs_digest(db_name, job_id)
    if _open_graph_is_current(cache, db_name, job_id, version, digest):
        return False
    with _refresh_lock(db_name, job_id):
        # Another request may have rebuilt it (or failed and backed off) while this one waited.
        if _in_rebuild_backoff(db_name, job_id) or _open_graph_is_current(
            cache, db_name, job_id, version, digest
        ):
            return False
        with cache.exclusive(db_name, job_id) as path:
            if not _is_populated_file(path, version, digest):
                logger.info(
                    "graph for %s/%s is behind assignment v%s: rebuilding",
                    db_name,
                    job_id,
                    version,
                )
                stats = _build_graph_file(db_name, job_id, path)
                if isinstance(stats, dict) and not stats.get("complete", True):
                    _record_rebuild_failure(db_name, job_id)
        return True


def _open_graph_is_current(
    cache: GraphStoreCache, db_name: str, job_id: str, version: int, digest: str | None = None
) -> bool:
    """True when the job's graph carries this code's schema revision, ``version``'s stamp
    and, when known, the digest of the current input artifacts (#847)."""
    with cache.reader(
        db_name, job_id, lambda path: _prepare_graph_file(db_name, job_id, path)
    ) as store:
        meta = store.graph_meta()
    return (
        meta["schema_revision"] == GRAPH_SCHEMA_REVISION
        and meta["effective_version"] == version
        and (digest is None or meta["inputs_digest"] == digest)
    )


def _refresh_lock(db_name: str, job_id: str) -> threading.Lock:
    key = (db_name, job_id)
    with _refresh_locks_guard:
        lock = _refresh_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _refresh_locks[key] = lock
        _refresh_locks.move_to_end(key)
        excess = len(_refresh_locks) - MAX_TRACKED_JOBS
        if excess > 0:
            for old_key, old_lock in list(_refresh_locks.items()):
                if excess <= 0 or old_key == key:
                    break
                if not old_lock.locked():
                    del _refresh_locks[old_key]
                    excess -= 1
        return lock


def get_graph_for_job(job_id: str = Depends(job_id_param)) -> Iterator[tuple[GraphStore, str]]:
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
    job_id: str = Depends(job_id_param),
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
def rebuild_assessment_graph(job_id: str = Depends(job_id_param)):
    """Force rebuild the graph from the artifacts and persist it to the store.

    Disabled unless MODERNIZER_ENABLE_RAW_GRAPH_QUERY=1. Reads rebuild the
    graph on demand when it is missing, so this is only needed to refresh it.
    """
    cache, _, _ = _services()
    db_name = _get_database_name(job_id)
    _require_graph_path(cache, db_name, job_id)
    with cache.exclusive(db_name, job_id) as path:
        stats = _build_graph_file(db_name, job_id, path)
    return {"status": "rebuilt" if stats.get("complete", True) else "incomplete", **stats}


@router.get("/{job_id}/load-test-results", response_model=LoadTestResultsResponse)
def load_test_results(
    job_id: str = Depends(job_id_param),
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
def graph_table_impact(
    table_id: str,
    job_id: str = Depends(job_id_param),
    graph: Any = Depends(get_graph_for_job),
):
    """Queries affected if the given source table changes."""
    store, _ = graph
    with _curated_errors():
        return graph_queries.table_impact(store, table_id)


@router.get(
    "/{job_id}/graph/queries/{query_id}/provenance",
    response_model=QueryProvenanceResponse,
)
def graph_query_provenance(
    query_id: str,
    job_id: str = Depends(job_id_param),
    graph: Any = Depends(get_graph_for_job),
):
    """Why a query migrated where it did, and which agent decided it."""
    store, _ = graph
    with _curated_errors():
        return graph_queries.query_provenance(store, query_id)


@router.get(
    "/{job_id}/graph/queries/{query_id}/trace",
    response_model=QueryTraceResponse,
)
def graph_query_trace(
    query_id: str,
    job_id: str = Depends(job_id_param),
    graph: Any = Depends(get_graph_for_job),
):
    """Why a query ended on its engine and not another (#339).

    Its first assignment with every engine's score or gate, each later move
    across the assignment versions with its reason, the final engine, and the
    runner-up with its score gap or disqualifying gate.
    """
    store, _ = graph
    with _curated_errors():
        trace = graph_queries.query_trace(store, query_id)
    if trace is None:
        raise HTTPException(status_code=404, detail="Query not found in this assessment")
    return trace


@router.get("/{job_id}/graph/engines/{engine}", response_model=EngineDetailResponse)
def graph_engine_detail(
    engine: str,
    job_id: str = Depends(job_id_param),
    graph: Any = Depends(get_graph_for_job),
):
    """Destinations and source tables migrating to a given engine."""
    store, _ = graph
    with _curated_errors():
        return graph_queries.engine_detail(store, engine)


@router.get("/{job_id}/graph/risks", response_model=RiskHotspotsResponse)
def graph_risks(job_id: str = Depends(job_id_param), graph: Any = Depends(get_graph_for_job)):
    """Tables carrying risk and anti-patterns, weighted by traffic."""
    store, _ = graph
    with _curated_errors():
        return graph_queries.risk_hotspots(store)
