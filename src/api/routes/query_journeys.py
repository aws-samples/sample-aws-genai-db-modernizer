"""Query journey routes — list and detail for modernization story."""

import math

from fastapi import APIRouter, Depends, HTTPException, Query

from src.api.dependencies import job_id_param
from src.api.services.local_execution import LocalExecutionService
from src.shared.database_name import validate_database_name
from src.storage.artifact_store import ArtifactStore
from src.storage.parallel import map_parallel

router = APIRouter(prefix="/api/v1/assessments", tags=["query-journeys"])

sfn_service: LocalExecutionService | None = None
artifact_store: ArtifactStore | None = None

_MAX_PAGE_SIZE = 200


def _get_database_name(job_id: str) -> str:
    """Resolve database_name from Step Functions execution input."""
    if not sfn_service:
        raise HTTPException(status_code=503, detail="Services not configured")
    execution = sfn_service.describe_execution(job_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Assessment not found")
    db_name: str = execution.get("input", {}).get("database_name", "")
    try:
        return validate_database_name(db_name)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Assessment not found") from exc


def _journeys_page_from_graph(
    job_id: str, page: int, page_size: int
) -> tuple[int, list[dict]] | None:
    """Return ``(total, page_items)`` from the assessment graph, or None if unavailable.

    The graph is the read-model that replaces the per-query journey artifacts.
    Uses the same graph accessor the /graph routes use (build-on-demand + cache).
    Only the requested page is read from the graph (#998): the results page
    requests every page at once, so reading the whole job per request ran the
    graph engine out of memory on large jobs. Returns None when the graph
    layer is not wired (so the caller falls back to the journey artifacts,
    which still exist for legacy jobs)."""
    try:
        from src.api.routes import graph as graph_route
        from src.graph import queries as graph_queries

        with graph_route.graph_lease(job_id) as (store, _db):
            total = graph_queries.query_count(store)
            skip = (page - 1) * page_size
            if skip >= total:
                return total, []
            return total, graph_queries.query_journeys_page(store, skip, page_size)
    except HTTPException as exc:
        if exc.status_code == 404 and isinstance(exc.__cause__, ValueError):
            raise
        return None
    except RuntimeError as exc:
        mapped = graph_route.graph_error_to_http(exc)
        if mapped is not None:
            raise mapped from exc
        return None
    except Exception:  # noqa: BLE001 - graph not wired/available -> artifact fallback
        # Includes the 503 graph_lease raises when the graph layer isn't
        # configured: that just means "no graph here", so fall back to the
        # journey artifacts rather than failing the request.
        return None


@router.get("/{job_id}/query-journeys")
def list_query_journeys(
    job_id: str = Depends(job_id_param),
    page: int = Query(default=1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(default=50, ge=1, description="Items per page (max 200)"),
):
    """Return paginated list of all query journeys for a job.

    Served from the context graph when available (the read-model that replaces
    the per-query journey artifacts), falling back to the journey artifacts for
    legacy jobs that predate the graph read-model."""
    page_size = min(page_size, _MAX_PAGE_SIZE)

    from_graph = _journeys_page_from_graph(job_id, page, page_size)
    if from_graph is not None:
        total, page_items = from_graph
        if total == 0:
            raise HTTPException(
                status_code=404, detail="No query journeys found for this assessment"
            )
        total_pages = math.ceil(total / page_size)
        return {
            "job_id": job_id,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "items": page_items,
        }

    # Fallback: per-query journey artifacts (legacy jobs only).
    if not artifact_store:
        raise HTTPException(status_code=503, detail="Services not configured")

    db_name = _get_database_name(job_id)
    prefix = f"{db_name}/{job_id}/query-journeys/"
    try:
        all_keys = sorted(artifact_store.list_prefix(prefix))
    except ValueError as exc:
        raise HTTPException(status_code=404, detail="Assessment not found") from exc

    total = len(all_keys)
    if total == 0:
        raise HTTPException(
            status_code=404,
            detail="No query journeys found for this assessment",
        )

    total_pages = math.ceil(total / page_size)

    start = (page - 1) * page_size
    end = start + page_size
    page_keys = all_keys[start:end]

    # Read the page's journeys concurrently — on the ATX backend each read is a
    # network round trip, so a full 200-item page is ~60-100s serially.
    # map_parallel preserves page order and drops any unreadable journey.
    store = artifact_store
    items = map_parallel(store.read_json, page_keys)

    return {
        "job_id": job_id,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "items": items,
    }


@router.get("/{job_id}/query-journeys/{query_id}")
def get_query_journey(query_id: str, job_id: str = Depends(job_id_param)):
    """Return the full modernization journey for a single query.

    Served from the context graph when available, falling back to the per-query
    journey artifact for legacy jobs that predate the graph read-model."""
    try:
        from src.api.routes import graph as graph_route
        from src.graph import queries as graph_queries

        with graph_route.graph_lease(job_id) as (store, _db):
            journey = graph_queries.query_journey(store, query_id)
        if journey is not None:
            return journey
        # Graph is available but has no such query — fall through to the artifact
        # (a legacy job may still have it), then 404.
    except HTTPException as exc:
        if exc.status_code == 404 and isinstance(exc.__cause__, ValueError):
            raise
    except RuntimeError as exc:
        mapped = graph_route.graph_error_to_http(exc)
        if mapped is not None:
            raise mapped from exc
    except Exception:  # noqa: BLE001 - graph not wired/available -> artifact fallback  # nosec B110
        pass  # intentional: any graph failure means "try the artifact path"

    if not artifact_store:
        raise HTTPException(status_code=503, detail="Services not configured")

    db_name = _get_database_name(job_id)
    path = f"{db_name}/{job_id}/query-journeys/{query_id}.json"

    try:
        journey = artifact_store.read_json(path)
    except Exception as exc:
        raise HTTPException(
            status_code=404,
            detail=f"Journey not found for query '{query_id}'",
        ) from exc

    return journey
