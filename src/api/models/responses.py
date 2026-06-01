"""Response models for the Database Modernizer API."""

from pydantic import BaseModel


class AssessmentCreated(BaseModel):
    job_id: str
    status: str
    created_at: str
    estimated_completion_time: str
    execution_arn: str


class AssessmentPrepared(BaseModel):
    job_id: str
    upload_prefix: str
    upload_bucket: str
    upload_url: str  # Presigned PUT URL for the single file
    upload_key: str  # S3 key the file uploads to
    status: str = "PREPARED"
    created_at: str
    expires_in_seconds: int = 3600


class StageProgress(BaseModel):
    name: str
    status: str  # pending, in-progress, completed, failed
    duration_seconds: int | None = None


class AssessmentProgress(BaseModel):
    percent_complete: int
    current_stage: str | None = None
    current_activity: str | None = None
    estimated_remaining_seconds: int | None = None
    stages: list[StageProgress]


class ErrorDetail(BaseModel):
    error: str | None = None
    cause: str | None = None


class AssessmentDetail(BaseModel):
    job_id: str
    status: str
    source_database_type: str | None = None
    database_name: str | None = None
    created_at: str | None = None
    execution_arn: str | None = None
    progress: AssessmentProgress | None = None
    error: ErrorDetail | None = None


class AssessmentSummary(BaseModel):
    job_id: str
    source_database_type: str | None = None
    database_name: str | None = None
    status: str
    created_at: str | None = None
    completed_at: str | None = None
    duration_seconds: int | None = None
    progress_percent: int = 0


class AssessmentList(BaseModel):
    assessments: list[AssessmentSummary]
    total_count: int
    limit: int
    offset: int


class AgentStatus(BaseModel):
    agent_name: str
    status: str
    started_at: str | None = None
    completed_at: str | None = None
    duration_seconds: int | None = None
    output_size_bytes: int | None = None
    details: str | None = None
    artifact_summary: dict | None = None


class LogEntry(BaseModel):
    timestamp: str
    agent: str | None = None
    level: str | None = None
    message: str


class LogsResponse(BaseModel):
    logs: list[LogEntry]
    next_token: str | None = None


class DashboardStats(BaseModel):
    total_assessments: int
    active_jobs: int
    success_rate_percent: float
    average_duration_hours: float
    completed_today: int
    last_analysis_at: str | None = None
