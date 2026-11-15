# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Dashboard routes — aggregate stats for the dashboard page."""

from fastapi import APIRouter, HTTPException

from src.api.durations import duration_seconds
from src.api.models.responses import DashboardStats
from src.api.services.local_execution import LocalExecutionService

router = APIRouter(prefix="/api/v1/dashboard", tags=["dashboard"])

sfn_service: LocalExecutionService | None = None


@router.get("/stats", response_model=DashboardStats)
async def get_dashboard_stats():
    """Get aggregate stats for the dashboard system status cards."""
    if not sfn_service:
        raise HTTPException(status_code=503, detail="Services not configured")

    # Get all executions to compute stats
    all_executions = sfn_service.list_executions(max_results=100)
    running = sfn_service.list_executions(status_filter="RUNNING", max_results=100)
    waiting = sfn_service.list_executions(status_filter="WAITING", max_results=100)
    succeeded = sfn_service.list_executions(status_filter="SUCCEEDED", max_results=100)

    total = len(all_executions)
    # A job waiting on a human or an external LLM response is unfinished,
    # same as RUNNING (#383, #518 round 1) -- both count as active.
    active = len(running) + len(waiting)
    success_count = len(succeeded)
    success_rate = (success_count / total * 100) if total > 0 else 0

    # Average over jobs with a known, non-negative duration; unknown otherwise.
    durations = [
        d / 3600
        for d in (duration_seconds(ex.get("started_at"), ex.get("stopped_at")) for ex in succeeded)
        if d is not None
    ]
    avg_duration = round(sum(durations) / len(durations), 1) if durations else None

    # Last analysis timestamp
    last_at = all_executions[0]["started_at"] if all_executions else None

    return DashboardStats(
        total_assessments=total,
        active_jobs=active,
        success_rate_percent=round(success_rate, 1),
        average_duration_hours=avg_duration,
        completed_today=0,  # TODO: filter by today's date
        last_analysis_at=last_at,
    )
