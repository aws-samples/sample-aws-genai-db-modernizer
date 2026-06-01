"""Dashboard routes — aggregate stats for the dashboard page."""

from fastapi import APIRouter, HTTPException

from src.api.models.responses import DashboardStats
from src.api.services.step_functions import StepFunctionsService

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])

sfn_service: StepFunctionsService | None = None


@router.get("/stats", response_model=DashboardStats)
async def get_dashboard_stats():
    """Get aggregate stats for the dashboard system status cards."""
    if not sfn_service:
        raise HTTPException(status_code=503, detail="Services not configured")

    # Get all executions to compute stats
    all_executions = sfn_service.list_executions(max_results=100)
    running = sfn_service.list_executions(status_filter="RUNNING", max_results=100)
    succeeded = sfn_service.list_executions(status_filter="SUCCEEDED", max_results=100)

    total = len(all_executions)
    active = len(running)
    success_count = len(succeeded)
    success_rate = (success_count / total * 100) if total > 0 else 0

    # Compute average duration from completed jobs
    durations = []
    for ex in succeeded:
        if ex.get("started_at") and ex.get("stopped_at"):
            from datetime import datetime as dt

            start = dt.fromisoformat(ex["started_at"])
            end = dt.fromisoformat(ex["stopped_at"])
            durations.append((end - start).total_seconds() / 3600)

    avg_duration = sum(durations) / len(durations) if durations else 0

    # Last analysis timestamp
    last_at = all_executions[0]["started_at"] if all_executions else None

    return DashboardStats(
        total_assessments=total,
        active_jobs=active,
        success_rate_percent=round(success_rate, 1),
        average_duration_hours=round(avg_duration, 1),
        completed_today=0,  # TODO: filter by today's date
        last_analysis_at=last_at,
    )
