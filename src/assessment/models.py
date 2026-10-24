"""The typed request and result of :func:`src.assessment.assess` (contract 1.0).

The stable surface is ``AssessmentRequest`` -> ``AssessmentResult`` with ``summary``,
``statements[]`` and ``groups[]``. It is versioned on its own (``contract_version``,
semantic versioning), separately from the pipeline's internal contracts. Internal
contracts appear only in the optional ``artifacts`` block, verbatim and with no
compatibility promise.

The stable surface is for engine placement: which engine serves each statement, which
statements move together and which engine owns each table. It is not a data model.
Partition keys, sort keys, indexes and target tables come only from schema design
(``run_through="schema_design"``, which needs a model); its designs are in the
``artifacts`` block until a stable design surface is agreed, and nothing the
deterministic analysis guesses about a data model is part of this surface.

Engine ids are the engine catalog's active ids (``src/catalog/engines``). The request
validates them; the result carries them as plain strings.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.shared.database_name import DATABASE_NAME_PATTERN, validate_database_name
from src.shared.target_databases import normalize_target_databases

#: The version of this public surface (not of any internal contract).
CONTRACT_VERSION: Literal["1.0"] = "1.0"

RunThrough = Literal["analysis", "reality_check", "schema_design", "synthesis"]

#: The ``run_through`` values this release runs (every value of the contract since #1387).
SUPPORTED_RUN_THROUGH: tuple[str, ...] = ("analysis", "reality_check", "schema_design", "synthesis")

#: The phases that always use a model: refused with ``deterministic=True``.
MODEL_PHASES: tuple[str, ...] = ("schema_design", "synthesis")

#: The public reason codes: why a statement is on its engine. Routing's own rules are
#: finer-grained and change often; each maps onto exactly one of these
#: (``src.assessment.reason_codes``). Adding a value is a MINOR change of this contract.
PublicReasonCode = Literal[
    "highest_score",
    "tie_rule",
    "co_dependency",
    "write_group",
    "override",
    "fallback",
    "consolidated",
    "engine_floor",
    "sweep",
    "capability_flag",
    "customer_choice",
    "post_schema_reroute",
    "unservable",
]

#: How a table's owner was chosen (the facts layer's owner basis, ``src.shared.report_facts``).
OwnerBasis = Literal["writes", "split_writes", "most_queries", "retained", "design"]

#: What a result is, so a routing-only result is never read as a data model:
#: ``analysis_only`` (scores, nothing placed), ``recommendation`` (engine placement, no
#: data model) and ``designed`` (a schema design ran, with a model, for at least one
#: engine; its data model is in ``artifacts``). A run through ``schema_design`` or
#: ``synthesis`` that had no engine to design (``summary.designed_engines`` empty) stays
#: ``recommendation``.
ResultLabel = Literal["analysis_only", "recommendation", "designed"]

#: The label each ``run_through_reached`` gives a result with a design (see ``result_label``).
LABEL_BY_PHASE: dict[str, str] = {
    "analysis": "analysis_only",
    "reality_check": "recommendation",
    "schema_design": "designed",
    "synthesis": "designed",
}


def result_label(run_through_reached: str, designed_engines: list[str]) -> str:
    """The label of a result: the phase's, except that a model phase with no designed
    engine is a ``recommendation`` (it carries no data model)."""
    if run_through_reached in MODEL_PHASES and not designed_engines:
        return "recommendation"
    return LABEL_BY_PHASE[run_through_reached]


#: An analysed engine's part in the result: ``owner`` (statements are placed on it),
#: ``cache_layer`` (a cache in front of other engines' reads; it owns no statement),
#: ``consolidated`` (the reality check moved its statements to another engine) or
#: ``not_selected`` (analysed, but no statement was placed on it).
EngineRole = Literal["owner", "cache_layer", "consolidated", "not_selected"]

#: A statement's type, as the library's parser reads it.
StatementType = Literal["SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "OTHER"]

COMPATIBILITY_NOTE: Literal["none: internal contracts, may change in any release"] = (
    "none: internal contracts, may change in any release"
)


class _Strict(BaseModel):
    # Unknown fields are refused. In the serialization JSON Schema (what a result is as
    # JSON), a field with a default is required: ``model_dump_json`` always writes it, so
    # the published result schema says so (#1183). The request schema is the validation
    # schema, where a default still makes a field optional.
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)


# --------------------------------------------------------------------------- request


class AssessmentOptions(_Strict):
    """How far to run and over which engines. Echoed in the result as applied."""

    # The published schema states the one cross-field rule: a phase that needs a
    # model is sent with ``deterministic: false`` (its default is true).
    model_config = ConfigDict(
        **_Strict.model_config,
        json_schema_extra={
            "if": {
                "properties": {"run_through": {"enum": list(MODEL_PHASES)}},
                "required": ["run_through"],
            },
            "then": {
                "properties": {"deterministic": {"const": False}},
                "required": ["deterministic"],
            },
        },
    )

    target_engines: list[str] | None = Field(
        None,
        description=(
            "Engine scope: the engine catalog ids to consider. None means every engine "
            "the workload warrants. Ids are lower-cased, trimmed, de-duplicated and "
            "sorted; an empty list, a legacy id, a planned engine or an unknown id is "
            "refused."
        ),
    )
    run_through: RunThrough = Field(
        "reality_check",
        description=(
            "Last phase to run. 'analysis' scores every statement and assigns nothing; "
            "'reality_check' adds the final engine placement; 'schema_design' designs "
            "the data model of every engine the placement keeps; 'synthesis' adds the "
            "report. 'schema_design' and 'synthesis' need a model (deterministic=False); "
            "their designs and report are in `artifacts`."
        ),
    )
    deterministic: bool = Field(
        True,
        description=(
            "Run with no model. Analysis never uses one; with False the reality check "
            "has a model review its consolidations and write its summary, schema design "
            "designs with a model and synthesis writes its executive summary with one. "
            "Must be False for 'schema_design' and 'synthesis'."
        ),
    )
    include_artifacts: bool = Field(
        False,
        description="Include the internal contracts in `artifacts` (no compatibility promise).",
    )

    @field_validator("target_engines", mode="before")
    @classmethod
    def _normalize_target_engines(cls, value: Any) -> list[str] | None:
        # The one engine-scope validation every entry point shares.
        return normalize_target_databases(value, setting="target_engines")

    @model_validator(mode="after")
    def _model_phase_needs_a_model(self) -> AssessmentOptions:
        if self.deterministic and self.run_through in MODEL_PHASES:
            raise ValueError(
                f"run_through={self.run_through!r} needs a model: schema design always "
                "designs the data model with one, so it cannot run with "
                "deterministic=True (the default). Set deterministic=False, or stop at "
                "'reality_check' for engine placement with no model"
            )
        return self


class WorkloadScope(_Strict):
    """Informational: what a cut workload left out. Echoed back unchanged; no rule reads it."""

    tables: list[str] = Field(default_factory=list)
    derived_from: str | None = None
    rewritten_query_ids: list[str] = Field(
        default_factory=list,
        description="Statements the caller rewrote; marked rewritten_by_caller in the result.",
    )
    dropped_foreign_keys: list[dict[str, Any]] = Field(default_factory=list)


class AssessmentRequest(_Strict):
    """One assessment: a database's raw collection and the options to run it with."""

    contract_version: Literal["1.0"] = CONTRACT_VERSION
    database_name: str = Field(
        ...,
        min_length=1,
        description=(
            "1 to 128 ASCII letters, digits, underscores or hyphens, not starting with a hyphen."
        ),
        # The published schema states the rule ``validate_database_name`` applies.
        json_schema_extra={"pattern": f"^{DATABASE_NAME_PATTERN}$"},
    )
    collection: dict[str, Any] = Field(
        ...,
        description=(
            "A raw collection: the unmodified output of the collection scripts "
            "(statement text, execution statistics and the schema). A collector output "
            "(contract 3.x) is accepted too, but every derived fact is read again from "
            "each statement's text by the library's parser; derived fields it carries "
            "are ignored."
        ),
    )
    options: AssessmentOptions = Field(default_factory=AssessmentOptions)
    scope: WorkloadScope | None = None

    @field_validator("database_name")
    @classmethod
    def _valid_database_name(cls, value: str) -> str:
        return validate_database_name(value)

    @field_validator("collection")
    @classmethod
    def _non_empty_collection(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not value:
            raise ValueError("collection is empty")
        return value


# --------------------------------------------------------------------------- result


class RelationalEngine(_Strict):
    """The Aurora engine chosen for the source, and why."""

    engine: str
    reason: str = Field(..., description="Customer-facing sentence; do not parse")


class EngineLine(_Strict):
    """One analysed engine, as the run left it."""

    engine: str
    role: EngineRole
    statement_count: int = Field(
        0, ge=0, description="Statements placed on it (`statements_per_engine`); 0 for a cache"
    )
    consolidated_into: str | None = Field(
        None,
        description=(
            "Set when role is 'consolidated': the engine that took most of its statements "
            "(`consolidations` lists every move)"
        ),
    )
    unique_queries: int | None = Field(
        None,
        ge=0,
        description=(
            "Reality check: of the statements it held when the check ran, those whose "
            "score on it leads the best other engine by at least `unique_delta_threshold` "
            "points; None when the reality check did not assess it"
        ),
    )
    avg_delta: float | None = Field(
        None,
        description=(
            "Reality check: its mean score lead over the best other engine on the "
            "statements it held; None when the reality check did not assess it"
        ),
    )
    reason: str = Field(
        ...,
        description=(
            "Customer-facing sentence; do not parse. For an owner or the cache, the "
            "executive report's 'why this engine' text; for a consolidated engine, the "
            "reality check's reason"
        ),
    )


class Consolidation(_Strict):
    """One reality-check consolidation: statements moved off an engine that added no
    unique value."""

    from_engine: str
    to_engine: str
    query_count: int = Field(..., ge=0, description="Statements it moved")
    reason: str = Field(..., description="Customer-facing sentence; do not parse")


class TableLine(_Strict):
    """One source table: the engine that owns it and the engines that read copies of it."""

    table_id: str = Field(..., description="The source table, as in the collection")
    owner: str | None = Field(
        ...,
        description=(
            "The engine that takes the table's writes; never a cache-only engine. None "
            "only when a cache is the only engine that maps the table"
        ),
    )
    owner_basis: OwnerBasis | None = Field(
        None,
        description=(
            "writes: the engine of its writes; split_writes: writes on several engines, "
            "the owner serves most of its queries; most_queries: no writes, the engine "
            "serving most of its "
            "queries; retained: no owner engine queries it, the retained relational "
            "engine; design: no queries, from the schema design"
        ),
    )
    also_read_by: list[str] = Field(
        default_factory=list,
        description="Other engines whose statements read it (each needs a replica), most first",
    )
    cached_by: str | None = Field(None, description="The cache in front of its reads, if any")
    reason: str | None = Field(
        None,
        description=(
            "Customer-facing sentence on why the owner is the owner; do not parse. None "
            "only when the table has no owner basis"
        ),
    )


class Summary(_Strict):
    run_through_reached: RunThrough
    source_engine: str = Field(
        ..., description="The source database engine, as the collection states it"
    )
    assignment_version: int | None = Field(
        None,
        ge=1,
        description=(
            "The assignment version the result reflects (it rises when the reality check, "
            "a customer edit or the post-schema routing writes a new one); None at "
            "'analysis'"
        ),
    )
    statement_count: int = Field(..., ge=0, description="Placed statements plus unservable ones")
    statements_per_engine: dict[str, int] = Field(
        default_factory=dict, description="Placed statements per engine; empty at 'analysis'"
    )
    unservable_count: int = Field(0, ge=0)
    unique_delta_threshold: int = Field(
        ...,
        ge=0,
        description=(
            "The score lead (points) a statement needs on an engine for the reality "
            "check to count it as that engine's unique value"
        ),
    )
    relational_engine: RelationalEngine | None = Field(
        None,
        description=(
            "The Aurora engine for the source. At 'analysis', None for a source with no "
            "Aurora engine of its own (the choice is made when statements are placed)"
        ),
    )
    engines: list[EngineLine] = Field(
        default_factory=list,
        description="One line per analysed engine, in analysis order; empty at 'analysis'",
    )
    consolidations: list[Consolidation] = Field(
        default_factory=list,
        description="The reality check's consolidations, in its order; empty at 'analysis'",
    )
    tables: list[TableLine] = Field(
        default_factory=list, description="One line per source table; empty at 'analysis'"
    )
    designed_engines: list[str] = Field(
        default_factory=list,
        description=(
            "The engines with a schema design (in `artifacts.schema_designs`), in triage's "
            "order; empty before 'schema_design', and when no engine could be designed"
        ),
    )
    design_note: str | None = Field(
        None,
        description=(
            "Customer-facing sentence; do not parse. Set only when 'schema_design' ran "
            "and no engine could be designed (the result is then a 'recommendation')"
        ),
    )


class PatternRef(_Strict):
    """A pattern from the pattern catalog (``src/catalog/patterns``)."""

    id: str = Field(..., description="The catalog's pattern id")
    name: str = Field(..., description="The catalog's display name for it")


class StatementResult(_Strict):
    query_id: str
    query_tags: list[str] = Field(
        default_factory=list, description="Leading or trailing comment tags; never read by routing"
    )
    query_type: StatementType | None = Field(None, description="As the parser reads it")
    join_count: int | None = Field(None, ge=0, description="As the parser reads it")
    tables: list[str] = Field(
        default_factory=list, description="The tables it touches, as the parser reads them"
    )
    calls_per_second: float | None = Field(None, ge=0)
    rewritten_by_caller: bool = False
    engine: str | None = Field(None, description="None before 'reality_check', or when unservable")
    confidence: int | None = Field(
        None,
        ge=0,
        le=100,
        description=(
            "The score it was placed with, as the assignment stores it. Until #1373 is "
            "fixed, a consolidated statement keeps the removed engine's score here; its "
            "`fit` for `engine` is the score on the engine it is on"
        ),
    )
    reason_code: PublicReasonCode | None = None
    consolidated_from: str | None = Field(
        None, description="The engine the reality check moved it from"
    )
    excluded: list[str] = Field(
        default_factory=list,
        description=(
            "Engines ruled out for it, by id, sorted: a hard or capability gate, the "
            "engine scope, and from 'reality_check' on also engines that cannot own its "
            "writes (those keep their score in `fit`)"
        ),
    )
    unservable_reason: str | None = None
    fit: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "The routing scorer's score per analysed engine it can be scored on "
            "(valid for ruleset_hash only)"
        ),
    )
    signals: list[str] = Field(default_factory=list, description="Workload signals that name it")
    matched_patterns: list[PatternRef] = Field(
        default_factory=list,
        description=(
            "The catalog patterns it matches on its assigned engine, as that engine's "
            "analysis found them; empty before 'reality_check' and when unservable"
        ),
    )
    tied_engines: list[str] = Field(default_factory=list)
    reason_text: str | None = Field(
        None,
        description=(
            "Customer-facing sentence; do not parse. It can quote collection text "
            "(table names): escape it when rendering"
        ),
    )


