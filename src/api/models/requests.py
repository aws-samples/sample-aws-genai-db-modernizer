"""Request models for the Database Modernizer API."""

from pydantic import BaseModel, Field, model_validator


class ConnectionDetails(BaseModel):
    host: str
    port: int = Field(ge=1, le=65535)
    database: str
    credentials_type: str = "secrets-arn"
    secret_arn: str | None = None
    secret_name: str | None = None
    username: str | None = None
    password: str | None = None


class CollectionOptions(BaseModel):
    anonymize_pii: bool = True
    include_sample_data: bool = True
    sample_size: int = Field(default=1000, ge=100, le=10000)
    query_log_period_days: int = Field(default=7, ge=1, le=30)
    query_log_source: str = "performance-insights"
    include_table_patterns: str | None = None
    exclude_table_patterns: str | None = None
    top_sql_queries: int = 100
    top_wait_events: int = 50


class AssessmentRequest(BaseModel):
    source_database_type: str
    database_name: str
    connection: ConnectionDetails | None = None
    options: CollectionOptions | None = None
    target_databases: list[str] | None = None
    full_analysis: bool = False
    collection_mode: str = "live"  # live, ddl, or offline
    offline_s3_key: str | None = None
    cluster_id: str | None = (
        None  # RDS instance/cluster ID — auto-discovers VPC, SG, port for live mode
    )
    job_id: str | None = None  # Pre-created job ID from /assessments/prepare

    @model_validator(mode="after")
    def validate_connection_required(self):
        if self.collection_mode in ("live", "ddl") and not self.connection:
            raise ValueError("connection is required for live and ddl modes")
        if self.collection_mode == "offline" and not self.offline_s3_key:
            raise ValueError("offline_s3_key is required for offline mode")
        return self


class PrepareAssessmentRequest(BaseModel):
    database_name: str
    source_database_type: str = "mysql"
