"""
Pydantic models for Referee Output Contract

This module defines the output contract for the Referee agent, which produces
the final database modernization assessment. The output includes:
- Recommended database architecture
- Table-to-database mappings
- TCO (Total Cost of Ownership) analysis
- Risk assessment with mitigation strategies
- Optional assessment report URLs (HTML/PDF)

Version History:
- v2.0 (2026-02-02): Updated to MAJOR.MINOR versioning per ADR-008
- v1.0 (2026-01-21): Initial version based on referee-output.json schema

Related Documentation:
- docs/03-contracts/agent-contracts-spec.md - Contract specification
- docs/02-architecture/decisions/ADR-008-contract-versioning.md - Versioning strategy
"""

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class ArchitectureType(str, Enum):
    """Type of recommended architecture"""

    SINGLE_DATABASE = "SINGLE_DATABASE"
    MULTI_DATABASE = "MULTI_DATABASE"
    HYBRID_WITH_CACHE = "HYBRID_WITH_CACHE"


class DatabaseService(str, Enum):
    """AWS Database services"""

    DYNAMODB = "DynamoDB"
    DOCUMENTDB = "DocumentDB"
    ELASTICACHE = "ElastiCache"
    OPENSEARCH = "OpenSearch"
    NEPTUNE = "Neptune"
    KEYSPACES = "Keyspaces"
    AURORA_POSTGRESQL = "Aurora PostgreSQL"
    AURORA_MYSQL = "Aurora MySQL"


class RiskLevel(str, Enum):
    """Risk severity levels"""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


class RiskType(str, Enum):
    """Types of migration risks"""

    DATA_CONSISTENCY = "DATA_CONSISTENCY"
    PERFORMANCE_DEGRADATION = "PERFORMANCE_DEGRADATION"
    COST_OVERRUN = "COST_OVERRUN"
    MIGRATION_COMPLEXITY = "MIGRATION_COMPLEXITY"
    OPERATIONAL_RISK = "OPERATIONAL_RISK"
    SECURITY_RISK = "SECURITY_RISK"


class CacheType(str, Enum):
    """ElastiCache types"""

    REDIS = "ElastiCache Redis"
    MEMCACHED = "ElastiCache Memcached"


class CostBreakdown(BaseModel):
    """Cost breakdown per database"""

    database: str = Field(description="Database service name")
    monthly_cost_usd: float = Field(ge=0, description="Monthly cost in USD")


class ThreeYearTCO(BaseModel):
    """Three-year total cost of ownership"""

    current: float | None = Field(None, ge=0, description="Current 3-year TCO in USD")
    projected: float | None = Field(None, ge=0, description="Projected 3-year TCO in USD")
    savings: float | None = Field(None, description="3-year savings in USD")


class CachingLayer(BaseModel):
    """Caching layer recommendation"""

    recommended: bool | None = Field(None, description="Whether caching layer is recommended")
    type: CacheType | None = Field(None, description="Type of caching service")
    rationale: str | None = Field(None, description="Rationale for caching recommendation")


class Alternative(BaseModel):
    """Alternative database recommendation"""

    database: str = Field(description="Alternative database service name")
    confidence_score: int = Field(ge=0, le=100, description="Confidence score for alternative")


class DatabaseAllocation(BaseModel):
    """Database allocation for tables"""

    service: DatabaseService = Field(description="AWS database service")
    table_count: int = Field(ge=0, description="Number of tables allocated to this database")
    rationale: str = Field(description="Why these tables are allocated to this database")
    tables: list[str] | None = Field(
        None, description="List of table IDs assigned to this database"
    )
    confidence_score: int | None = Field(
        None, ge=0, le=100, description="Confidence score for this allocation"
    )


class TableMapping(BaseModel):
    """Mapping of source table to recommended database"""

    source_table: str = Field(description="Source table ID")
    recommended_database: str = Field(description="Recommended target database service")
    confidence_score: int = Field(ge=0, le=100, description="Confidence score for recommendation")
    alternatives: list[Alternative] | None = Field(
        None, description="Alternative database recommendations with lower confidence"
    )
    caching_layer: CachingLayer | None = Field(
        None, description="Optional caching layer recommendation"
    )


