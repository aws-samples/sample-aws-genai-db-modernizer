"""Assessment routes — job lifecycle and monitoring."""

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, HTTPException

from src.api.models.requests import AssessmentRequest, PrepareAssessmentRequest
from src.api.models.responses import (
    AgentStatus,
    AssessmentCreated,
    AssessmentDetail,
    AssessmentList,
    AssessmentPrepared,
    AssessmentProgress,
    AssessmentSummary,
    ErrorDetail,
    LogEntry,
    LogsResponse,
    StageProgress,
)
from src.api.services.cloudwatch import CloudWatchLogsService
from src.api.services.s3_artifacts import S3ArtifactsService
from src.api.services.step_functions import StepFunctionsService

router = APIRouter(prefix="/api/v1/assessments", tags=["assessments"])

# Services injected by main.py at startup
sfn_service: StepFunctionsService | None = None
s3_service: S3ArtifactsService | None = None
cw_service: CloudWatchLogsService | None = None


def _require_services() -> tuple[StepFunctionsService, S3ArtifactsService]:
    """Raise 503 if services aren't configured; returns narrowed types."""
    if not sfn_service or not s3_service:
        raise HTTPException(status_code=503, detail="Services not configured")
    return sfn_service, s3_service


@router.post("/prepare", response_model=AssessmentPrepared, status_code=201)
async def prepare_assessment(request: PrepareAssessmentRequest):
    """Pre-create a job ID and S3 upload folder for offline/DDL assessments.

    Returns the job ID, S3 upload prefix, and a presigned URL so the client
    can upload collector output files before starting the assessment.
    """
    _, s3_svc = _require_services()
    job_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    upload_prefix = f"{request.database_name}/{job_id}/uploads/"
    upload_key = f"{upload_prefix}collector-output.json"
    expires_in = 3600  # 1 hour

    # Generate presigned URL for uploading
    upload_url = s3_svc.client.generate_presigned_url(
        "put_object",
        Params={
            "Bucket": s3_svc.bucket,
            "Key": upload_key,
            "ContentType": "application/json",
        },
        ExpiresIn=expires_in,
    )

    return AssessmentPrepared(
        job_id=job_id,
        upload_prefix=upload_prefix,
        upload_bucket=s3_svc.bucket,
        upload_url=upload_url,
        upload_key=upload_key,
        status="PREPARED",
        created_at=now.isoformat(),
        expires_in_seconds=expires_in,
    )


@router.post("/{job_id}/uploads/confirm")
async def confirm_upload(job_id: str, database_name: str):
    """Confirm that the file upload completed successfully.

    Checks that the collector output file exists in S3 at the expected
    location. Call this after the presigned URL upload finishes.
    """
    _, s3_svc = _require_services()
    upload_key = f"{database_name}/{job_id}/uploads/collector-output.json"

    try:
        head = s3_svc.client.head_object(Bucket=s3_svc.bucket, Key=upload_key)
        return {
            "job_id": job_id,
            "status": "confirmed",
            "filename": "collector-output.json",
            "size_bytes": head["ContentLength"],
            "upload_key": upload_key,
        }
    except Exception as e:
        raise HTTPException(
            status_code=404,
            detail=f"Upload not found at {upload_key}. Did the presigned URL upload complete?",
        ) from e


@router.get("/{job_id}/uploads")
async def list_uploads(job_id: str, database_name: str):
    """List uploaded files for a prepared assessment."""
    _, s3_svc = _require_services()
    prefix = f"{database_name}/{job_id}/uploads/"

    try:
        response = s3_svc.client.list_objects_v2(
            Bucket=s3_svc.bucket,
            Prefix=prefix,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to list uploads: {e}") from e

    files = []
    for obj in response.get("Contents", []):
        key = obj["Key"]
        filename = key.removeprefix(prefix)
        if filename:  # Skip empty prefix matches
            files.append(
                {
                    "key": key,
                    "filename": filename,
                    "size_bytes": obj["Size"],
                    "last_modified": obj["LastModified"].isoformat(),
                }
            )

    return {"job_id": job_id, "uploads": files}


@router.delete("/{job_id}/uploads/{filename}")
async def delete_upload(job_id: str, database_name: str, filename: str):
    """Delete an uploaded file from a prepared assessment."""
    _, s3_svc = _require_services()
    key = f"{database_name}/{job_id}/uploads/{filename}"

    try:
        # Check if file exists first
        s3_svc.client.head_object(Bucket=s3_svc.bucket, Key=key)
    except s3_svc.client.exceptions.NoSuchKey as e:
        raise HTTPException(status_code=404, detail=f"File not found: {filename}") from e
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"File not found: {filename}") from e

    s3_svc.client.delete_object(Bucket=s3_svc.bucket, Key=key)
    return {"job_id": job_id, "filename": filename, "status": "deleted"}


