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
- 1.6 (2026-10-07): Added ``Assignment.aurora_engine_choice`` (#381). A source
  with no Aurora dialect of its own (SQL Server, Oracle, DB2) has both Aurora
  engines compete on workload fit instead of being skipped; the resolver
  collapses the two to a single winner and records the decision here --
  totals, margin and any deciding features -- so every deliverable can state
  the move is cross-engine. ``None`` for a homogeneous source (MySQL/MariaDB/
  PostgreSQL, unaffected) and for artifacts written before 1.6.
- 1.7 (2026-10-08): Added ``Assignment.cache_safety_net_notes`` (#424, #459). A
  customer-facing subset of ``cache_notes``: only the post-schema safety net's
  own notes (never a customer-edit note with full query hashes, or the
  legacy-migration note). Every deliverable except the engineering report
  reads this field instead of ``cache_notes``. Defaults to an empty list, so
  artifacts written before 1.7 still load.
- 1.8 (2026-10-12): Added ``QueryAssignment.tied_engines`` (#503 review): the
  engines an exact score tie left unresolved -- no ADR-031 rule decided
  between them -- so the customer can see and choose at the approval gate,
  rather than only inferring it from free-text ``assignment_reason``. Empty
  (the common case: no tie, or a tie a real rule like source-compatible or
  stability decided) for every query, and for every artifact written before
  this version.
- 1.9 (2026-10-14, #629): Added ``AssignmentSource.POST_SCHEMA_ROUTING``: the
  version the post-schema router writes when PE review or a designer's
  unsupported pattern moves a query to another kept engine (maintainer
  decision, 2026-10-14), so schema design and the assignment agree. Also
  ``AssignmentStatus.REROUTED_AFTER_APPROVAL`` and ``Assignment.post_schema_reroutes``
  (which queries moved after approval, from where to where, and why), so a
  rerouted version never claims the customer approved it. And
  ``AssignmentStatus.REROUTE_ROLLED_BACK`` with ``Assignment.rolled_back_version``:
  the version that restates the routing when a reroute's redesign fails.
- 1.10 (2026-10-14, #616): Added ``Assignment.moves``, the move ledger
  (``src.contracts.move_ledger.Move``): one record per engine change of a
  query, in order, with the scores, runner-up, rule and evidence. Per-version
  (maintainer decision, 2026-10-14): a version holds only the Moves that
  produced it, never a copy of an earlier version's. Replaying v1's moves
  rebuilds v1's ``assigned_engine`` and ``tied_engines``; v1 plus each later
  version's own moves rebuilds that version. Only the first assignment
  writes Moves today; the reality check, customer gate and post-schema
  router (and its rollback) start their version with an empty list until
  they record their own (later #616 changes). Defaults to an empty list, so
  artifacts written before 1.10 still load.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field

from src.contracts.feasibility_models import FeasibilityFinding
from src.contracts.move_ledger import Move


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
    # Written by the post-schema router (#629): the deterministic scorer moved
    # queries after the customer approved the version it edits. Never stamped
    # as customer-approved, because the customer did not see these moves; the
    # deliverables list them (``post_schema_reroutes``) with the override path.
    REROUTED_AFTER_APPROVAL = "rerouted_after_approval"
    # Restates the routing a failed post-schema reroute started from (#629):
    # the redesign of ``rolled_back_version`` failed, so that version is
    # abandoned and this one carries the earlier routing and its designs.
    REROUTE_ROLLED_BACK = "reroute_rolled_back"


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
    POST_SCHEMA_ROUTING = "post_schema_routing"


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
    tied_engines: list[str] = Field(
        default_factory=list,
        description=(
            "The engines an exact score tie left unresolved for this query (#503): no "
            "ADR-031 rule -- not source-compatible, not stability -- decided between them, "
            "so assigned_engine is a deterministic placeholder, not a preference. Empty "
            "when there was no tie, or a real rule decided it. Sorted, length >= 2 when set."
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


class AuroraEngineChoice(BaseModel):
    """How the resolver picked one Aurora engine for a source with no Aurora
    dialect of its own (SQL Server, Oracle, DB2 -- #381).

    Triage selects both ``aurora_mysql`` and ``aurora_postgresql`` for such a
    source, and every query is scored against both the same way it is scored
    against any other candidate engine. ``AssignmentResolver`` compares the
    two engines on ``totals`` (the sum of each engine's adjusted score across
    every query) before assigning anything, and the winner serves every
    relational query -- the assignment never carries both Aurora engines.

    ``margin`` is ``totals["aurora_postgresql"] - totals["aurora_mysql"]``;
    ``deciding_features`` is non-empty only when the totals were close enough
    for source features (recursive CTEs, sequences, ``MERGE``, JSON, multiple
    schemas, the Babelfish compatibility path, ...) to tip the choice, per
    ``aurora_choice.choose_heterogeneous_engine``. Always present for a
    heterogeneous source, never for a homogeneous one (MySQL/MariaDB ->
    Aurora MySQL, PostgreSQL -> Aurora PostgreSQL keep their own dialect
    without a competition to record).
    """

    source_engine: str = Field(..., description="The collector's source database engine")
    engine: str = Field(
        ..., description="The Aurora engine chosen (aurora_mysql/aurora_postgresql)"
    )
    totals: dict[str, float] = Field(
        default_factory=dict,
        description="Each competing Aurora engine's total adjusted score across every query",
    )
    margin: float | None = Field(
        None, description="aurora_postgresql's total minus aurora_mysql's total"
    )
    deciding_features: list[str] = Field(
        default_factory=list,
        description="Source features that tipped a close call (empty when the totals decided it)",
    )
    reason: str = Field(..., description="One-line explanation of the decision")


class PostSchemaReroute(BaseModel):
    """One query the post-schema router moved after approval (#629).

    PE review (or a designer's unsupported pattern) flagged the query on
    ``from_engine``; the shared scorer picked ``to_engine`` among the engines
    the approved assignment keeps.
    """

    query_id: str = Field(..., description="The rerouted query")
    from_engine: str = Field(..., description="The engine it was approved on")
    to_engine: str = Field(..., description="The scorer's best remaining kept engine")
    reason: str = Field(..., description="Why, in customer-facing words (deterministic)")
    reason_category: str | None = Field(None, description="The flag's reason category")
    score_before: float | None = Field(None, description="from_engine's score, ignoring the flag")
    score_after: float | None = Field(None, description="to_engine's score")
    approved_version: int = Field(..., ge=1, description="The version the customer approved")


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
    cache_safety_net_notes: list[str] = Field(
        default_factory=list,
        description=(
            "The safety net's own notes only (#459), a customer-facing subset of "
            "cache_notes: never a customer-edit note (full query hashes) or the "
            "legacy-migration note. Every deliverable except the engineering report "
            "reads this field, not cache_notes."
        ),
    )
    rolled_back_version: int | None = Field(
        None,
        ge=1,
        description=(
            "For a ``reroute_rolled_back`` version: the abandoned post-schema reroute "
            "version whose redesign failed (#629). None otherwise."
        ),
    )
    post_schema_reroutes: list[PostSchemaReroute] = Field(
        default_factory=list,
        description=(
            "Queries the post-schema router moved after the customer approved the "
            "assignment (#629), cumulative across reroute versions. Empty for every "
            "version the router did not write."
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
    aurora_engine_choice: AuroraEngineChoice | None = Field(
        None,
        description=(
            "How the resolver picked one Aurora engine when the source has no Aurora "
            "dialect of its own (#381). None for a homogeneous source and for "
            "artifacts written before 1.6."
        ),
    )
    moves: list[Move] = Field(
        default_factory=list,
        description=(
            "The move ledger (#616), per-version: only the engine changes of a query "
            "that produced this version, in seq order, never a copy of an earlier "
            "version's. Replaying v1's moves rebuilds v1's assigned_engine and "
            "tied_engines; v1 plus each later version's own moves rebuilds that version. "
            "Only the first assignment records Moves today: the reality check, customer "
            "gate and post-schema router (and its rollback) write an empty list until "
            "they record their own (later #616 changes). Empty for artifacts written "
            "before 1.10."
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
