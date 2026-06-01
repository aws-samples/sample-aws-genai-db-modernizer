"""
Reality Check Output Contract

Data models for the reality check agent output: unique value assessment,
consolidation decisions, architectural patterns, and recommendations.

Version History:
- 1.0 (2026-04-16): Initial version — CTO-level engine consolidation
"""

from pydantic import BaseModel, Field


class UniqueValueAssessment(BaseModel):
    """Per-engine assessment of unique vs redundant queries.

    A query is "unique" to an engine when the engine's fit score exceeds
    the best alternative by at least UNIQUE_DELTA_THRESHOLD points.
    """

    total_queries: int = Field(..., ge=0, description="Total queries assigned to this engine")
    unique_queries: list[str] = Field(
        ..., description="Query IDs where this engine has a significant advantage"
    )
    redundant_queries: list[str] = Field(
        ..., description="Query IDs where another engine is nearly as good"
    )
    unique_ratio: float = Field(
        ..., ge=0, le=1, description="Fraction of queries that are unique (0.0-1.0)"
    )
    avg_delta: float = Field(
        ...,
        description="Average fit score delta (this engine vs best alternative) across non-mandatory queries",
    )
    is_primary: bool = Field(..., description="Whether this engine was selected as the primary")
    is_mandatory: bool = Field(
        ..., description="Whether this engine has mandatory signal overrides"
    )
    consolidation_blocked: str | None = Field(
        None, description="Reason consolidation was blocked (if applicable)"
    )


class Consolidation(BaseModel):
    """Record of queries moved from one engine to another."""

    from_engine: str = Field(..., description="Engine queries were moved away from")
    to_engine: str = Field(..., description="Engine queries were moved to")
    query_count: int = Field(..., ge=1, description="Number of queries moved")
    reason: str = Field(..., description="Why these queries were consolidated")
    saved_cost_estimate: float = Field(
        ..., ge=0, description="Estimated monthly cost savings in USD"
    )
    action: str = Field(default="full", description="Consolidation type: 'full' or 'partial'")
    queries_retained: list[str] = Field(
        default_factory=list,
        description="Query IDs retained on source engine (partial consolidation only)",
    )
    retention_reason: str | None = Field(
        None, description="Why retained queries could not be moved"
    )


class LightweightRecommendation(BaseModel):
    """Recommendation to use a lightweight managed service instead of a full engine."""

    capability: str = Field(..., description="The capability that requires this service")
    service: str = Field(..., description="AWS service name (e.g., 'Amazon Athena + S3')")
    query_ids: list[str] = Field(..., description="Query IDs served by this recommendation")
    pattern: str = Field(..., description="Integration pattern description")
    cost_profile: str = Field(..., description="Cost characteristics")
    replaces_engine: str = Field(
        ..., description="The engine this recommendation replaces for these queries"
    )
    limitations: str = Field(..., description="Known limitations of this approach")


class ArchitecturalPattern(BaseModel):
    """Detected architectural pattern for multi-engine setup."""

    name: str = Field(..., description="Pattern name (e.g., CQRS, Materialized View)")
    description: str = Field(..., description="What the pattern does")
    when: str = Field(..., description="When this pattern applies")
    example: str = Field(..., description="Concrete example for this workload")
    applies_to: dict = Field(..., description="Engines and roles this pattern applies to")


class RealityCheckOutputContract(BaseModel):
    """Output contract for the reality check agent.

    Captures the CTO-level decision: which engines earn their place,
    which get consolidated, and what architectural patterns emerge.
    """

    contract_version: str = Field(default="1.1", description="Contract version")
    source_assignment_version: int = Field(
        ..., ge=1, description="Base assignment version this check was run against"
    )
    unique_value_assessment: dict[str, UniqueValueAssessment] = Field(
        default_factory=dict, description="Per-engine unique value analysis"
    )
    consolidations: list[Consolidation] = Field(
        ..., description="List of consolidation decisions (empty if no changes)"
    )
    architectural_patterns: list[ArchitecturalPattern] = Field(
        ..., description="Detected architectural patterns"
    )
    executive_summary: str | None = Field(
        None,
        description="LLM-generated executive summary of the workload and architecture recommendation (2-3 sentences)",
    )
    recommendations: list[str] = Field(..., description="Human-readable recommendations")
    before_distribution: dict[str, int] = Field(
        ..., description="Query count per engine before reality check"
    )
    after_distribution: dict[str, int] = Field(
        ..., description="Query count per engine after reality check"
    )
    lightweight_recommendations: list[LightweightRecommendation] = Field(
        default_factory=list,
        description="Lightweight managed-service alternatives for small orphan query sets",
    )