@router.post("", response_model=AssessmentCreated, status_code=202)
async def create_assessment(request: AssessmentRequest):
    """Start a new database modernization assessment."""
    sfn_svc, _ = _require_services()
    job_id = request.job_id or str(uuid.uuid4())
    now = datetime.now(UTC)

    sfn_input = {
        "job_id": job_id,
        "database_name": request.database_name,
        "source_database_type": request.source_database_type,
        "connection": request.connection.model_dump() if request.connection else {},
        "options": request.options.model_dump() if request.options else {},
        "target_databases": request.target_databases or [],
        "full_analysis": request.full_analysis,
        "synthesis_iteration": 0,
        "collection_mode": request.collection_mode,
        "offline_s3_key": request.offline_s3_key or "",
        # Live mode fields — empty defaults for ddl/offline (SFN JSONPath requires all keys)
        "cluster_endpoint": request.connection.host if request.connection else "offline",
        "secret_arn": request.connection.secret_arn or "" if request.connection else "",
        "automation_instance_id": "",
        "port": str(request.connection.port) if request.connection else "0",
        "engine": request.source_database_type,
        "db_instance_identifier": "",
    }

    # For live mode with cluster_id: discover RDS, provision automation machine, add SG ingress
    if request.collection_mode == "live" and request.cluster_id:
        from src.tools.aws.automation import (
            add_ingress_rule,
            discover_cluster,
            ensure_automation_machine,
        )

        cluster_info = discover_cluster(request.cluster_id)
        auto = ensure_automation_machine(
            vpc_id=cluster_info["vpc_id"],
            subnet_id=cluster_info["subnet_id"],
            vpc_cidr=cluster_info["vpc_cidr"],
            route_table_id=cluster_info["route_table_id"],
            subnet_id_2=cluster_info["subnet_id_2"],
        )
        add_ingress_rule(
            rds_security_group_id=cluster_info["rds_security_group_id"],
            automation_security_group_id=auto["security_group_id"],
            port=cluster_info["port"],
        )

        # Enrich SFN input with discovered values
        sfn_input["automation_instance_id"] = auto["instance_id"]
        sfn_input["cluster_endpoint"] = cluster_info["endpoint"]
        sfn_input["port"] = cluster_info["port"]
        sfn_input["engine"] = cluster_info["engine"]
        sfn_input["db_instance_identifier"] = cluster_info["db_instance_identifier"]

    result = sfn_svc.start_execution(job_id, sfn_input)

    return AssessmentCreated(
        job_id=job_id,
        status="PENDING",
        created_at=now.isoformat(),
        estimated_completion_time=(now + timedelta(hours=6)).isoformat(),
        execution_arn=result["execution_arn"],
    )


@router.get("", response_model=AssessmentList)
async def list_assessments(
    status: str | None = None,
    limit: int = 25,
    offset: int = 0,
):
    """List all assessments."""
    sfn_svc, _ = _require_services()
    executions = sfn_svc.list_executions(
        status_filter=status.upper() if status else None,
        max_results=limit + offset,
    )

    # Apply offset/limit
    page = executions[offset : offset + limit]

    assessments = []
    for ex in page:
        duration = None
        if ex["started_at"] and ex["stopped_at"]:
            start = datetime.fromisoformat(ex["started_at"])
            end = datetime.fromisoformat(ex["stopped_at"])
            duration = int((end - start).total_seconds())

        assessments.append(
            AssessmentSummary(
                job_id=ex["job_id"],
                database_name=ex.get("database_name"),
                status=ex["status"],
                created_at=ex["started_at"],
                completed_at=ex["stopped_at"],
                duration_seconds=duration,
            )
        )

    return AssessmentList(
        assessments=assessments,
        total_count=len(executions),
        limit=limit,
        offset=offset,
    )


