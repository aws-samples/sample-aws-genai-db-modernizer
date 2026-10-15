"""Pydantic response models for curated graph query endpoints."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.contracts.move_ledger import MoveActor, MovePass, ReasonCode


class AffectedQuery(BaseModel):
    query_id: str
    calls_per_second: float
    destinations: list[str]
    access_patterns: list[str]
    anti_patterns: list[str]


class TableImpactResponse(BaseModel):
    table_id: str
    affected_queries: list[AffectedQuery]


class ProvenanceDecision(BaseModel):
    decision_id: str
    category: str
    description: str
    agent_id: str | None = None
    phase: str | None = None


class QueryProvenanceResponse(BaseModel):
    query_id: str
    destination: str | None = None
    confidence: float | None = None
    assignment_reason: str | None = None
    signals: list[str]
    decisions: list[ProvenanceDecision]


class EngineDestination(BaseModel):
    destination_id: str
    source_tables: list[str]
    access_patterns: list[str]
    query_count: int


class EngineDetailResponse(BaseModel):
    engine: str
    destinations: list[EngineDestination]


class RiskHotspot(BaseModel):
    table_id: str
    total_calls_per_second: float
    risk_count: int
    anti_pattern_count: int


class RiskHotspotsResponse(BaseModel):
    hotspots: list[RiskHotspot]


class LatencyPercentilesModel(BaseModel):
    p50: float
    p90: float
    p95: float
    p99: float
    p999: float
    min: float
    max: float


class LoadTestQuery(BaseModel):
    query_id: str
    source_latency: LatencyPercentilesModel
    target_latency: LatencyPercentilesModel
    improvement_factor: float | None = None
    throughput_rps: float | None = None
    error_rate_pct: float | None = None


class LoadTestPattern(BaseModel):
    pattern_id: str
    engine: str
    schema_version: int
    description: str
    pattern_group: str
    design_rps: float
    queries: list[LoadTestQuery]


class LoadTestResultsResponse(BaseModel):
    job_id: str
    results: list[LoadTestPattern]


# ---------------------------------------------------------------------------
# Query decision trace (#339): why a query ended on engine X and not Y, from the
# move ledger (#616). Kept stable for the UI view and the engineering-report
# appendix that read it. Every ``explanation`` is plain text from the one
# renderer (src/shared/move_reasons.py); a consumer escapes it like any string.
# ---------------------------------------------------------------------------


class TraceCandidate(BaseModel):
    """One engine on a step's candidate ladder: its score, or the gate that excluded it."""

    engine: str = Field(..., description="Engine id")
    engine_name: str = Field(..., description="Engine display name")
    score: float | None = Field(None, description="The score the pass compared (0-100)")
    gate: str | None = Field(
        None,
        description=(
            "The gate that excluded the engine: a scorer gate kind (hard_exclusion, "
            "capability, model_flag) or write_gate"
        ),
    )
    gate_text: str | None = Field(None, description="The gate in plain words")


class TraceEngineScore(BaseModel):
    """An engine and its score (a step's runner-up)."""

    engine: str
    engine_name: str
    score: float | None = None


class TraceStep(BaseModel):
    """One Move of the query, in the assignment version that recorded it."""

    model_config = ConfigDict(populate_by_name=True, serialize_by_alias=True)

    version: int = Field(..., description="The assignment version whose moves hold this step")
    seq: int = Field(..., description="The Move's order within that version")
    pass_: MovePass = Field(..., alias="pass", description="The routing pass that moved it")
    actor: MoveActor
    reason_code: ReasonCode = Field(..., description="The rule (machine id; see explanation)")
    rule: str = Field(..., description="The rule as a short plain-English label")
    from_engine: str | None = None
    from_engine_name: str | None = None
    to_engine: str
    to_engine_name: str
    score_before: float | None = None
    score_after: float | None = None
    runner_up: TraceEngineScore | None = None
    tied_engines: list[str] = Field(default_factory=list)
    tied_engine_names: list[str] = Field(default_factory=list)
    group_key: str | None = None
    candidates: list[TraceCandidate] = Field(
        default_factory=list, description="Every analyzed engine's score or gate at this step"
    )
    explanation: str = Field(..., description="Why the query landed here, in one sentence")


TraceBasis = Literal["query", "group_mean"]


class TraceRunnerUp(BaseModel):
    """The engine of the final plan that came closest to the query.

    Always an engine the final assignment uses, never one the plan dropped,
    and compared on the scale the deciding pass used (the query's own scores,
    or its co-dependency group's mean scores). Engines that scored higher are
    in ``QueryTraceResponse.outscored_by``, so ``gap`` is never negative.
    """

    engine: str
    engine_name: str
    score: float | None = None
    final_score: float | None = Field(
        None, description="The final engine's score on the same basis"
    )
    basis: TraceBasis = Field(
        "query", description="query: the query's own scores; group_mean: its group's means"
    )
    gap: float | None = Field(
        None,
        description=(
            "final_score minus score, 0 or more. None when either has no score or the "
            "runner-up is gated"
        ),
    )
    gate: str | None = Field(
        None,
        description=(
            "Set only when no other engine of the plan could take the query: its gate, or "
            "group_owner (it cannot own every query of the co-dependency group)"
        ),
    )
    explanation: str


class TraceOutscored(BaseModel):
    """An engine that scored higher than the final one, and the step that decided against it."""

    engine: str
    engine_name: str
    score: float
    final_score: float
    basis: TraceBasis = "query"
    in_plan: bool = Field(..., description="Whether the final assignment still uses the engine")
    decided_by_version: int = Field(..., description="The version of the deciding step")
    decided_by_seq: int = Field(..., description="The seq of the deciding step")
    decided_by_rule: ReasonCode = Field(..., description="The deciding step's rule")
    left_it: bool = Field(
        ..., description="True when the deciding step took the query off this engine"
    )
    explanation: str


class QueryTraceResponse(BaseModel):
    """A query's routing decisions, from its first assignment to its final engine."""

    query_id: str
    has_ledger: bool = Field(
        ..., description="False for a job written before the move ledger: no steps to show"
    )
    first_assignment: TraceStep | None = Field(
        None, description="The query's first Move, with the full candidate ladder"
    )
    moves: list[TraceStep] = Field(
        default_factory=list,
        description="Every later Move across the assignment versions, in order",
    )
    final_engine: str | None = None
    final_engine_name: str | None = None
    final_version: int | None = Field(
        None, description="The assignment version of the query's last Move"
    )
    effective_version: int | None = Field(
        None,
        description=(
            "The effective assignment version the graph was built from (it may hold no "
            "Move for this query). None when unknown"
        ),
    )
    final_score: float | None = None
    tied_engines: list[str] = Field(
        default_factory=list, description="The query's unresolved tie at the end, if any"
    )
    tied_engine_names: list[str] = Field(default_factory=list)
    runner_up: TraceRunnerUp | None = None
    outscored_by: list[TraceOutscored] = Field(
        default_factory=list,
        description=(
            "Engines that scored higher than the final one (removed, consolidated or "
            "overridden), best first, each with the step that decided against it"
        ),
    )
    summary: str = Field("", description="One line for the whole trace")
