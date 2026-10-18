"""
Analysis Output Contract Models

Version History:
- 2.2 (2026-10-18): Add KeyScopedWrite and KeyAnchor models and optional key_scoped_writes
  field (#1061)
- 2.1 (2026-03-17): Add AggregateRecommendation model and optional aggregate_recommendations field
- 2.0 (2026-02-02): Updated to MAJOR.MINOR versioning
- 1.0 (2026-01-21): Initial version - Output contract for all Analysis agents
"""

from collections.abc import Collection, Iterable, Mapping
from datetime import datetime
from enum import Enum
from typing import Any, Literal

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


class KeyAnchor(BaseModel):
    """One key column a key-scoped write pins (#1061): its collected cardinality, or
    ``"unknown"`` when the collector has no statistics for it, and the other queries that look
    the table up by it when the workload makes it a key (none for a primary key,
    foreign key or index column). A low- or unknown-cardinality anchor is still a key:
    the schema design decides how to model it (a partition key, or part of a sort key,
    a GSI or a filter)."""

    column: str
    cardinality: int | Literal["unknown"]
    lookup_query_ids: list[str] = Field(default_factory=list)


class KeyScopedWrite(BaseModel):
    """A write the analysis found key scoped (ADR-031 Decision 3, #915, #1061): it changes
    the rows one value of each of ``columns`` addresses, a key anchor that is not a full
    primary or unique key of ``table_id``. Its schema-design requirement is
    ``schema_note``: each column must become the partition key of an item collection (or
    a GSI's) so the write reads its items by key (a Query plus a batch write)."""

    query_id: str
    table_id: str
    columns: list[str] = Field(..., min_length=1)
    schema_note: str
    # Per column of ``columns``: its cardinality and the lookups behind it (#1061).
    anchors: list[KeyAnchor] = Field(default_factory=list)

    def in_scope(self, query_ids: Collection[str]) -> "KeyScopedWrite | None":
        """This write as one design call sees it (#1104 review): None when the write is
        not one of ``query_ids`` (another group's, or a query another engine serves),
        else the write with each anchor's ``lookup_query_ids`` trimmed to ``query_ids``,
        so it never names a query the designer cannot see."""
        if self.query_id not in query_ids:
            return None
        anchors = [
            a.model_copy(
                update={"lookup_query_ids": [q for q in a.lookup_query_ids if q in query_ids]}
            )
            for a in self.anchors
        ]
        return self.model_copy(update={"anchors": anchors})


def key_scoped_writes_in_scope(
    writes: Iterable[Mapping[str, Any]] | None, query_ids: Collection[str]
) -> list[dict[str, Any]]:
    """The raw ``key_scoped_writes`` of an analysis file, as one design call sees them
    (``KeyScopedWrite.in_scope``): the one scoping every designer input path uses (the
    external request, each design group's input and the Bedrock projection)."""
    out = []
    for raw in writes or []:
        scoped = KeyScopedWrite.model_validate(raw).in_scope(query_ids)
        if scoped is not None:
            out.append(scoped.model_dump(mode="json"))
    return out


class AnalysisOutputContract(BaseModel):
    """Output contract for all Analysis agents"""

    contract_version: str = Field(
        default="2.2",
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
    key_scoped_writes: list[KeyScopedWrite] | None = Field(
        None,
        description=(
            "DynamoDB analysis only: every key-scoped write and the key it needs (#1061); "
            "the DynamoDB schema designer reads it"
        ),
    )

    model_config = ConfigDict(use_enum_values=True, extra="ignore")  # Forward compatibility
