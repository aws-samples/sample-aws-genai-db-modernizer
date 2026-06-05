"""
Referee Input Contract (Pydantic Model)

Version: 2.0
"""

from pydantic import BaseModel, ConfigDict, Field


class RefereeOptions(BaseModel):
    """Options controlling referee behavior"""

    prioritization_strategy: str = Field(
        default="impact_effort",
        description="Strategy for prioritizing recommendations (impact_effort, cost, risk)",
    )

    include_quick_wins: bool = Field(
        default=True, description="Include quick win recommendations (<2 weeks effort)"
    )

    include_strategic: bool = Field(
        default=True, description="Include strategic recommendations (1-3 months effort)"
    )

    include_long_term: bool = Field(
        default=True, description="Include long-term recommendations (3-12 months effort)"
    )

    max_recommendations: int = Field(
        default=20, ge=5, le=50, description="Maximum number of recommendations to include"
    )

    model_config = ConfigDict(extra="ignore")


class RefereeInput(BaseModel):
    """
    Input contract for Referee agent.

    Provides collector output, all analysis outputs, and configuration
    for synthesizing final modernization recommendations.

    Version history:
    - 2.0: Initial version with MAJOR.MINOR versioning
    """

    contract_version: str = Field(
        default="2.0", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )

    job_id: str = Field(description="Unique job identifier")

    # Collector output (as dict to avoid circular import)
    collector_output: dict = Field(description="Complete output from Collector agent")

    # Analysis outputs from all analysis agents
    analysis_outputs: list[dict] = Field(
        description="Outputs from all analysis agents (1-7 agents)", min_length=1, max_length=7
    )

    # Current costs for TCO comparison
    current_monthly_cost: float | None = Field(
        default=None, ge=0, description="Current monthly database costs (for TCO comparison)"
    )

    referee_options: RefereeOptions = Field(
        default_factory=RefereeOptions, description="Options controlling referee behavior"
    )

    # Execution options
    timeout_minutes: int = Field(
        default=120,
        ge=30,
        le=240,
        description="Maximum referee processing time in minutes (default: 2 hours)",
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
                "analysis_outputs": [
                    {"job_id": "job-123", "target_database": "dynamodb", "confidence_score": 0.85},
                    {
                        "job_id": "job-123",
                        "target_database": "aurora_mysql",
                        "confidence_score": 0.92,
                    },
                ],
                "current_monthly_cost": 500.0,
                "referee_options": {
                    "prioritization_strategy": "impact_effort",
                    "max_recommendations": 20,
                },
            }
        },
    )
