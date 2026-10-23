"""The typed request and result of :func:`src.assessment.assess` (contract 1.0).

The stable surface is ``AssessmentRequest`` -> ``AssessmentResult`` with ``summary``,
``statements[]`` and ``groups[]``. It is versioned on its own (``contract_version``,
semantic versioning), separately from the pipeline's internal contracts. Internal
contracts appear only in the optional ``artifacts`` block, verbatim and with no
compatibility promise.

The result is for engine placement: which engine serves each statement, which
statements move together and which engine owns each table. It is not a data model.
Partition keys, sort keys, indexes and target tables come only from schema design
(``run_through="schema_design"``, which needs a model); nothing the deterministic
analysis guesses about a data model is part of this surface.

Engine ids are the engine catalog's active ids (``src/catalog/engines``). The request
validates them; the result carries them as plain strings.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.shared.database_name import validate_database_name
from src.shared.target_databases import normalize_target_databases

#: The version of this public surface (not of any internal contract).
CONTRACT_VERSION: Literal["1.0"] = "1.0"

RunThrough = Literal["analysis", "reality_check", "schema_design", "synthesis"]

#: The ``run_through`` values this release runs. The others are part of the contract
#: and refused, with a message, until they are implemented (#1387).
SUPPORTED_RUN_THROUGH: tuple[str, ...] = ("analysis", "reality_check")

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

COMPATIBILITY_NOTE: Literal["none: internal contracts, may change in any release"] = (
    "none: internal contracts, may change in any release"
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- request


class AssessmentOptions(_Strict):
    """How far to run and over which engines. Echoed in the result as applied."""

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
            "'reality_check' adds the final engine placement. 'schema_design' (the data "
            "model, which needs a model) and 'synthesis' are refused in this release."
        ),
    )
    deterministic: bool = Field(
        True,
        description=(
            "Run with no model. Analysis never uses one; with False the reality check "
            "has a model review its consolidations and write its summary."
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

    @field_validator("run_through")
    @classmethod
    def _supported_run_through(cls, value: str) -> str:
        if value not in SUPPORTED_RUN_THROUGH:
            what = " (the data model, which needs a model)" if value == "schema_design" else ""
            raise ValueError(
                f"run_through={value!r}{what} is not implemented yet; this release runs "
                f"{' or '.join(repr(v) for v in SUPPORTED_RUN_THROUGH)}"
            )
        return value


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
        description="1 to 128 ASCII letters, digits, underscores or hyphens.",
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
            "the owner has most; most_queries: no writes, the engine serving most of its "
            "queries; retained: no owner engine queries it, the retained relational "
            "engine; design: no queries, from the schema design"
        ),
    )
    also_read_by: list[str] = Field(
        default_factory=list,
        description="Other engines whose statements read it (each needs a replica), most first",
    )
    cached_by: str | None = Field(None, description="The cache in front of its reads, if any")


class Summary(_Strict):
    run_through_reached: RunThrough
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
    tables: list[TableLine] = Field(
        default_factory=list, description="One line per source table; empty at 'analysis'"
    )


class StatementResult(_Strict):
    query_id: str
    query_tags: list[str] = Field(
        default_factory=list, description="Leading or trailing comment tags; never read by routing"
    )
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
    matched_patterns: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Per analysed engine, the catalog access-pattern ids it matches",
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
    """Internal contracts, verbatim. No compatibility promise; each keeps its own version."""

    model_config = ConfigDict(extra="allow")

    compatibility: Literal["none: internal contracts, may change in any release"] = (
        COMPATIBILITY_NOTE
    )


class AssessmentResult(_Strict):
    contract_version: Literal["1.0"] = CONTRACT_VERSION
    library_version: str
    ruleset_version: str
    ruleset_hash: str
    options: AssessmentOptions
    scope: WorkloadScope | None = None
    summary: Summary
    statements: list[StatementResult]
    groups: list[Group] = Field(default_factory=list)
    artifacts: Artifacts | None = None
