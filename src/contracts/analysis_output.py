"""
Analysis Output Contract Models

Version History:
- 2.1 (2026-03-17): Add AggregateRecommendation model and optional aggregate_recommendations field
- 2.0 (2026-02-02): Updated to MAJOR.MINOR versioning
- 1.0 (2026-01-21): Initial version - Output contract for all Analysis agents
"""

from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class TargetDatabase(str, Enum):
    """Supported target databases for migration analysis"""

    DYNAMODB = "dynamodb"
    DOCUMENTDB = "documentdb"
    ELASTICACHE = "elasticache"
    OPENSEARCH = "opensearch"
    NEPTUNE = "neptune"
    KEYSPACES = "keyspaces"
    AURORA = "aurora"
    AURORA_POSTGRESQL = "aurora_postgresql"
    AURORA_MYSQL = "aurora_mysql"


class RecommendationLevel(str, Enum):
    """Overall recommendation levels for table migration suitability"""

    HIGHLY_SUITABLE = "HIGHLY_SUITABLE"
    SUITABLE = "SUITABLE"
    MARGINAL = "MARGINAL"
    NOT_SUITABLE = "NOT_SUITABLE"


class MigrationComplexity(str, Enum):
    """Migration complexity levels"""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Confidence(str, Enum):
    """Confidence levels for pattern detection"""

    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class Severity(str, Enum):
    """Severity levels for anti-patterns"""

    CRITICAL = "CRITICAL"
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class AgentMetadata(BaseModel):
    """Metadata about the analysis agent and execution"""

    agent_name: str = Field(
        ..., description="Name of analysis agent (e.g., 'dynamodb-analysis-agent')"
    )
    agent_version: str = Field(
        ..., pattern=r"^\d+\.\d+\.\d+$", description="Semantic version of analysis agent"
    )
    target_database: TargetDatabase = Field(description="Target AWS database service")
    analysis_timestamp: datetime = Field(description="ISO 8601 timestamp when analysis completed")
    analysis_duration_seconds: float | None = Field(
        None, ge=0, description="Time taken to complete analysis in seconds"
    )


class ScoreBreakdown(BaseModel):
    """Detailed scoring breakdown for table recommendations"""

    pattern_match_score: int = Field(
        ...,
        ge=0,
        le=100,
        description="How well query patterns match target database strengths (weight: 40%)",
    )
    complexity_score: int = Field(
        ..., ge=0, le=100, description="Migration complexity (lower is better, weight: 30%)"
    )
    performance_score: int = Field(
        ..., ge=0, le=100, description="Expected performance improvement (weight: 20%)"
    )
    cost_score: int = Field(..., ge=0, le=100, description="Cost efficiency (weight: 10%)")


class TableRecommendation(BaseModel):
    """Recommendation for migrating a specific table to target database"""

    table_id: str = Field(..., description="References table_id from Collector output")
    confidence_score: int = Field(
        ...,
        ge=0,
        le=100,
        description="Confidence score (0-100) that this table is suitable for target database",
    )
    rationale: str | None = Field(None, description="Human-readable explanation of recommendation")
    score_breakdown: ScoreBreakdown
    supporting_patterns: list[str] | None = Field(
        None, description="List of pattern IDs that support this recommendation"
    )
    concerns: list[str] | None = Field(
        None, description="List of concerns or caveats for this recommendation"
    )
    migration_complexity: MigrationComplexity | None = Field(
        None, description="Estimated migration complexity"
    )


class Pattern(BaseModel):
    """Detected workload pattern that supports migration decision"""

    pattern_id: str = Field(..., description="Unique identifier for this pattern")
    pattern_type: str = Field(
        ...,
        description="Type of pattern detected (e.g., 'key-value-lookup', 'time-series', 'graph-traversal')",
    )
    confidence: Confidence = Field(..., description="Confidence in pattern detection")
    description: str | None = Field(None, description="Human-readable description of the pattern")
    query_ids: list[str] | None = Field(None, description="Query IDs that exhibit this pattern")
    table_ids: list[str] | None = Field(None, description="Table IDs involved in this pattern")
    frequency_percent: float | None = Field(
        None, ge=0, le=100, description="Percentage of total workload represented by this pattern"
    )