class Group(_Strict):
    """Statements that move together."""

    group_id: str = Field(..., description="Stable within a result")
    kind: Literal["co_dependency", "write_group"]
    query_ids: list[str]
    tables: list[str] = Field(default_factory=list)
    engine: str | None = Field(
        None, description="Where the group lands; None before 'reality_check' or when unservable"
    )
    reason: str


class Artifacts(BaseModel):
    """Internal contracts, verbatim. No compatibility promise; each keeps its own version.

    From ``schema_design`` on: ``schema_designs`` (each designed engine's schema output,
    the data model) and, at ``synthesis``, ``synthesis_report`` (the report every
    deliverable renders). ``artifact_keys`` names the store key each was read from
    (under ``<database_name>/<job_id>/``), for a caller that passed its own store."""

    model_config = ConfigDict(extra="allow", json_schema_serialization_defaults_required=True)

    compatibility: Literal["none: internal contracts, may change in any release"] = (
        COMPATIBILITY_NOTE
    )


class AssessmentResult(_Strict):
    """One assessment's result: where each statement runs, which statements move
    together and which engine owns each table (not a data model).

    Determinism: for run_through="analysis" (no model), the same collection, library
    version and engine scope give an identical result, in any process and under any
    PYTHONHASHSEED. For run_through="reality_check" this is not guaranteed: everything
    up to the reality check's model review is deterministic, but the review (with
    deterministic=False) can differ between runs and change consolidations. Each change
    it makes is recorded in the assignment's moves with actor "model_flag"
    (artifacts.assignment.moves, with include_artifacts=True), and the statement it
    moved has reason_code "capability_flag". run_through="schema_design" and
    "synthesis" always use a model: the placement is the reality check's (the same
    guarantee) plus the post-schema routing's moves of the statements a design cannot
    serve (each a "post_schema_flag" Move, reason_code "post_schema_reroute"), and the
    designs and the report's executive summary can differ between runs. Results are
    never stored or reused: a repeat call is a new run."""

    contract_version: Literal["1.0"] = CONTRACT_VERSION
    label: ResultLabel = Field(
        ...,
        description=(
            "analysis_only: scores, nothing placed; recommendation: engine placement, not "
            "a data model (also a 'schema_design' or 'synthesis' run with no engine to "
            "design: see summary.design_note); designed: engine placement and a schema "
            "design's data model for summary.designed_engines (the designs are in "
            "`artifacts`)"
        ),
    )
    library_version: str
    ruleset_version: str
    ruleset_hash: str
    options: AssessmentOptions
    scope: WorkloadScope | None = None
    summary: Summary
    statements: list[StatementResult]
    groups: list[Group] = Field(default_factory=list)
    artifacts: Artifacts | None = None

    @model_validator(mode="after")
    def _label_matches_the_phase(self) -> AssessmentResult:
        reached = self.summary.run_through_reached
        expected = result_label(reached, self.summary.designed_engines)
        if self.label != expected:
            designed = (
                f" with {len(self.summary.designed_engines)} designed engines"
                if reached in MODEL_PHASES
                else ""
            )
            raise ValueError(
                f"label must be {expected!r} for run_through_reached={reached!r}"
                f"{designed}, not {self.label!r}"
            )
        return self
