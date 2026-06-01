"""
Analysis Input Contract (Pydantic Model)

Version: 2.0
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class TargetDatabase(str, Enum):
    """Target database services for analysis"""

    dynamodb = "dynamodb"
    documentdb = "documentdb"
    aurora_mysql = "aurora_mysql"
    aurora_postgresql = "aurora_postgresql"
    elasticache = "elasticache"
    opensearch = "opensearch"
    neptune = "neptune"
    keyspaces = "keyspaces"


class AnalysisOptions(BaseModel):
    """Options controlling analysis behavior"""

    perform_cost_estimation: bool = Field(
        default=True, description="Estimate costs for target database"
    )

    perform_load_testing: bool = Field(
        default=False, description="Perform load testing simulation (expensive, optional)"
    )

    include_migration_complexity: bool = Field(
        default=True, description="Assess migration complexity and effort"
    )

    confidence_threshold: float = Field(
        default=0.5, ge=0.0, le=1.0, description="Minimum confidence score for recommendations"
    )

    model_config = ConfigDict(extra="ignore")


class AnalysisInput(BaseModel):
    """
    Input contract for Analysis agents.

    Provides collector output and configuration for analyzing
    database suitability for a specific target database service.

    Version history:
    - 2.0: Initial version with MAJOR.MINOR versioning
    """

    contract_version: str = Field(
        default="2.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )

    job_id: str = Field(description="Unique job identifier")

    # Collector output (as dict to avoid circular import)
    collector_output: dict = Field(description="Complete output from Collector agent")

    target_database: TargetDatabase = Field(description="Target database service to analyze for")

    analysis_options: AnalysisOptions = Field(
        default_factory=AnalysisOptions, description="Options controlling analysis behavior"
    )

    # AWS region for cost estimation
    target_region: str = Field(default="us-east-1", description="AWS region for cost estimation")

    # Execution options
    timeout_minutes: int = Field(
        default=60, ge=10, le=180, description="Maximum analysis time in minutes (default: 1 hour)"
    )

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "example": {
                "contract_version": "2.0",
                "job_id": "job-123",
                "collector_output": {
                    "job_id": "job-123",
                    "database_metadata": {"engine": "mysql", "table_count": 100},
                },
                "target_database": "dynamodb",
                "analysis_options": {
                    "perform_cost_estimation": True,
                    "perform_load_testing": False,
                },
                "target_region": "us-east-1",
            }
        },
    )
