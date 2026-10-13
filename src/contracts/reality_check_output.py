"""
Reality Check Output Contract

Data models for the reality check agent output: unique value assessment,
consolidation decisions, architectural patterns, and recommendations.

Version History:
- 1.0 (2026-04-16): Initial version — CTO-level engine consolidation
- 1.3 (2026-10-03): executive_summary describes the final records; the LLM text and
  the check findings are kept in executive_summary_llm / _validation_warnings (#236)
- 1.4 (2026-10-04): validation_incomplete lists consolidation validation batches that
  got no LLM verdict (#285)
- 1.5 (2026-10-06): UniqueValueAssessment.cost_justification records why a mandatory
  engine's fixed cost was judged proportionate (or not) to its workload share --
  without this field Pydantic silently dropped it, so the OpenSearch justification
  floor's reasoning (#326) and the generalized cost-share floor (#167) never reached
  the deliverables (review of #375, finding 5)
- 1.6 (2026-10-11): Consolidation.workload_justified records whether the source
  engine's FIRST-ASSIGNMENT workload ever cleared the #326 floor (None when no
  floor applies) -- an engine that never did is removed later with no saving
  claimed for it, not presented as giving up something that earned its place
  (review of #475, finding 3)
- 1.7 (2026-10-12): UniqueValueAssessment.tie_unresolved, floor_unplaced_queries
  and floor_unplaced_reason declared explicitly -- without them Pydantic
  silently dropped the same way cost_justification did in 1.5. tie_unresolved
  marks an engine Phase 1 found tied with another on burden-per-unique-query,
  with no ADR-031 rule yet to decide between them (#503), so both are kept;
  floor_unplaced_queries/_reason record a mandatory engine's queries that
  fail the justification floor (#326, #167) but have no eligible destination
  either, so the engine is kept with the reason stated rather than silently
  unchanged (review of #502, round 6)
- 1.8 (2026-10-12): eliminated_engines records which engines have no in-scope
  query left, computed once by ``src.shared.engine_elimination`` -- the
  handler, the UI and synthesis previously disagreed on this whenever an
  engine kept only out-of-scope queries (#235)
- 1.9 (2026-10-12, #503): Added ``query_tie_unresolved_count``: how many of
  ``revised_assignments`` carry a non-empty ``QueryAssignment.tied_engines``
  once the reality check has re-resolved what it can -- a per-QUERY
  exact-score tie no ADR-031 rule decided, surfaced as a count here
  (synthesis carries the same count on its own ``AssignmentSummary``) so
  the approval gate can show it alongside the existing, engine-level
  ``tie_unresolved`` already on ``UniqueValueAssessment``. Defaults to 0,
  so artifacts written before this version read as having no unresolved
  query ties. Bumped from the originally-proposed 1.8 to 1.9 after #542
  merged its own, unrelated 1.8 (``eliminated_engines``) first.
- 1.10 (2026-10-13, #589): Added ``sweep_decisions``: the sanity sweep's own
  kept-or-removed verdict for every tiny orphan engine it evaluated
  (``consolidation_validator.sanity_sweep``). Before this field, that
  explanation only ever reached a ``[reality-check]`` log line -- a KEPT
  engine (at least one query, or co-dependency group, had no serviceable
  committed destination) left no trace in the written artifact at all.
  Defaults to an empty list, so an artifact written before this version
  reads as having no sweep decisions.
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
    cost_justification: str | None = Field(
        None,
        description=(
            "Why this engine's fixed monthly cost was judged proportionate, or not, to its "
            "workload share -- set when a mandatory signal override alone did not excuse it "
            "from the operational-burden review (#326 for opensearch, #167 for any other "
            "mandatory engine)"
        ),
    )
    tie_unresolved: bool = Field(
        default=False,
        description=(
            "Phase 1 found this engine tied with another on burden-per-unique-query, and "
            "the tiebreak (fewer destination engines, then least total fit lost) was also "
            "an exact tie -- no ADR-031 rule yet decides between two such engines (#503), "
            "so both stay committed rather than one being picked arbitrarily"
        ),
    )
    floor_unplaced_queries: list[str] = Field(
        default_factory=list,
        description=(
            "This engine's queries that failed the mandatory-engine justification floor "
            "(#326, #167) but had no eligible destination engine either, so the engine "
            "keeps them"
        ),
    )
    floor_unplaced_reason: str | None = Field(
        None,
        description=(
            "Why floor_unplaced_queries stayed: no committed engine was eligible to serve "
            "them, stated explicitly rather than leaving the engine unchanged with no "
            "recorded reason"
        ),
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
    workload_justified: bool | None = Field(
        None,
        description=(
            "Whether from_engine's FIRST-ASSIGNMENT workload ever cleared the #326 "
            "justification floor (real search depth plus traffic or corpus size). "
            "None when no floor applies to this engine (only OpenSearch has one); "
            "False means it was never justified, so this consolidation's removal "
            "is not an infrastructure saving to claim (#475)"
        ),
    )


class SweepDecision(BaseModel):
    """One sanity-sweep kept/removed verdict for a tiny orphan engine (#589).

    ``consolidation_validator.sanity_sweep`` runs after every other
    consolidation pass; for each engine at or under
    ``ORPHAN_ENGINE_THRESHOLD`` queries it either removes the engine (every
    query, or co-dependency group, has a serviceable, eligible committed
    destination) or keeps it (at least one does not). Before this field the
    explanation reached only a ``[reality-check]`` log line -- a KEPT engine
    left no trace in the written artifact, since it generates no
    :class:`Consolidation` record either.
    """

    engine: str = Field(..., description="The orphan engine the sweep evaluated")
    decision: str = Field(..., description="'kept' or 'removed'")
    reason: str = Field(..., description="Why the sweep kept or removed this engine")
    blocking_query_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Query (or co-dependency group member) IDs that had no serviceable, "
            "eligible committed destination -- what blocked a 'kept' decision. "
            "Always empty for a 'removed' decision"
        ),
    )
    blocking_rule: str | None = Field(
        None,
        description=(
            "Why the blocking query/group had nowhere else to go: 'signal_override' "
            "(mandatory for a signal override), 'no_serviceable_engine' (a hard "
            "exclusion or missing capability on every other committed engine), "
            "'ineligible_destination' (serviceable, but not somewhere the reality "
            "check's own placement rule would ever put it), or 'mixed' when "
            "different blocked queries/groups on the same engine had different "
            "rules. None for a 'removed' decision"
        ),
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

    contract_version: str = Field(default="1.10", description="Contract version")
    source_assignment_version: int = Field(
        ..., ge=1, description="Base assignment version this check was run against"
    )
    output_assignment_version: int | None = Field(
        None,
        ge=2,
        description=(
            "Assignment version this check wrote its consolidation to "
            "(next_assignment_version at write time); None when nothing was consolidated"
        ),
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
        description=(
            "Executive summary of the final consolidation (2-3 sentences): the LLM summary "
            "when it matches the final records, else one built from them"
        ),
    )
    executive_summary_source: str | None = Field(
        None,
        description=(
            "Which text executive_summary holds: llm, or deterministic_fallback (the LLM "
            "summary contradicted the final records); None when there is no summary"
        ),
    )
    executive_summary_llm: str | None = Field(
        None, description="LLM-written summary as received, kept for audit even when replaced"
    )
    executive_summary_validation_warnings: list[str] = Field(
        default_factory=list,
        description="Where the LLM summary contradicted the final records",
    )
    recommendations: list[str] = Field(..., description="Human-readable recommendations")
    before_distribution: dict[str, int] = Field(
        ..., description="Query count per engine before reality check"
    )
    after_distribution: dict[str, int] = Field(
        ..., description="Query count per engine after reality check"
    )
    eliminated_engines: list[str] = Field(
        default_factory=list,
        description=(
            "Engines the single rule (#235) eliminates: an engine that had an in-scope "
            "query before this check and has none after it (src.shared.engine_elimination). "
            "Not the same as before_distribution/after_distribution, which count every "
            "query, in or out of scope -- an engine can be eliminated here while still "
            "owning out-of-scope queries"
        ),
    )
    lightweight_recommendations: list[LightweightRecommendation] = Field(
        default_factory=list,
        description="Lightweight managed-service alternatives for small orphan query sets",
    )
    validation_incomplete: list[dict] = Field(
        default_factory=list,
        description=(
            "Consolidation validation batches that got no LLM verdict (from_engine, "
            "to_engine, batch as 1-based start-end of total, error); their moves stand "
            "unreviewed"
        ),
    )
    query_tie_unresolved_count: int = Field(
        default=0,
        ge=0,
        description=(
            "How many queries in revised_assignments still carry a non-empty "
            "QueryAssignment.tied_engines after the reality check (#503): a per-query "
            "exact-score tie no ADR-031 rule decided. 0 for an artifact written before "
            "this field existed."
        ),
    )
    sweep_decisions: list[SweepDecision] = Field(
        default_factory=list,
        description=(
            "The sanity sweep's own kept-or-removed verdict for every tiny orphan "
            "engine it evaluated (#589). Empty when the sweep evaluated no orphan "
            "engine, or for an artifact written before this field existed"
        ),
    )