class Risk(BaseModel):
    """Migration risk assessment"""

    risk_id: str = Field(description="Unique risk identifier")
    risk_type: RiskType = Field(description="Type of risk")
    severity: RiskLevel = Field(description="Risk severity level")
    description: str = Field(description="Detailed risk description")
    affected_tables: list[str] | None = Field(None, description="Tables affected by this risk")
    mitigation: str | None = Field(None, description="Recommended mitigation strategy")


class RecommendedArchitecture(BaseModel):
    """Recommended database architecture"""

    databases: list[DatabaseAllocation] = Field(
        min_length=1, description="List of database allocations"
    )
    architecture_type: ArchitectureType = Field(description="Type of recommended architecture")
    architecture_diagram_url: str | None = Field(
        None, description="URL to generated architecture diagram"
    )
    rationale: str | None = Field(
        None, description="Overall rationale for architecture recommendation"
    )


class TCOAnalysis(BaseModel):
    """Total cost of ownership analysis"""

    current_monthly_cost: float = Field(ge=0, description="Current monthly database cost (USD)")
    projected_monthly_cost: float = Field(
        ge=0, description="Projected monthly cost with recommended architecture (USD)"
    )
    savings_percent: float = Field(
        description="Percentage cost savings (can be negative if cost increases)"
    )
    cost_breakdown: list[CostBreakdown] | None = Field(
        None, description="Cost breakdown by database service"
    )
    three_year_tco: ThreeYearTCO | None = Field(
        None, description="Three-year total cost of ownership projection"
    )
    assumptions: list[str] | None = Field(
        None, description="List of assumptions in TCO calculation"
    )


class RiskAssessment(BaseModel):
    """Risk assessment for migration"""

    overall_risk_level: RiskLevel = Field(description="Overall risk level for migration")
    risks: list[Risk] = Field(description="List of identified risks")
    mitigation_strategies: list[str] | None = Field(
        None, description="Recommended mitigation strategies"
    )


class AssessmentReport(BaseModel):
    """Assessment report URLs"""

    contract_version: str = Field(
        default="2.0",
        pattern=r"^\d+\.\d+$",
        description="Contract version (MAJOR.MINOR format)",
    )
    html_url: str | None = Field(None, description="URL to HTML assessment report")
    pdf_url: str | None = Field(None, description="URL to PDF assessment report")


class RefereeOutputContract(BaseModel):
    """
    Output contract for Referee agent

    This is the primary output of the Referee agent, containing the complete
    database modernization assessment with recommendations, cost analysis, and risk assessment.

    The Referee agent aggregates outputs from all analysis agents (DynamoDB, Aurora,
    DocumentDB, ElastiCache, OpenSearch, Neptune, Keyspaces) and produces a unified
    recommendation with confidence scores and detailed rationale.

    Key Features:
    - Multi-database architecture recommendations (single, multi, or hybrid with cache)
    - Per-table database mappings with confidence scores
    - TCO analysis comparing current vs projected costs
    - Risk assessment with severity levels and mitigation strategies
    - Optional HTML/PDF report generation

    Example Usage:
        ```python
        from src.contracts.referee_output import RefereeOutputContract

        output = RefereeOutputContract(
            contract_version="2.0",
            recommended_architecture=...,
            table_mappings=...,
            tco_analysis=...,
            risk_assessment=...
        )
        ```
    """

    contract_version: str = Field(
        default="2.0",
        pattern=r"^\d+\.\d+$",
        description="Contract version (MAJOR.MINOR format)",
    )
    recommended_architecture: RecommendedArchitecture = Field(
        description="Recommended database architecture with service allocations"
    )
    table_mappings: list[TableMapping] = Field(
        description="Mapping of source tables to target databases"
    )
    tco_analysis: TCOAnalysis = Field(
        description="Total cost of ownership analysis and projections"
    )
    risk_assessment: RiskAssessment = Field(
        description="Risk assessment with mitigation strategies"
    )
    assessment_report: AssessmentReport | None = Field(
        None, description="Assessment report URLs (HTML/PDF)"
    )
    schema_design_requested: bool | None = Field(
        False, description="Whether customer requested detailed schema design"
    )
    selected_databases: list[str] | None = Field(
        None, description="List of databases customer selected for schema design"
    )

    model_config = ConfigDict(extra="ignore")  # Forward compatibility