class AntiPattern(BaseModel):
    """Detected anti-pattern that may impact migration success"""

    anti_pattern_id: str
    anti_pattern_type: str = Field(
        ...,
        description="Type of anti-pattern (e.g., 'hot-partition', 'excessive-scanning', 'unbounded-queries')",
    )
    severity_weight: float = Field(
        ..., ge=0.0, le=1.0, description="Severity weight (0.0 negligible to 1.0 critical)"
    )
    description: str | None = None
    query_ids: list[str] | None = None
    table_ids: list[str] | None = None
    recommendation: str | None = Field(None, description="How to address this anti-pattern")


class WorkloadAnalysis(BaseModel):
    """Analysis of workload patterns and anti-patterns"""

    patterns_detected: list[Pattern]
    anti_patterns_detected: list[AntiPattern] | None = None


class CostEstimate(BaseModel):
    """Cost estimation for target database migration"""

    monthly_cost_usd: float = Field(..., ge=0, description="Estimated monthly cost in USD")
    cost_components: dict[str, Any] = Field(
        ..., description="Breakdown of cost components (database-specific)"
    )
    pricing_assumptions: list[str] | None = Field(
        None, description="List of assumptions made in cost calculation"
    )


class PerformanceMetrics(BaseModel):
    """Performance metrics from load testing"""

    p50_latency_ms: float | None = Field(None, ge=0)
    p95_latency_ms: float | None = Field(None, ge=0)
    p99_latency_ms: float | None = Field(None, ge=0)
    throughput_ops_per_second: float | None = Field(None, ge=0)


class LoadTestResults(BaseModel):
    """Optional load test results if performed"""

    test_performed: bool | None = None
    test_tables_created: list[str] | None = None
    performance_metrics: PerformanceMetrics | None = None


class AggregateRecommendation(BaseModel):
    """Recommendation for migrating a group of related tables together."""

    aggregate_id: str = Field(
        ..., description="Unique identifier for this aggregate (e.g., 'agg-orders')"
    )
    root_table: str = Field(..., description="table_id of the aggregate root table")
    member_tables: list[str] = Field(..., description="All table_ids in this aggregate")
    co_access_confidence: int = Field(
        ...,
        ge=0,
        le=100,
        description="Confidence (0-100) that these tables are co-accessed and should migrate together",
    )
    combined_migration_complexity: MigrationComplexity = Field(
        ..., description="Combined migration complexity for the aggregate group"
    )
    # LLM-enriched key design fields (populated by advisor, None when deterministic-only)
    partition_key: str | None = Field(
        default=None, description="Recommended DynamoDB partition key column"
    )
    sort_key: str | None = Field(default=None, description="Recommended DynamoDB sort key column")
    key_design_rationale: str | None = Field(
        default=None, description="Design reasoning for the key choice"
    )
    supporting_access_patterns: list[str] = Field(
        default_factory=list,
        description="query_ids that justify this key design",
    )


class AnalysisOutputContract(BaseModel):
    """Output contract for all Analysis agents"""

    contract_version: str = Field(
        default="2.1",
        pattern=r"^\d+\.\d+$",
        description="Contract version (MAJOR.MINOR format)",
    )
    agent_metadata: AgentMetadata = Field(
        ..., description="Analysis agent metadata including name, version, and timestamp"
    )
    table_recommendations: list[TableRecommendation] = Field(
        ..., description="Recommendations for each table analyzed"
    )
    workload_analysis: WorkloadAnalysis = Field(
        ..., description="Detected patterns and anti-patterns in workload"
    )
    cost_estimate: CostEstimate = Field(
        ..., description="Estimated monthly cost for target database"
    )
    load_test_results: LoadTestResults | None = Field(
        None, description="Optional load test results if performed"
    )
    aggregate_recommendations: list[AggregateRecommendation] | None = Field(
        None, description="Optional aggregate recommendations when table aggregates are detected"
    )

    model_config = ConfigDict(use_enum_values=True, extra="ignore")  # Forward compatibility
