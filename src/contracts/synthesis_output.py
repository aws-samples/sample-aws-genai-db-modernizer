"""
Synthesis Output Contract

Data models for the synthesis agent output: the final modernization report
with ranking, architecture, table mappings, query groups, TCO, and risks.

Version History:
- 1.0: Initial synthesis output contract.
- 1.1 (2026-08-27): Added optional ``source`` provenance to ``AssignmentSummary``
  so a consumer can see which pipeline stage produced the consumed assignment
  version (ADR-028). Backward compatible — ``source`` defaults to ``None``.
- 1.2 (2026-10-04): Added optional ``cache_overlay`` (#296): the cache layer's
  queries and share of calls, which the owner distribution never counts, plus the
  notes of the post-schema safety net. A cache-layer ``ranking`` entry carries
  ``role: "cache_layer"``, ``cache_overlay_queries`` and
  ``cache_call_share_percent``. Backward compatible — defaults to ``None``.
- 1.3 (2026-10-04): Added optional ``routed_confidence`` to ``EngineRanking``
  (#152): the mean per-query fit of the queries the effective assignment routes
  to the engine (for the cache layer, of the reads it fronts; see
  ``routed_confidence_basis``), with ``routed_queries``, ``routed_tables`` (real
  source tables only), ``routed_confidence_evidence`` (``table`` / ``partial`` /
  ``signal_only``: whether a rated source table backs the fit),
  ``routed_queries_without_table_evidence``, ``routed_lead`` and
  ``routed_lead_count`` (the signal or cache pattern that leads the routed queries)
  and the engine's ``rationale``.
  ``analysis_confidence`` (= ``confidence_score``, the average over every analyzed
  table) and ``weight`` are declared as audit fields. The ranking is ordered by
  workload share. Backward compatible — every new field defaults to ``None``.
"""

from datetime import datetime
from typing import Any, Literal

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
    analysis_confidence: int | None = Field(
        None,
        ge=0,
        le=100,
        description="Audit: suitability averaged over every analyzed table (= confidence_score)",
    )
    routed_confidence: int | None = Field(
        None,
        ge=0,
        le=100,
        description=(
            "Mean per-query fit of the queries routed to the engine (cache layer: of the "
            "reads it fronts). None without an assignment or with no routed query (#152)"
        ),
    )
    routed_confidence_basis: Literal["owned_queries", "cached_reads"] | None = Field(None)
    routed_queries: int | None = Field(None, ge=0)
    routed_tables: int | None = Field(None, ge=0, description="Real source tables only")
    routed_confidence_evidence: Literal["table", "partial", "signal_only"] | None = Field(
        None,
        description=(
            "Whether a source table the engine's analysis rated backs the fit; signal_only "
            "means the fit is the basic baseline plus the signal bonus"
        ),
    )
    routed_queries_without_table_evidence: int | None = Field(None, ge=0)
    routed_lead: str | None = Field(
        None,
        description=(
            "Owners: the triage signal that is a plurality of the routed queries; cache "
            "layer: the most common cache_pattern. None without a clear plurality"
        ),
    )
    routed_lead_count: int | None = Field(None, ge=0)
    rationale: str | None = Field(
        None, description="Why the engine is in the target, from its routed workload (#152)"
    )
    weight: float | None = Field(
        None, description="Audit: the analysis weight that ordered the ranking before #152"
    )

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
    source: str | None = Field(
        None,
        description=(
            "Provenance of the assignment version this report was built from "
            "(assignment_resolution | reality_check | customer_gate). ADR-028."
        ),
    )
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
        default="1.3",
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
    ranking: list[EngineRanking] = Field(
        ..., description="Engines by workload share, owners first, then the cache layer (#152)"
    )
    summary: str = Field(..., description="Executive summary text")
    summary_deterministic: str = Field(..., description="Deterministic summary (no LLM)")
    summary_source: str = Field(
        default="deterministic",
        description=(
            "Which text ``summary`` holds: llm, deterministic, or deterministic_fallback "
            "(the LLM summary failed the table-assignment post-check)"
        ),
    )
    summary_llm: str | None = Field(
        None, description="LLM-written summary as received, kept for audit even when rejected"
    )
    summary_validation_warnings: list[str] = Field(
        default_factory=list,
        description="Post-check findings: summary sentences naming a table under the wrong engine",
    )
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
    cache_overlay: dict[str, Any] | None = Field(
        None,
        description=(
            "Cache layer view (#296): engine, query_count, calls_per_second, "
            "call_share_percent, owners, patterns, dropped_query_ids and notes. The cache "
            "owns no query, so it is never part of the owner workload share."
        ),
    )

    model_config = ConfigDict(extra="allow")