@router.get("/{job_id}", response_model=AssessmentDetail)
async def get_assessment(job_id: str):
    """Get assessment status with pipeline progress."""
    sfn_svc, _ = _require_services()
    execution = sfn_svc.describe_execution(job_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Assessment not found")

    # Get per-agent progress from execution history
    history = sfn_svc.get_execution_history(job_id)
    stages = []
    completed_count = 0
    current_stage = None

    for stage in history:
        duration = None
        if stage.get("started_at") and stage.get("completed_at"):
            from datetime import datetime as dt

            start = dt.fromisoformat(stage["started_at"])
            end = dt.fromisoformat(stage["completed_at"])
            duration = int((end - start).total_seconds())

        stages.append(
            StageProgress(
                name=stage["name"],
                status=stage["status"],
                duration_seconds=duration,
            )
        )

        if stage["status"] == "completed":
            completed_count += 1
        elif stage["status"] == "in-progress":
            current_stage = stage["name"]

    total_stages = max(len(stages), 1)
    percent = int((completed_count / total_stages) * 100) if stages else 0

    progress = AssessmentProgress(
        percent_complete=percent,
        current_stage=current_stage,
        current_activity=f"Running {current_stage}" if current_stage else None,
        stages=stages,
    )

    sfn_input = execution.get("input", {})

    # Surface error details for failed executions
    error_detail = None
    if execution.get("error") or execution.get("cause"):
        error_detail = ErrorDetail(
            error=execution.get("error"),
            cause=execution.get("cause"),
        )

    return AssessmentDetail(
        job_id=job_id,
        status=execution["status"],
        source_database_type=sfn_input.get("source_database_type"),
        database_name=sfn_input.get("database_name"),
        created_at=execution.get("started_at"),
        execution_arn=sfn_svc._execution_arn(job_id),
        progress=progress,
        error=error_detail,
    )


@router.delete("/{job_id}")
async def cancel_assessment(job_id: str):
    """Cancel a running assessment or delete a completed one."""
    sfn_svc, _ = _require_services()
    execution = sfn_svc.describe_execution(job_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Assessment not found")

    if execution["status"] == "RUNNING":
        sfn_svc.stop_execution(job_id)

    return {"job_id": job_id, "status": "CANCELLED", "message": "Assessment cancelled successfully"}


@router.get("/{job_id}/agents")
async def get_agent_statuses(job_id: str):
    """Get per-agent status table with artifact summaries for completed agents."""
    sfn_svc, s3_svc = _require_services()
    execution = sfn_svc.describe_execution(job_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Assessment not found")

    history = sfn_svc.get_execution_history(job_id)
    database_name = execution.get("input", {}).get("database_name", "")

    agents = []
    for stage in history:
        duration = None
        if stage.get("started_at") and stage.get("completed_at"):
            from datetime import datetime as dt

            start = dt.fromisoformat(stage["started_at"])
            end = dt.fromisoformat(stage["completed_at"])
            duration = int((end - start).total_seconds())

        # Check S3 for output size and summary if completed
        output_size = None
        artifact_summary = None
        if stage["status"] == "completed" and database_name:
            agent_name = _stage_to_agent_name(stage["name"])
            filename = _agent_filename(agent_name) if agent_name else None
            if agent_name and filename:
                output_size = s3_svc.artifact_size(database_name, job_id, agent_name, filename)
                artifact_summary = _extract_artifact_summary(database_name, job_id, agent_name)

        agents.append(
            AgentStatus(
                agent_name=stage["name"],
                status=stage["status"],
                started_at=stage.get("started_at"),
                completed_at=stage.get("completed_at"),
                duration_seconds=duration,
                output_size_bytes=output_size,
                artifact_summary=artifact_summary,
                details=_summarize_artifact(artifact_summary),
            )
        )

    return {"agents": agents}


@router.get("/{job_id}/execution-history")
async def get_execution_history(job_id: str):
    """Get full Step Functions execution history with all state types.

    Returns a flat table of all states (Task, Map, MapIteration, Pass, etc.)
    in chronological order — mirrors the SFN console table view.
    """
    sfn_svc, _ = _require_services()
    execution = sfn_svc.describe_execution(job_id)
    if not execution:
        raise HTTPException(status_code=404, detail="Assessment not found")

    states = sfn_svc.get_full_execution_history(job_id)

    return {
        "job_id": job_id,
        "status": execution["status"],
        "started_at": execution["started_at"],
        "stopped_at": execution.get("stopped_at"),
        "states": states,
    }


@router.get("/{job_id}/logs", response_model=LogsResponse)
async def get_logs(
    job_id: str,
    agent: str | None = None,
    limit: int = 100,
    next_token: str | None = None,
):
    """Get execution logs, optionally filtered by agent."""
    _require_services()  # validates services exist
    if not cw_service:
        raise HTTPException(status_code=503, detail="CloudWatch logs not configured")

    stream_prefix = agent if agent else None
    result = cw_service.get_logs(
        stream_prefix=stream_prefix,
        limit=limit,
        next_token=next_token,
    )

    logs = [
        LogEntry(
            timestamp=str(entry["timestamp"]),
            message=entry["message"],
            agent=_stream_to_agent(entry.get("log_stream", "")),
        )
        for entry in result["logs"]
    ]

    return LogsResponse(logs=logs, next_token=result.get("next_token"))


def _summarize_artifact(summary: dict | None) -> str | None:
    """Build a short human-readable detail string from an artifact summary."""
    if not summary:
        return None

    try:
        # Collector
        if "tables_collected" in summary:
            parts = [f"{summary['tables_collected']} tables"]
            if summary.get("queries_collected"):
                parts.append(f"{summary['queries_collected']} queries")
            if summary.get("engine"):
                parts.append(str(summary["engine"]))
            return "Collected " + ", ".join(parts)

        # Referee triage
        if "selected_agents" in summary:
            selected = summary.get("selected_agents") or []
            skipped = summary.get("skipped_agents") or []
            return f"Selected {len(selected)} targets, skipped {len(skipped)}"

        # Analysis agents
        if "target_database" in summary:
            parts = [f"{summary.get('tables_analyzed', 0)} tables analyzed"]
            if summary.get("avg_confidence_score"):
                parts.append(f"{summary['avg_confidence_score']}% confidence")
            if summary.get("monthly_cost_usd"):
                parts.append(f"${summary['monthly_cost_usd']}/mo est.")
            return ", ".join(parts)

        # Referee synthesis
        if "ranking" in summary:
            ranking = summary.get("ranking") or []
            if ranking and isinstance(ranking, list) and len(ranking) > 0:
                top = ranking[0].get("target", "unknown")
                score = ranking[0].get("confidence_score", 0)
                return f"Top recommendation: {top} ({score}% confidence)"
            return "Synthesis complete"
    except Exception:
        return None

    return None


def _extract_artifact_summary(database_name: str, job_id: str, agent_name: str) -> dict | None:
    """Extract key metrics from an agent's S3 artifact for the status view."""
    assert s3_service is not None
    try:
        if agent_name == "collector":
            data = s3_service.read_artifact(database_name, job_id, "collector", "output.json")
            if not data:
                return None
            tables = data.get("database_schema", {}).get("tables", [])
            queries = data.get("queries", {}).get("query_patterns", [])
            meta = data.get("metadata", {}).get("source_database", {})
            return {
                "engine": meta.get("engine"),
                "tables_collected": len(tables),
                "queries_collected": len(queries),
                "database_size_gb": meta.get("database_size_gb"),
            }

        if agent_name == "referee-triage":
            data = s3_service.read_artifact(database_name, job_id, "referee-triage", "triage.json")
            if not data:
                return None
            return {
                "selected_agents": [a.get("agent_type") for a in data.get("selected_agents", [])],
                "skipped_agents": [a.get("agent_type") for a in data.get("skipped_agents", [])],
                "confidence_score": data.get("confidence_score"),
                "signals_detected": len(data.get("signals", [])),
            }

        if agent_name == "referee-synthesis":
            data = s3_service.read_artifact(
                database_name, job_id, "referee-synthesis", "report.json"
            )
            if not data:
                return None
            ranking = data.get("ranking", [])
            return {
                "ranking": [
                    {"target": r.get("target"), "confidence_score": r.get("confidence_score")}
                    for r in ranking
                ],
                "recommended_schema_designs": data.get("recommended_schema_designs", []),
                "summary_text": data.get("summary", ""),
            }

        if agent_name.startswith("analysis"):
            # analysis agents: try analysis-{type}/analysis.json
            agent_type = agent_name.replace("analysis-", "") if "-" in agent_name else agent_name
            data = s3_service.read_analysis(database_name, job_id, agent_type)
            if not data:
                return None
            table_recs = data.get("table_recommendations", [])
            patterns = data.get("workload_analysis", {}).get("patterns_detected", [])
            anti_patterns = data.get("workload_analysis", {}).get("anti_patterns_detected", [])
            cost = data.get("cost_estimate", {})
            avg_confidence = 0
            if table_recs:
                avg_confidence = round(
                    sum(t.get("confidence_score", 0) for t in table_recs) / len(table_recs)
                )
            return {
                "target_database": agent_type,
                "tables_analyzed": len(table_recs),
                "avg_confidence_score": avg_confidence,
                "patterns_detected": len(patterns),
                "anti_patterns_detected": len(anti_patterns or []),
                "monthly_cost_usd": cost.get("monthly_cost_usd"),
            }

    except Exception:
        return None

    return None


def _stage_to_agent_name(stage_name: str) -> str | None:
    """Map Step Functions state name to S3 agent directory."""
    mapping = {
        "RunCollector": "collector",
        "RunRefereeTriage": "referee-triage",
        "RunRefereeSynthesis": "referee-synthesis",
        "RunAnalysis": "analysis",
        "RunAssignmentResolution": "assignment-resolver",
        "RunRealityCheck": "reality-check",
        "RunSchemaDesign": "schema-design",
        "RunSchemaAgent": "schema-design",
    }
    return mapping.get(stage_name)


def _agent_filename(agent_name: str) -> str | None:
    """Map agent name to its output filename."""
    mapping = {
        "collector": "output.json",
        "referee-triage": "triage.json",
        "referee-synthesis": "report.json",
    }
    return mapping.get(agent_name, "analysis.json")


def _stream_to_agent(log_stream: str) -> str | None:
    """Extract agent name from CloudWatch log stream name."""
    # Stream format: <prefix>/<task-id>
    if "/" in log_stream:
        return log_stream.split("/")[0]
    return None
