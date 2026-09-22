"""Query journey routes — list and detail for modernization story."""

import math

from fastapi import APIRouter, HTTPException, Query

from src.api.services.step_functions import StepFunctionsService
from src.storage.artifact_store import ArtifactStore
from src.storage.parallel import map_parallel

router = APIRouter(prefix="/api/v1/assessments", tags=["query-journeys"])

sfn_service: StepFunctionsService | None = None
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
    return db_name


def _journeys_from_graph(job_id: str) -> list[dict] | None:
    """Return all query journeys from the assessment graph, or None if unavailable.

    The graph is the read-model that replaces the per-query journey artifacts.
    Uses the same graph accessor the /graph routes use (build-on-demand + cache).
    Returns None when the graph layer is not wired (so the caller falls back to
    the journey artifacts, which still exist for legacy/json-mode jobs)."""
    try:
        from src.api.routes import graph as graph_route
        from src.graph import queries as graph_queries

        store, _db = graph_route._get_graph(job_id)
        return graph_queries.query_journeys(store)
    except Exception:  # noqa: BLE001 - graph not wired/available -> artifact fallback
        # Includes the 503 _get_graph raises when the graph layer isn't
        # configured: that just means "no graph here", so fall back to the
        # journey artifacts rather than failing the request.
        return None


@router.get("/{job_id}/query-journeys")
async def list_query_journeys(
    job_id: str,
    page: int = Query(default=1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(default=50, ge=1, description="Items per page (max 200)"),
):
    """Return paginated list of all query journeys for a job.

    Served from the context graph when available (the read-model that replaces
    the per-query journey artifacts), falling back to the journey artifacts for
    legacy / JOURNEY_MODE=json jobs."""
    page_size = min(page_size, _MAX_PAGE_SIZE)

    graph_items = _journeys_from_graph(job_id)
    if graph_items is not None:
        total = len(graph_items)
        if total == 0:
            raise HTTPException(
                status_code=404, detail="No query journeys found for this assessment"
            )
        total_pages = math.ceil(total / page_size)
        start = (page - 1) * page_size
        page_items = graph_items[start : start + page_size]
        return {
            "job_id": job_id,
            "total": total,
            "page": page,
            "page_size": page_size,
            "total_pages": total_pages,
            "items": page_items,
        }

    # Fallback: per-query journey artifacts (legacy / json mode).
    if not artifact_store:
        raise HTTPException(status_code=503, detail="Services not configured")

    db_name = _get_database_name(job_id)
    prefix = f"{db_name}/{job_id}/query-journeys/"
    all_keys = sorted(artifact_store.list_prefix(prefix))

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
async def get_query_journey(job_id: str, query_id: str):
    """Return the full modernization journey for a single query.

    Served from the context graph when available, falling back to the per-query
    journey artifact for legacy / JOURNEY_MODE=json jobs."""
    try:
        from src.api.routes import graph as graph_route
        from src.graph import queries as graph_queries

        store, _db = graph_route._get_graph(job_id)
        journey = graph_queries.query_journey(store, query_id)
        if journey is not None:
            return journey
        # Graph is available but has no such query — fall through to the artifact
        # (a legacy job may still have it), then 404.
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
