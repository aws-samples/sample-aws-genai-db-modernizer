"""Query journey routes — list and detail for modernization story."""

import math

from fastapi import APIRouter, HTTPException, Query

from src.api.services.step_functions import StepFunctionsService
from src.storage.artifact_store import ArtifactStore

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


@router.get("/{job_id}/query-journeys")
async def list_query_journeys(
    job_id: str,
    page: int = Query(default=1, ge=1, description="Page number (1-based)"),
    page_size: int = Query(default=50, ge=1, description="Items per page (max 200)"),
):
    """Return paginated list of all query journeys for a job."""
    if not artifact_store:
        raise HTTPException(status_code=503, detail="Services not configured")

    page_size = min(page_size, _MAX_PAGE_SIZE)

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

    items = []
    for key in page_keys:
        try:
            items.append(artifact_store.read_json(key))
        except Exception:  # nosec B112
            continue

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
    """Return the full modernization journey for a single query."""
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
