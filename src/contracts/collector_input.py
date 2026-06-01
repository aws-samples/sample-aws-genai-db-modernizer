"""
Collector Input Contract (Pydantic Model)

Version: 3.0

Two collection modes:
  - LIVE: Connect to cluster endpoint + Secrets Manager, collect everything directly
  - DDL:  Parse DDL scripts from S3, pull metrics via cluster endpoint (no DB connection)
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DatabaseEngine(str, Enum):
    mysql = "mysql"
    postgresql = "postgresql"
    mariadb = "mariadb"
    sqlserver = "sqlserver"
    oracle = "oracle"
    db2 = "db2"


class CollectionMode(str, Enum):
    live = "live"  # Connect to DB directly
    ddl = "ddl"  # Parse DDL from S3, metrics via AWS APIs only
    offline = "offline"  # Parse pre-collected JSON from S3, metrics via AWS APIs only


class LiveConnectionConfig(BaseModel):
    """Configuration for LIVE mode — DB access via SSM Run Command on automation instance."""

    secret_arn: str = Field(description="AWS Secrets Manager ARN containing username/password")
    automation_instance_id: str = Field(
        description="EC2 instance ID of the automation machine (SSM-managed, has DB drivers)"
    )

    model_config = ConfigDict(extra="ignore")


class DDLConfig(BaseModel):
    """Configuration for DDL mode — parse uploaded DDL scripts from S3."""

    s3_bucket: str = Field(description="S3 bucket where DDL files are stored")
    s3_key: str = Field(description="S3 key (path) to DDL file or prefix for multiple files")

    model_config = ConfigDict(extra="ignore")


class OfflineConfig(BaseModel):
    """Configuration for offline mode — parse pre-collected JSON from S3."""

    s3_bucket: str = Field(description="S3 bucket where offline collection JSON is stored")
    s3_key: str = Field(description="S3 key (path) to the offline collection JSON file")

    model_config = ConfigDict(extra="ignore")


class CollectionOptions(BaseModel):
    """Options controlling what data to collect."""

    anonymize_pii: bool = Field(default=True, description="Anonymize PII in sample data")
    collect_sample_data: bool = Field(
        default=True, description="Collect sample rows (LIVE mode only)"
    )
    sample_row_count: int = Field(
        default=10, ge=0, le=100, description="Number of sample rows per table"
    )
    collect_query_patterns: bool = Field(
        default=True, description="Collect query patterns (LIVE: performance_schema, DDL: PI only)"
    )
    query_log_days: int = Field(default=7, ge=1, le=30, description="Days of query logs to analyze")

    model_config = ConfigDict(extra="ignore")


class AWSMetricsConfig(BaseModel):
    """AWS API-based metrics collection — used in both modes."""

    region: str = Field(description="AWS region (e.g., us-east-1)")

    db_instance_identifier: str | None = Field(
        default=None, description="RDS instance identifier for API calls"
    )
    db_cluster_identifier: str | None = Field(
        default=None, description="RDS cluster identifier (for Aurora clusters)"
    )

    # CloudWatch
    collect_cloudwatch_metrics: bool = Field(default=True, description="Collect CloudWatch metrics")
    cloudwatch_days: int = Field(default=7, ge=1, le=30, description="Days of CloudWatch metrics")

    # Performance Insights
    collect_performance_insights: bool = Field(
        default=True, description="Collect Performance Insights data"
    )
    performance_insights_days: int = Field(
        default=7, ge=1, le=7, description="Days of PI data (max 7 free tier)"
    )

    # CloudWatch Database Insights
    collect_database_insights: bool = Field(
        default=True, description="Collect CloudWatch Database Insights"
    )

    # Cross-account
    assume_role_arn: str | None = Field(
        default=None, description="IAM role ARN for cross-account access"
    )
    external_id: str | None = Field(
        default=None, description="External ID for cross-account role assumption"
    )

    model_config = ConfigDict(extra="ignore")


class CollectorInput(BaseModel):
    """
    Input contract for Collector agents.

    Two modes:
      - LIVE: cluster_endpoint + secret_arn → connect, collect schema/queries/metrics
      - DDL:  cluster_endpoint + s3 DDL location → parse DDL, pull metrics via AWS APIs

    Both modes use cluster_endpoint for AWS API calls (CloudWatch, PI, Database Insights).

    Version history:
    - 2.0: Initial version
    - 3.0: Two-mode collection (live/ddl), cluster_endpoint as primary identifier
    """

    contract_version: str = Field(
        default="3.0", pattern=r"^\d+\.\d+$", description="Contract version"
    )
    job_id: str = Field(description="Unique job identifier")

    # Common fields (both modes)
    engine: DatabaseEngine = Field(description="Database engine type")
    cluster_endpoint: str = Field(
        description="RDS/Aurora cluster or instance endpoint (used for AWS API lookups and LIVE connection)"
    )
    port: int = Field(description="Database port")
    database_name: str = Field(description="Database/schema name to analyze")

    # Mode selection
    mode: CollectionMode = Field(
        default=CollectionMode.live,
        description="Collection mode: live, ddl, or offline",
    )

    # Mode-specific config
    live_config: LiveConnectionConfig | None = Field(
        default=None, description="LIVE mode config (required when mode=live)"
    )
    ddl_config: DDLConfig | None = Field(
        default=None, description="DDL mode config (required when mode=ddl)"
    )
    offline_config: OfflineConfig | None = Field(
        default=None, description="Offline mode config (required when mode=offline)"
    )

    # AWS metrics (both modes)
    aws_config: AWSMetricsConfig | None = Field(
        default=None,
        description="AWS metrics collection config (CloudWatch, PI, Database Insights)",
    )

    collection_options: CollectionOptions = Field(
        default_factory=CollectionOptions, description="Collection options"
    )

    timeout_minutes: int = Field(
        default=360, ge=30, le=720, description="Max collection time in minutes"
    )

    @model_validator(mode="after")
    def validate_mode_config(self):
        if self.mode == CollectionMode.live and not self.live_config:
            raise ValueError("live_config is required when mode=live")
        if self.mode == CollectionMode.ddl and not self.ddl_config:
            raise ValueError("ddl_config is required when mode=ddl")
        if self.mode == CollectionMode.offline and not self.offline_config:
            raise ValueError("offline_config is required when mode=offline")
        return self

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "examples": [
                {
                    "title": "LIVE mode",
                    "job_id": "job-001",
                    "engine": "mysql",
                    "cluster_endpoint": "mydb.cluster-abc123.us-east-1.rds.amazonaws.com",
                    "port": 3306,
                    "database_name": "production",
                    "mode": "live",
                    "live_config": {
                        "secret_arn": "arn:aws:secretsmanager:us-east-1:123456789:secret:mydb-creds",  # pragma: allowlist secret
                        "automation_instance_id": "i-0abc123def456789",
                    },
                    "aws_config": {
                        "region": "us-east-1",
                        "db_cluster_identifier": "mydb",
                        "collect_cloudwatch_metrics": True,
                        "collect_performance_insights": True,
                        "collect_database_insights": True,
                    },
                },
                {
                    "title": "DDL mode",
                    "job_id": "job-002",
                    "engine": "postgresql",
                    "cluster_endpoint": "mydb.cluster-abc123.us-east-1.rds.amazonaws.com",
                    "port": 5432,
                    "database_name": "production",
                    "mode": "ddl",
                    "ddl_config": {
                        "s3_bucket": "my-modernizer-bucket",
                        "s3_key": "uploads/job-002/schema.sql",
                    },
                    "aws_config": {
                        "region": "us-east-1",
                        "db_cluster_identifier": "mydb",
                    },
                },
                {
                    "title": "Offline mode",
                    "job_id": "job-003",
                    "engine": "mysql",
                    "cluster_endpoint": "mydb.cluster-abc123.us-east-1.rds.amazonaws.com",
                    "port": 3306,
                    "database_name": "production",
                    "mode": "offline",
                    "offline_config": {
                        "s3_bucket": "my-modernizer-bucket",
                        "s3_key": "uploads/job-003/collection-output.json",
                    },
                    "aws_config": {
                        "region": "us-east-1",
                        "db_instance_identifier": "mydb",
                        "collect_cloudwatch_metrics": True,
                        "collect_performance_insights": True,
                    },
                },
            ]
        },
    )
