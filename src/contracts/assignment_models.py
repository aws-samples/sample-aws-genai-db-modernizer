"""
Assignment Contract Models

Data models for query-level engine assignment: assignment status tracking,
query assignments, table assignments, versioned assignment artifacts,
and validation results.

Version History:
- 1.0 (2026-04-01): Initial version — Phase 1A assignment models
- 1.1 (2026-08-27): Added optional ``AssignmentSource`` and the ``source`` field
  on ``Assignment`` for version provenance; first real use of
  ``AssignmentStatus.CUSTOMER_APPROVED`` (customer approved routing as-is at the
  review gate). Both are backward compatible — ``source`` defaults to ``None``
  on artifacts written before it existed (ADR-028).
- 1.2 (2026-08-27): Added the optional ``accepted_feasibility_findings`` field on
  ``Assignment`` recording blocking feasibility findings the customer explicitly
  accepted at the review gate. Defaults to an empty list, so it is backward
  compatible with artifacts written before it existed (ADR-029 Layer C).
- 1.3 (2026-09-18): Added the optional ``co_dependency_propagated`` flag on
  ``QueryAssignment``, set when a query is moved automatically to stay co-located
  with a co-dependent query the customer re-routed (ADR-029 Amendment 3).
  Defaults to False, backward compatible with artifacts written before it existed.
- 1.4 (2026-10-04): Added the cache overlay (#296). ElastiCache is a cache layer, not
  a system of record, so it never owns a query: ``assigned_engine`` is always the
  query's system-of-record engine. A hot read the cache can front carries the
  optional ``cache_engine`` / ``cache_pattern`` / ``cache_reason`` fields on
  ``QueryAssignment``, and ``Assignment.cache_overlay`` summarises them (queries and
  share of calls). ``cache_customer_override`` marks a cache the customer asked for
  (kept even when the query fails the hot-read rule), ``cache_dropped`` a cache the
  post-schema safety net removed, and ``Assignment.cache_notes`` records cache
  decisions (safety-net drops, legacy ElastiCache owners moved to their
  system-of-record engine on load). All default to ``None``/False/empty, so
  artifacts written before 1.4 still load.
- 1.5 (2026-10-05): Added ``Assignment.unresolved_table_names`` (#316). A
  query's ``source_tables`` can hold names the SQL parser mistook for tables
  (CTE aliases, system catalogs, sequences, keywords, qualified columns);
  ``derive_table_assignments`` now drops those before building
  ``table_assignments`` rather than treating them as tables, and records the
  dropped names here. Defaults to an empty ``UnresolvedNames``, so artifacts
  written before 1.5 still load.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from src.contracts.feasibility_models import FeasibilityFinding


class UnresolvedNames(BaseModel):
    """A count and the sorted names behind it, for names that did not resolve
    to a known table or view.

    Produced by ``derive_table_assignments`` (#316): a name from a query's
    ``source_tables`` that is not a table or view the collector saw (a CTE
    alias, a system catalog, a sequence, a keyword, a column the SQL parser
    mistook for a table) is dropped before building ``table_assignments``
    and counted here instead. Synthesis copies this value onto its own
    report rather than recomputing it (#225, #316): the migration-waves
    builder's own ``known_tables`` scoping is a separate, defensive check on
    the same table_assignments, not the source of this count.
    """

    count: int = Field(..., ge=0, description="Number of unresolved names")
    names: list[str] = Field(default_factory=list, description="The unresolved names, sorted")


class AssignmentStatus(str, Enum):
    """Status of an assignment artifact indicating its approval state."""

    AUTO_GENERATED = "auto_generated"
    CUSTOMER_APPROVED = "customer_approved"
    CUSTOMER_MODIFIED = "customer_modified"


class AssignmentSource(str, Enum):
    """Pipeline stage that produced an assignment version (provenance).

    ``status`` tracks approval state; ``source`` records *which stage* wrote the
    version, so a consumer can reason about the lineage instead of trusting the
    version number alone (ADR-028). ``None`` on an artifact means the source was
    not recorded (a legacy version written before this field existed).
    """

    ASSIGNMENT_RESOLUTION = "assignment_resolution"
    REALITY_CHECK = "reality_check"
    CUSTOMER_GATE = "customer_gate"


class QueryAssignment(BaseModel):
    """Single query-to-engine assignment with confidence and scope metadata."""

    query_id: str = Field(..., description="Unique query identifier from collector output")
    assigned_engine: str = Field(..., description="Target engine this query is assigned to")
    confidence: int = Field(
        ...,
        ge=0,
        le=100,
        description="Assignment confidence score (0-100 integer scale)",
    )
    source_tables: list[str] = Field(..., description="Tables this query accesses")
    assignment_reason: str = Field(..., description="Why this engine was chosen for the query")
    in_scope: bool = Field(
        default=True,
        description="False when customer excluded this query from current iteration",
    )
    customer_override: bool = Field(
        default=False,
        description="True if customer changed the assignment",
    )
    co_dependency_propagated: bool = Field(
        default=False,
        description=(
            "True if this query was moved automatically to stay co-located with a "
            "co-dependent query the customer explicitly re-routed (shared JOIN group). "
            "Distinct from customer_override, which marks the customer's own picks."
        ),
    )
    signal_override: str | None = Field(
        default=None,
        description=(
            "Triage signal that forced this query to its engine (e.g. text_search). "
            "Reality Check treats these queries as mandatory for the engine."
        ),
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Warnings associated with this query assignment",
    )
    cache_engine: str | None = Field(
        default=None,
        description=(
            "Cache layer that fronts this query (cache-aside), e.g. 'elasticache'. "
            "The query stays owned by assigned_engine; the cache never owns it (#296)."
        ),
    )
    cache_pattern: str | None = Field(
        default=None,
        description=(
            "Cacheable read shape that qualified the query for the cache overlay: "
            "point_lookup, top_n, session_lookup or reference_read."
        ),
    )
    cache_reason: str | None = Field(
        default=None,
        description="Short reason the query is cached (call rate and result size).",
    )
    cache_customer_override: bool = Field(
        default=False,
        description=(
            "True when the customer decided the cache for this query: cached when "
            "cache_engine is set (kept even if the query fails the hot-read rule, with a "
            "warning), not cached when cache_engine is empty. Re-evaluation keeps the "
            "customer's choice either way."
        ),
    )
    cache_dropped: bool = Field(
        default=False,
        description=(
            "True when the post-schema safety net removed the cache overlay because the "
            "cache's schema design has no in-scope access pattern for the query."
        ),
    )


class CacheOverlaySummary(BaseModel):
    """The cache layer's share of the workload (#296).

    The cache owns no query, so the owner distribution never counts it. This is
    the separate view: how many in-scope queries it fronts and their share of calls.
    """

    engine: str = Field(..., description="Cache engine, e.g. 'elasticache'")
    query_count: int = Field(..., ge=0, description="In-scope queries the cache fronts")
    calls_per_second: float = Field(
        ..., ge=0, description="Combined call rate of those queries (calls/s)"
    )
    call_share_percent: float = Field(
        ...,
        ge=0,
        le=100,
        description="Their share of the in-scope workload's calls (percent)",
    )
    owners: dict[str, int] = Field(
        default_factory=dict,
        description="Owner engine -> number of cached queries it owns",
    )
    patterns: dict[str, int] = Field(
        default_factory=dict,
        description="cache_pattern -> number of cached queries",
    )
    min_calls_per_second: float = Field(
        ..., ge=0, description="Hot-read call-rate floor a query had to meet"
    )
    max_rows_avg: float = Field(
        ..., ge=0, description="Largest average result size a cached query may return"
    )


class TableAssignment(BaseModel):
    """Derived table-to-engine assignment.

    A table can span multiple engines when different queries against the
    same table have different access patterns. The primary_engine is the
    engine with the most assigned queries for this table.
    """

    table_id: str = Field(..., description="Table identifier from collector output")
    primary_engine: str = Field(
        ..., description="Engine with the most assigned queries for this table"
    )
    engines: list[str] = Field(
        ..., description="All engines that have queries referencing this table"
    )
    query_count: int = Field(
        ..., ge=0, description="Total number of queries referencing this table"
    )
    multi_engine_reason: str | None = Field(
        None,
        description="Reason this table spans multiple engines (set when engines has 2+ entries)",
    )


class Assignment(BaseModel):
    """Complete versioned assignment artifact."""

    job_id: str = Field(..., description="Unique job identifier")
    version: int = Field(..., ge=1, description="Monotonically increasing version number")
    status: AssignmentStatus = Field(..., description="Origin status of this assignment")
    timestamp: datetime = Field(
        ..., description="ISO 8601 timestamp when this assignment was created"
    )
    query_assignments: list[QueryAssignment] = Field(
        ..., description="Per-query engine assignments"
    )
    table_assignments: list[TableAssignment] = Field(
        ..., description="Derived per-table engine assignments"
    )
    co_dependency_groups: list[list[str]] = Field(
        ...,
        description="Groups of query IDs sharing significant JOIN relationships",
    )
    validation_warnings: list[str] = Field(..., description="Warnings from assignment validation")
    previous_version: int | None = Field(
        None,
        description="Version number of the previous assignment (None for first version)",
    )
    source: AssignmentSource | None = Field(
        None,
        description=(
            "Pipeline stage that produced this version (provenance). None for "
            "legacy artifacts written before this field existed (ADR-028)."
        ),
    )
    accepted_feasibility_findings: list[FeasibilityFinding] = Field(
        default_factory=list,
        description=(
            "Blocking feasibility findings the customer explicitly accepted at the "
            "review gate (ADR-029 Layer C). Empty by default; recorded for audit."
        ),
    )
    cache_notes: list[str] = Field(
        default_factory=list,
        description=(
            "Cache layer decisions recorded for audit (#296): safety-net drops after "
            "schema design, legacy ElastiCache owners moved to their system-of-record engine."
        ),
    )
    cache_overlay: CacheOverlaySummary | None = Field(
        None,
        description=(
            "Cache layer summary (#296): queries the cache fronts and their share of "
            "calls. None when no query qualifies or no cache engine was analyzed."
        ),
    )
    unresolved_table_names: UnresolvedNames = Field(
        default_factory=lambda: UnresolvedNames(count=0, names=[]),
        description=(
            "Names from query_assignments[].source_tables that are not a table or view "
            "in the collected schema (#316): CTE aliases, system catalogs, sequences, "
            "keywords or columns the SQL parser mistook for tables. Dropped before "
            "deriving table_assignments rather than counted as tables. Empty when the "
            "collector schema was unavailable (every name is kept, legacy behavior) or "
            "every name resolved."
        ),
    )


class ValidationResult(BaseModel):
    """Result of validating an assignment."""

    valid: bool = Field(..., description="Whether the assignment passed validation")
    warnings: list[str] = Field(
        default_factory=list,
        description="Validation warnings (assignment may still be accepted)",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Validation errors (assignment is rejected)",
    )
