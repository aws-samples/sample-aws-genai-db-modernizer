"""Aurora MySQL schema-design output contract.

Relational target: table definitions with translated column types (carrying
provenance), the full generated DDL, application-layer notes for features that
cannot be translated mechanically, and LLM-authored Aurora optimizations.
Mirrors the envelope of the other per-engine contracts (see
``documentdb_model_output.py``).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .schema_design_output import TradeOff


class TargetColumn(BaseModel):
    name: str = Field(..., description="Column name")
    aurora_type: str = Field(..., description="Resolved Aurora MySQL type, e.g. VARCHAR(255)")
    source_type: str | None = Field(None, description="Source NormalizedDataType, if known")
    script_derived: bool = Field(
        ..., description="True if the type was resolved deterministically by the script"
    )
    needs_judgment: bool = Field(
        default=False, description="True if the LLM confirmed/refined an ambiguous type"
    )

    model_config = ConfigDict(extra="ignore")


class TargetTable(BaseModel):
    table_name: str = Field(..., description="Target Aurora MySQL table name")
    columns: list[TargetColumn] = Field(
        ..., min_length=1, description="Translated column definitions"
    )
    primary_key: list[str] = Field(default_factory=list, description="Primary key column names")
    indexes: list[str] = Field(default_factory=list, description="CREATE INDEX statements")
    foreign_keys: list[str] = Field(default_factory=list, description="FK ALTER TABLE statements")

    model_config = ConfigDict(extra="ignore")


class AppLayerNote(BaseModel):
    """A source feature that cannot be translated to DDL and needs app-layer handling."""

    feature: str = Field(..., description="e.g. 'trigger', 'sequence', 'stored_procedure'")
    source_object: str = Field(..., description="Name of the source object")
    recommendation: str = Field(..., description="Recommended application-layer replacement")

    model_config = ConfigDict(extra="ignore")


class Optimization(BaseModel):
    """An Aurora-specific optimization recommended by the LLM from query patterns."""

    category: Literal["partitioning", "index", "read_replica", "io_optimized", "other"] = Field(
        ..., description="Optimization category"
    )
    target: str = Field(..., description="Table/column/cluster the optimization applies to")
    recommendation: str = Field(..., description="What to do")
    rationale: str = Field(..., description="Why this optimization is recommended")

    model_config = ConfigDict(extra="ignore")


class AccessPattern(BaseModel):
    """A query the Aurora target must serve, derived from the observed query log.

    Script-derived, not LLM-authored. ``design_rps`` is the collector's measured
    ``calls_per_second``; the source latency and DB-load percentages travel with
    the pattern so a load test can report a per-query before/after rather than
    only an aggregate. ``index_used`` is the single judgment field — see
    ``aurora_common/access_patterns.py``.
    """

    pattern_id: str = Field(..., description="Stable id — the source query_id")
    description: str = Field(..., description="Human-readable summary of the pattern")
    operation: str = Field(..., description="SELECT / INSERT / UPDATE / DELETE / MERGE / OTHER")
    sql_text: str | None = Field(None, description="Source SQL; runs as-is on a carry_over target")
    source_tables: list[str] = Field(
        default_factory=list, description="Tables the query touches, from the collector"
    )
    source_query_ids: list[str] = Field(
        default_factory=list, description="Collector query_ids this pattern came from"
    )
    filter_columns: list[str] = Field(default_factory=list, description="Predicate columns")
    sort_columns: list[str] = Field(default_factory=list, description="ORDER BY columns")

    index_used: str = Field(
        ...,
        description=(
            "Index expected to serve this pattern, by leftmost-prefix; "
            "'__seq_scan__' when no index leads with a filtered column"
        ),
    )
    design_rps: float = Field(
        ..., ge=0, description="Measured calls_per_second — never invented; 0.0 when unmeasured"
    )
    source_latency_ms_p95: float | None = Field(
        None, description="Source p95 latency, for the load test's before/after comparison"
    )
    source_db_load_pct: float | None = Field(
        None, description="Share of source DB load, for prioritising which patterns matter"
    )

    script_derived: bool = Field(
        ..., description="True if the serving index was resolved deterministically"
    )
    needs_judgment: bool = Field(
        default=False, description="True if index selection was ambiguous and needs the LLM"
    )
    judgment_reason: str | None = Field(
        None, description="Why judgment is needed, or why a sequential scan is expected"
    )
    in_scope: bool = Field(
        default=True, description="False when the pattern cannot drive a load test"
    )
    out_of_scope_reason: str | None = Field(
        None, description="Why the pattern is out of scope, when in_scope=false"
    )

    model_config = ConfigDict(extra="ignore")


class AuroraMySQLModelOutputContract(BaseModel):
    """Output contract for the Aurora MySQL schema-design agent."""

    contract_version: str = Field(default="1.0", pattern=r"^\d+\.\d+$")
    job_id: str = Field(..., description="job_id from collector output")
    source_database: str = Field(..., description="database_name from collector metadata")
    target_engine: str = Field(default="aurora_mysql", description="Target engine identifier")

    migration_strategy: Literal["carry_over", "translate"] = Field(
        ..., description="carry_over for same-family sources, translate otherwise"
    )
    table_definitions: list[TargetTable] = Field(
        ..., min_length=1, description="Aurora MySQL table designs"
    )
    generated_ddl: str = Field(..., description="Full CREATE script from the DDL generator")
    app_layer_notes: list[AppLayerNote] = Field(
        default_factory=list, description="Source features requiring application-layer handling"
    )
    optimizations: list[Optimization] = Field(
        default_factory=list, description="LLM-recommended Aurora-specific optimizations"
    )
    access_patterns: list[AccessPattern] = Field(
        default_factory=list,
        description=(
            "Queries the target must serve, derived from the observed query log. "
            "Overwritten in code after generation so design_rps stays measured — "
            "do not re-author these."
        ),
    )

    trade_offs: list[TradeOff] = Field(..., min_length=1, description="Design trade-off decisions")
    validation_passed: bool = Field(..., description="Whether all validation checks passed")
    validation_failures: list[str] = Field(
        default_factory=list, description="Validation failures when validation_passed=false"
    )

    model_config = ConfigDict(extra="ignore")
