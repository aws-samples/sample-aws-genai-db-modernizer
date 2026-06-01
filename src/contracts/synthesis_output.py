"""
Synthesis Output Contract

Data models for the synthesis agent output: the final modernization report
with ranking, architecture, table mappings, query groups, TCO, and risks.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .schema_design_output import TradeOff


class EngineRanking(BaseModel):
    """Ranking entry for a target engine."""

    target: str = Field(..., description="Engine name")
    confidence_score: int = Field(..., ge=0, le=100, description="Overall confidence")
    pattern_score: int | None = Field(None, ge=0, le=100)
    complexity_score: int | None = Field(None, ge=0, le=100)
    performance_score: int | None = Field(None, ge=0, le=100)
    cost_score: int | None = Field(None, ge=0, le=100)
    migration_complexity_avg: str | None = Field(None, description="LOW, MEDIUM, HIGH")
    assigned_queries: int | None = Field(None, ge=0)
    workload_percent: float | None = Field(None, ge=0, le=100)

    model_config = ConfigDict(extra="allow")


class TableMapping(BaseModel):
    """Mapping of a source table to its target engine."""

    source_table: str = Field(..., description="Source table ID")
    recommended_database: str = Field(..., description="Primary target engine")
    confidence_score: int = Field(..., ge=0, le=100)
    alternatives: list[dict] | None = Field(None)

    model_config = ConfigDict(extra="allow")


class QueryGroup(BaseModel):
    """Group of queries organized by access pattern."""

    group_name: str = Field(..., description="Human-readable group name")
    engines: list[str] = Field(default_factory=list, description="Engines serving this group")
    access_patterns: list[dict] = Field(
        default_factory=list, description="Access patterns in this group"
    )

    model_config = ConfigDict(extra="allow")


class CostBreakdown(BaseModel):
    """Cost breakdown per engine."""

    database: str = Field(..., description="Engine name")
    monthly_cost_usd: float = Field(..., ge=0)

    model_config = ConfigDict(extra="allow")


class TCOAnalysis(BaseModel):
    """Total cost of ownership analysis."""

    current_monthly_cost: float = Field(..., ge=0)
    projected_monthly_cost: float = Field(..., ge=0)
    savings_percent: float = Field(...)
    cost_breakdown: list[CostBreakdown] | None = Field(None)
    assumptions: list[str] | None = Field(None)

    model_config = ConfigDict(extra="allow")


class Risk(BaseModel):
    """A single migration risk."""

    severity: str = Field(..., description="LOW, MEDIUM, HIGH, CRITICAL")
    description: str = Field(...)

    model_config = ConfigDict(extra="allow")


class RiskAssessment(BaseModel):
    """Risk assessment for the migration."""

    overall_risk_level: str = Field(..., description="LOW, MEDIUM, HIGH, CRITICAL")
    risks: list[Risk] = Field(...)
    mitigation_strategies: list[str] | None = Field(None)

    model_config = ConfigDict(extra="allow")


class AssignmentSummary(BaseModel):
    """Summary of the query-to-engine assignment used for synthesis."""

    version: int | None = Field(None, ge=1)
    status: str | None = Field(None)
    query_count: int = Field(..., ge=0)
    in_scope_count: int = Field(..., ge=0)
    co_dependency_groups: int = Field(default=0, ge=0)


class SynthesisOutputContract(BaseModel):
    """Output contract for the synthesis agent.

    The final modernization report combining all pipeline artifacts into
    a comprehensive assessment with architecture, TCO, and risk analysis.

    Consumed by:
    - The UI (results page — architecture view, query groups, table mappings)
    - Step Functions (needs_deeper_analysis flag for the analysis loop)
    """

    contract_version: str = Field(
        default="1.0",
        pattern=r"^\d+\.\d+$",
        description="Contract version (MAJOR.MINOR format)",
    )
    job_id: str = Field(..., description="Job identifier")
    database_name: str = Field(..., description="Source database name")
    agent_type: str = Field(default="referee-synthesis", description="Agent identifier")
    status: str = Field(default="completed", description="Pipeline status")
    timestamp: datetime = Field(..., description="When synthesis was run")
    needs_deeper_analysis: bool = Field(
        default=False, description="Whether any engine needs further analysis"
    )
    ranking: list[EngineRanking] = Field(..., description="Engine rankings by confidence")
    summary: str = Field(..., description="Executive summary text")
    summary_deterministic: str = Field(..., description="Deterministic summary (no LLM)")
    recommended_architecture: dict[str, Any] = Field(
        ..., description="Architecture recommendation with type, databases, integrations"
    )
    table_mappings: list[TableMapping] = Field(
        ..., description="Source table to target engine mappings"
    )
    query_groups: list[QueryGroup] = Field(..., description="Queries grouped by access pattern")
    tco_analysis: TCOAnalysis = Field(..., description="Total cost of ownership analysis")
    risk_assessment: RiskAssessment = Field(..., description="Migration risk assessment")
    schema_designs: dict[str, Any] = Field(
        default_factory=dict, description="Per-engine schema design summaries"
    )
    trade_offs: list[TradeOff] = Field(default_factory=list, description="Collected trade-offs")
    assignment_summary: AssignmentSummary | None = Field(
        None, description="Summary of the assignment version used"
    )

    model_config = ConfigDict(extra="allow")
