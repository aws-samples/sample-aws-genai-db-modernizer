"""Results routes — analysis results, table mappings, and raw artifacts."""

from fastapi import APIRouter, HTTPException, Query

from src.api.services.s3_artifacts import S3ArtifactsService
from src.api.services.step_functions import StepFunctionsService

router = APIRouter(prefix="/api/v1/assessments", tags=["results"])

sfn_service: StepFunctionsService | None = None
s3_service: S3ArtifactsService | None = None


def _get_database_name(job_id: str) -> str:
    """Resolve database_name from Step Functions execution input."""
    if not sfn_service:
        raise HTTPException(status_code=503, detail="Services not configured")
    execution = sfn_service.describe_execution(job_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Assessment not found")
    db_name: str = execution.get("input", {}).get("database_name", "")
    return db_name


@router.get("/{job_id}/results")
async def get_results(job_id: str):
    """Get full analysis results (executive summary, architecture, TCO, risks)."""
    db_name = _get_database_name(job_id)
    if not s3_service:
        raise HTTPException(status_code=503, detail="Services not configured")

    synthesis = s3_service.read_synthesis(db_name, job_id)
    if not synthesis:
        raise HTTPException(status_code=404, detail="Results not available yet")

    triage = s3_service.read_triage(db_name, job_id)

    return {
        "job_id": job_id,
        "status": "COMPLETED",
        "synthesis": synthesis,
        "triage_summary": triage,
    }


@router.get("/{job_id}/collector")
async def get_collector_output(job_id: str):
    """Get raw collector output artifact."""
    db_name = _get_database_name(job_id)
    if not s3_service:
        raise HTTPException(status_code=503, detail="Services not configured")
    data = s3_service.read_collector(db_name, job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Collector output not available")
    return data


@router.get("/{job_id}/triage")
async def get_triage_output(job_id: str):
    """Get raw triage decisions artifact."""
    db_name = _get_database_name(job_id)
    if not s3_service:
        raise HTTPException(status_code=503, detail="Services not configured")
    data = s3_service.read_triage(db_name, job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Triage output not available")
    return data


@router.get("/{job_id}/analysis/{agent_type}")
async def get_analysis_output(job_id: str, agent_type: str):
    """Get raw analysis output for a specific agent type."""
    db_name = _get_database_name(job_id)
    if not s3_service:
        raise HTTPException(status_code=503, detail="Services not configured")
    data = s3_service.read_analysis(db_name, job_id, agent_type)
    if not data:
        raise HTTPException(
            status_code=404, detail=f"Analysis output for {agent_type} not available"
        )
    return data


@router.get("/{job_id}/reality-check")
async def get_reality_check(job_id: str):
    """Get reality check output — engine consolidations and recommendations."""
    db_name = _get_database_name(job_id)
    if not s3_service:
        raise HTTPException(status_code=503, detail="Services not configured")
    data = s3_service.read_reality_check(db_name, job_id)
    if not data:
        raise HTTPException(status_code=404, detail="Reality check output not available")
    return data


@router.get("/{job_id}/results/table-mappings")
async def get_table_mappings(
    job_id: str,
    limit: int = Query(default=25, ge=1, le=500, description="Max rows to return"),
    offset: int = Query(default=0, ge=0, description="Rows to skip"),
    recommended_db: str | None = Query(default=None, description="Filter by target engine"),
    sort: str = Query(
        default="confidence:desc",
        description="Sort field:direction — supported: confidence:asc, confidence:desc, source_table:asc, source_table:desc",
    ),
):
    """Get paginated table mappings from the synthesis report.

    Reads table_mappings from the synthesis artifact and returns a paginated,
    filterable slice. Returns 404 until synthesis has completed.
    """
    db_name = _get_database_name(job_id)
    if not s3_service:
        raise HTTPException(status_code=503, detail="Services not configured")

    synthesis = s3_service.read_synthesis(db_name, job_id)
    if not synthesis:
        raise HTTPException(
            status_code=404, detail="Results not available yet — synthesis has not completed"
        )

    all_mappings: list[dict] = synthesis.get("table_mappings", [])

    # Filter by recommended_db if provided
    if recommended_db:
        all_mappings = [
            m
            for m in all_mappings
            if m.get("recommended_database", "").lower() == recommended_db.lower()
        ]

    # Sort
    sort_field, _, sort_dir = sort.partition(":")
    reverse = sort_dir.lower() != "asc"
    if sort_field == "confidence":
        all_mappings = sorted(
            all_mappings, key=lambda m: m.get("confidence_score", 0), reverse=reverse
        )
    elif sort_field == "source_table":
        all_mappings = sorted(
            all_mappings, key=lambda m: m.get("source_table", "").lower(), reverse=reverse
        )

    total = len(all_mappings)
    page = all_mappings[offset : offset + limit]

    return {
        "table_mappings": page,
        "total_count": total,
        "limit": limit,
        "offset": offset,
    }


@router.get("/{job_id}/schema-designs")
async def get_schema_designs(job_id: str):
    """Get all schema design artifacts."""
    db_name = _get_database_name(job_id)
    if not s3_service:
        raise HTTPException(status_code=503, detail="Services not configured")
    designs = s3_service.read_all_schema_designs(db_name, job_id)
    return {"schema_designs": designs}
