"""Query Decision Trace Contract (#339)

Why a query ended on engine X and not Y, from the move ledger (#616): the
query's first assignment with every engine's score or gate, every later Move
across the assignment versions with its reason, the final engine, the
runner-up of the final plan with its gap or gate, and the engines that scored
higher with the step that decided against each.

One shape for every reader: the context graph's ``query_trace`` and the
``GET .../graph/queries/{query_id}/trace`` endpoint (the UI's "Why this
engine" section) return :class:`QueryTraceResponse`, and synthesis writes the
same records into ``report.json`` as :class:`QueryTraceDigest` (the queries
whose routing has a story to tell) for the engineering report's appendix.
Both are built by ``src.shared.query_trace.build_query_trace``.

Every ``explanation``/``summary``/``gate_text`` is plain text from the one
reason renderer (``src/shared/move_reasons.py``): display names, numbers and
fixed phrases only. A consumer still escapes it like any string.

Not versioned on its own: these models are nested in
``SynthesisOutputContract`` (``query_traces``), whose ``contract_version``
covers them.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from src.contracts.move_ledger import MoveActor, MovePass, ReasonCode


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
    seq: int = Field(
        ..., description="The Move's order within that version (version-wide, not per query)"
    )
    step: int = Field(1, ge=1, description="The query's own step number: 1 is its first assignment")
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
    ``score`` and ``final_score`` are always on that one scale, rounded to one
    decimal: an engine gated ``group_owner`` has no group mean, so its
    ``score`` is None rather than its own per-query score (#818 review).
    """

    engine: str
    engine_name: str
    score: float | None = Field(
        None, description="The runner-up's score on the basis scale; None when it has none"
    )
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
    decided_by_step: int = Field(1, ge=1, description="The query's own number of that step")
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
    final_group_mean: float | None = Field(
        None,
        description=(
            "When a co-dependency group decided the final engine: the group's mean score "
            "on it (one decimal). ``final_score`` stays the query's own score"
        ),
    )
    summary: str = Field("", description="One line for the whole trace")


class QueryTraceEntry(QueryTraceResponse):
    """A trace as report.json lists it: plus what identifies the query to a reader."""

    source_tables: list[str] = Field(default_factory=list, description="The query's tables")
    query_excerpt: str | None = Field(
        None,
        description=(
            "The start of the query's own SQL, whitespace collapsed, about 120 characters "
            "(the customer's text: renderers escape it)"
        ),
    )


class QueryTraceListing(BaseModel):
    """Why one query's trace is listed (the facts layer's ``trace_kinds``)."""

    query_id: str
    moved: bool = Field(..., description="Its engine changed after its first assignment")
    tied: bool = Field(..., description="It was tied at some step (settled or still open)")
    open_tie: bool = Field(False, description="Its tie is still unresolved at the end")
    overridden: bool = Field(
        ..., description="A customer or a design review (model flag) placed it"
    )
    pinned: bool = Field(
        False, description="A listed rule pinned it (full-text search), not its score"
    )


class TraceRuleCount(BaseModel):
    """How many of the queries not listed in full one rule placed."""

    rule: ReasonCode = Field(..., description="The first assignment's rule (machine id)")
    label: str = Field(..., description="The rule as a short plain-English label")
    query_count: int = Field(..., ge=0)


class CloseCall(BaseModel):
    """A query its final engine won by less than the close-call margin (#730).

    ``alternative`` is the trace's runner-up: an engine the final assignment
    keeps that can take the query, ``gap`` points behind on the ``basis``
    scale (``report_facts.is_close_call`` selects the rows, below
    ``report_facts.CLOSE_CALL_MARGIN``). The customer can move the query
    there in the assignment review.
    """

    query_id: str
    engine: str = Field(..., description="The final engine (id)")
    alternative: str = Field(..., description="The runner-up engine (id) that can take it")
    score: float | None = Field(None, description="The final engine's score on the basis")
    alternative_score: float | None = Field(None, description="The runner-up's score")
    gap: float = Field(..., ge=0, description="score minus alternative_score, one decimal")
    basis: TraceBasis = Field(
        "query", description="query: the query's own scores; group_mean: its group's means"
    )
    settled_tie: bool = Field(
        False, description="An exact tie a routing rule settled (gap 0, tied at some step)"
    )


class UnservableGate(BaseModel):
    """Why one engine could not take a query that landed on a fallback engine (#729)."""

    engine: str = Field(..., description="The engine ruled out (id)")
    engine_name: str = Field(..., description="Its display name")
    gate: str = Field(..., description="The gate kind (machine id; see gate_text)")
    gate_text: str = Field(..., description="The gate in plain words")


class UnservableQuery(BaseModel):
    """A query that landed on a fallback engine because no analyzed engine fit (#729).

    Its engine was decided by a fallback rule (no analyzed engine could take
    it) or every candidate engine is gated (``report_facts.is_unservable``).
    """

    query_id: str
    engine: str = Field(..., description="The engine it ends on (id)")
    engine_name: str = Field(..., description="Its display name")
    kind: Literal["fallback", "gated_everywhere"] = Field(
        ..., description="A fallback rule placed it, or every candidate is gated"
    )
    rule: ReasonCode = Field(..., description="The deciding step's rule (machine id)")
    rule_label: str = Field(..., description="The rule as a short plain-English label")
    explanation: str = Field(..., description="The deciding step's sentence")
    blocking_gates: list[UnservableGate] = Field(
        default_factory=list, description="Each ruled-out engine and its gate, by engine"
    )
    required_capabilities: list[str] = Field(
        default_factory=list, description="The hard capabilities triage found it needs (ids)"
    )


class QueryTraceDigest(BaseModel):
    """The decision traces synthesis writes into report.json (``query_traces``, #339).

    Only the queries whose routing has a story are listed: the ones whose
    engine changed after their first assignment, the ones tied at some step,
    the ones a customer or a design review placed, and the ones a listed
    rule pinned. At most ``report_facts.TRACE_FULL_CAP`` of them carry their
    full trace (``queries``); the rest of the listed ones say only why they
    are listed (``more_listed``). The queries not listed stayed where their
    first assignment put them and are counted by that assignment's rule
    (``unlisted_rules``), so the record stays small on a large workload. The
    engineering report renders it; ``facts.query_trace`` holds its figures
    (ADR-032).
    """

    has_ledger: bool = Field(
        ..., description="False for an assignment written before the move ledger"
    )
    query_count: int = Field(..., ge=0, description="Queries of the effective assignment")
    effective_version: int | None = Field(
        None, description="The effective assignment version the traces end at"
    )
    queries: list[QueryTraceEntry] = Field(
        default_factory=list,
        description=(
            "The full traces of the listed queries, at most the facts layer's cap "
            "(``report_facts.TRACE_FULL_CAP``), most notable first"
        ),
    )
    more_listed: list[QueryTraceListing] = Field(
        default_factory=list,
        description="Listed queries beyond the cap: why each is listed, without its trace",
    )
    unlisted_rules: list[TraceRuleCount] = Field(
        default_factory=list,
        description="The queries not listed, by the rule of their first assignment",
    )
    close_calls: list[CloseCall] = Field(
        default_factory=list,
        description=(
            "Every query, listed or not, whose runner-up is a kept engine less than the "
            "close-call margin behind (#730), by engine, alternative, gap and query id"
        ),
    )
    unservable: list[UnservableQuery] = Field(
        default_factory=list,
        description=(
            "Every query, listed or not, that landed on a fallback engine because no "
            "analyzed engine fit (#729), by engine and query id"
        ),
    )
