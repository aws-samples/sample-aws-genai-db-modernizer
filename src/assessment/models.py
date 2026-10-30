# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The typed request and result of :func:`src.assessment.assess` (contract 1.3).

The stable surface is ``AssessmentRequest`` -> ``AssessmentResult`` with ``summary``,
``statements[]`` and ``groups[]``. It is versioned on its own (``contract_version``,
semantic versioning), separately from the pipeline's internal contracts. Internal
contracts appear only in the optional ``artifacts`` block, verbatim and with no
compatibility promise.

The stable surface is for engine placement: which engine serves each statement, which
statements move together and which engine owns each table. Partition keys, sort keys,
indexes and target tables come only from schema design (``run_through="schema_design"``,
which needs a model): a designed result's ``designs`` carries each designed engine's
target tables, keys and indexes, copied from its design (the full designs are in the
``artifacts`` block). Nothing the deterministic analysis guesses about a data model is
part of this surface.

Engine ids are the engine catalog's active ids (``src/catalog/engines``). The request
validates them; the result carries them as plain strings.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    ValidationError,
    field_validator,
    model_validator,
)

from src.shared.database_name import DATABASE_NAME_PATTERN, validate_database_name
from src.shared.target_databases import normalize_target_databases

#: The version of this public surface (not of any internal contract).
CONTRACT_VERSION: Literal["1.3"] = "1.3"

#: The ``contract_version`` values the models read and write: 1.3 only. Any other value
#: is refused; the retired ones (:data:`RETIRED_CONTRACT_VERSIONS`) with
#: :class:`RetiredContractVersionError`.
ContractVersion = Literal["1.3"]

#: The contract versions this release refuses by name (#1641): 1.0, 1.1 and 1.2 were
#: initial development versions during the beta, removed in :data:`RETIRED_IN`.
RETIRED_CONTRACT_VERSIONS: tuple[str, ...] = ("1.0", "1.1", "1.2")

#: The release that removed :data:`RETIRED_CONTRACT_VERSIONS`.
RETIRED_IN = "v0.4.0-beta.3"


class RetiredContractVersionError(ValueError):
    """A request or a result names a retired contract version (1.0, 1.1 or 1.2).

    This release supports contract 1.3 only: the earlier versions were beta development
    versions, removed in v0.4.0-beta.3. Pin ``contract_version`` "1.3" (or send none);
    docs/contracts/assess.md's "Removed: 1.0, 1.1 and 1.2" says how to migrate. An
    unknown version is refused as any invalid field is (``pydantic.ValidationError``)."""

    def __init__(self, version: str) -> None:
        self.version = version
        super().__init__(
            f"contract_version {version!r} is no longer supported: this release supports "
            f'contract {CONTRACT_VERSION} only (pin contract_version "{CONTRACT_VERSION}", '
            "or send none). Versions "
            f"{', '.join(RETIRED_CONTRACT_VERSIONS[:-1])} and {RETIRED_CONTRACT_VERSIONS[-1]} "
            f"were beta development versions, removed in {RETIRED_IN}; to migrate, see "
            "docs/contracts/assess.md, 'Removed: 1.0, 1.1 and 1.2'."
        )


#: The engines with an entry in the design block: a result has an entry for each of its
#: designed engines in the set, in ``summary.designed_engines`` order.
DESIGN_ENTRY_ENGINES: frozenset[str] = frozenset(
    {"dynamodb", "aurora_mysql", "aurora_postgresql", "documentdb", "elasticache", "opensearch"}
)

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

#: Why the post-schema routing left a flagged statement where it is (the router's
#: ``TerminalQuery.cause``): ``no_kept_engine`` (no other engine the placement keeps, in
#: the engine scope, can take it) or ``depth_limit`` (the routing reached its last pass).
PostSchemaTerminalCause = Literal["no_kept_engine", "depth_limit"]

#: The reason category of a design's flag (the router's ``FlagReason`` values): why the
#: design or its reviewer found the engine unable to serve the statement. Adding a value
#: is a MINOR change of this contract.
FlagReasonCode = Literal["full_scan", "client_side_join", "missing_feature", "other"]

#: A reality-check consolidation's kind: every statement moved off the engine
#: (``full``), or some stayed (``partial``). Adding a value is a MINOR change.
ConsolidationAction = Literal["full", "partial"]

#: How a table's owner was chosen (the facts layer's owner basis, ``src.shared.report_facts``).
OwnerBasis = Literal["writes", "split_writes", "most_queries", "retained", "design"]

#: What a result is, so a routing-only result is never read as a data model:
#: ``analysis_only`` (scores, nothing placed), ``recommendation`` (engine placement, no
#: data model) and ``designed`` (a schema design ran, with a model, for at least one
#: engine; its data model is in ``designs``). A run through ``schema_design`` or
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

#: How a statement's facts were read (``StatementResult.parse_status``): ``parsed`` (the
#: SQL parser read it) or ``fallback`` (a pattern-based estimate). Adding a value is a
#: MINOR change of this contract.
ParseStatus = Literal["parsed", "fallback"]

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


if TYPE_CHECKING:  # its name, for the type checker only
    from pydantic._internal._model_construction import ModelMetaclass as _ModelMetaclass
else:
    #: The metaclass pydantic gives every model.
    _ModelMetaclass = type(BaseModel)


class _RefusesRetiredVersions(_ModelMetaclass):
    """Building a model (``Model(...)``) raises :class:`RetiredContractVersionError` for
    a retired version. It wraps the call rather than ``__init__``: a custom ``__init__``
    makes pydantic run ``model_validate`` through it, which drops its ``context``."""

    def __call__(cls, *args: Any, **kwargs: Any) -> Any:
        try:
            return super().__call__(*args, **kwargs)
        except ValidationError as exc:
            _raise_if_retired(exc)
            raise


class _VersionChecked(_Strict, metaclass=_RefusesRetiredVersions):
    """A document with a ``contract_version``: one naming a retired version is refused.

    Building it (``Model(...)``) and ``model_validate``, ``model_validate_json`` and
    ``model_validate_strings`` raise :class:`RetiredContractVersionError` itself. Any
    other path pydantic validates by (a ``TypeAdapter``, a model holding this one) raises
    a ``pydantic.ValidationError`` whose ``contract_version`` error carries the same
    message, with the error itself in its ``ctx``."""

    @field_validator("contract_version", mode="before", check_fields=False)
    @classmethod
    def _refuse_a_retired_version(cls, value: Any) -> Any:
        if isinstance(value, str) and value in RETIRED_CONTRACT_VERSIONS:
            raise RetiredContractVersionError(value)
        return value

    @classmethod
    def model_validate(cls, obj: Any, *args: Any, **kwargs: Any) -> Self:
        try:
            return super().model_validate(obj, *args, **kwargs)
        except ValidationError as exc:
            _raise_if_retired(exc)
            raise

    @classmethod
    def model_validate_json(cls, json_data: Any, *args: Any, **kwargs: Any) -> Self:
        try:
            return super().model_validate_json(json_data, *args, **kwargs)
        except ValidationError as exc:
            _raise_if_retired(exc)
            raise

    @classmethod
    def model_validate_strings(cls, obj: Any, *args: Any, **kwargs: Any) -> Self:
        try:
            return super().model_validate_strings(obj, *args, **kwargs)
        except ValidationError as exc:
            _raise_if_retired(exc)
            raise


def _raise_if_retired(exc: ValidationError) -> None:
    """Raise the :class:`RetiredContractVersionError` that ``exc`` wraps, if any: the one
    the ``contract_version`` validator raised for a retired version."""
    for error in exc.errors():
        raised = (error.get("ctx") or {}).get("error")
        if error.get("loc") == ("contract_version",) and isinstance(
            raised, RetiredContractVersionError
        ):
            raise raised from exc


def check_contract_version(version: Any) -> None:
    """Refuse a ``contract_version`` other than :data:`CONTRACT_VERSION`, for a request
    that skipped validation (``model_construct``, ``model_copy(update=...)``, a field set
    after it was built): :class:`RetiredContractVersionError` for a retired one, else a
    ``ValueError``."""
    if isinstance(version, str) and version in RETIRED_CONTRACT_VERSIONS:
        raise RetiredContractVersionError(version)
    if version != CONTRACT_VERSION:
        raise ValueError(
            f"contract_version {version!r} is not supported: this release supports "
            f"contract {CONTRACT_VERSION} only"
        )


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


#: Why a cut could not place a statement (``UnplacedStatement.reason``): it reads no table
#: (``no_tables_read``: a procedure call, a sequence read, a function call, a session
#: command), or only tables the collection does not hold (``tables_not_collected``: system
#: catalogs, an unlisted materialized view). Adding a value is a MINOR change.
UnplacedReason = Literal["no_tables_read", "tables_not_collected"]


class UnplacedStatement(_Strict):
    """A statement a cut could not place: it reads no table of the collection, so it
    belongs to no cut's tables."""

    query_id: str
    reason: UnplacedReason = Field(
        ...,
        description=(
            "no_tables_read: it reads no table (a procedure call, a sequence read, a "
            "function call, a session command); tables_not_collected: it reads only tables "
            "the collection does not hold (system catalogs, an unlisted materialized view)"
        ),
    )


class WorkloadScope(_Strict):
    """Informational: what a cut workload left out. Echoed back unchanged; no rule reads it.

    ``cut_collection`` (#1633) writes one for each cut: the tables kept, the statement
    parts in ``rewritten_query_ids``, the foreign keys it cut in ``dropped_foreign_keys``
    and the statements it could not place in ``unplaced_statements``."""

    tables: list[str] = Field(default_factory=list)
    derived_from: str | None = None
    rewritten_query_ids: list[str] = Field(
        default_factory=list,
        description="Statements the caller rewrote; marked rewritten_by_caller in the result.",
    )
    dropped_foreign_keys: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Foreign keys the cut workload left out. cut_collection writes one "
            "object per foreign key between a kept table and a dropped one: "
            "constraint_name, table_id (the table that holds the key) and "
            "referenced_table_id. Any other object is echoed as given"
        ),
    )
    unplaced_statements: list[UnplacedStatement] = Field(
        default_factory=list,
        description=(
            "The statements the cut left out because they read no table of the "
            "collection (none of them belongs to any cut's tables), each with its reason, "
            "unless the caller gave a part for it"
        ),
    )


class AssessmentRequest(_VersionChecked):
    """One assessment: a database's raw collection and the options to run it with."""

    contract_version: ContractVersion = Field(
        CONTRACT_VERSION,
        description=(
            "The contract version the caller builds on: 1.3, the only version this "
            "release reads (with none, 1.3). The retired 1.0, 1.1 and 1.2 are refused with "
            "RetiredContractVersionError; any other value is refused as invalid"
        ),
    )
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
            "(statement text, execution statistics and the schema), or a cut's collection "
            "(cut_collection). A collector output (any version the library reads) is accepted too, but "
            "every derived fact is read again from each statement's text by the "
            "library's parser; derived fields it carries are ignored."
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
    unique_query_ids: list[str] | None = Field(
        None,
        description=(
            "The statements `unique_queries` counts, by query id, in the "
            "reality check's order: its first-pass unique-value set for the engine. "
            "None when the reality check did not assess it"
        ),
    )


class Consolidation(_Strict):
    """One reality-check consolidation: statements moved off an engine that added no
    unique value."""

    from_engine: str
    to_engine: str
    query_count: int = Field(..., ge=0, description="Statements it moved")
    reason: str = Field(..., description="Customer-facing sentence; do not parse")
    action: ConsolidationAction | None = Field(
        None,
        description=(
            "full: every statement it held moved off from_engine; partial: "
            "some stayed (`queries_retained`). The library always writes it"
        ),
    )
    queries_retained: list[str] = Field(
        default_factory=list,
        description=(
            "A partial consolidation's statements that stayed on from_engine, "
            "by query id; empty for a full one"
        ),
    )


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


class TableColumn(_Strict):
    """One column of a source table, as the collection describes it."""

    name: str
    data_type: str = Field(..., description="Its type in the source engine, as collected")
    nullable: bool | None = Field(
        None,
        description=(
            "Whether it allows NULL; None when unknown, which includes a raw "
            "collection that gives no `is_nullable`, NULL or a value no collection script "
            "writes; a collector output carries the collector's value"
        ),
    )


#: Where a foreign key's average comes from (``TableForeignKey.basis``): the source
#: catalogue's row counts, as the collection read them: ``catalogue_exact`` when both are
#: exact counts (``row_count_basis`` exact), else ``catalogue_estimate``. Adding a value
#: is a MINOR change of this contract.
ForeignKeyRowsBasis = Literal["catalogue_estimate", "catalogue_exact"]

#: What the foreign key row counts are, in the schema and in docs/contracts/assess.md.
FOREIGN_KEY_ROWS_NOTE = (
    "These are statistics, not a design decision: an average cannot show whether a "
    "child set is bounded. For any data model, the recommended path is "
    'run_through="schema_design".'
)


class TableForeignKey(_Strict):
    """One foreign key constraint of a source table (its columns in constraint order).

    It carries the average of its two tables' row counts, read from the
    collection with no model call. These are statistics, not a design decision: an
    average cannot show whether a child set is bounded. For any data model, the
    recommended path is run_through="schema_design"."""

    constraint_name: str
    columns: list[str] = Field(default_factory=list, description="The referencing columns")
    referenced_table: str | None = Field(
        ...,
        description=(
            "The referenced table, as a `table_id` of this result (the referencing "
            "table's schema first, since collectors keep only the bare name); None when the "
            "collection does not hold that table, or the name is ambiguous"
        ),
    )
    referenced_columns: list[str] = Field(
        default_factory=list, description="The referenced columns, paired with `columns`"
    )
    avg_children_per_parent: float | None = Field(
        None,
        ge=0,
        description=(
            "The table's row count divided by the referenced table's, unrounded: "
            "rows of this table per row of `referenced_table`, on average. None when "
            "`referenced_table` is None, either table's `row_count` is None (unknown), or "
            "the referenced table has 0 rows; 0 only when both counts are known. "
            + FOREIGN_KEY_ROWS_NOTE
        ),
    )
    basis: ForeignKeyRowsBasis | None = Field(
        None,
        description=(
            "Where `avg_children_per_parent` comes from: the source catalogue's "
            "row counts, as the collection read them; catalogue_exact when both tables' "
            "`row_count_basis` is exact, catalogue_estimate otherwise. None when the average "
            "is None"
        ),
    )


#: How a table's row count was recorded (``TableFacts.row_count_basis``): ``exact`` (the
#: catalogue keeps an exact count: MySQL-family MyISAM and Aria) or ``estimate``
#: (PostgreSQL reltuples, Oracle NUM_ROWS, InnoDB TABLE_ROWS, SQL Server
#: sys.partitions.rows). Adding a value is a MINOR change of this contract.
RowCountBasis = Literal["exact", "estimate"]


class TableFacts(_Strict):
    """One collected source table's facts, as the library read them from the collection."""

    table_id: str = Field(
        ..., description="The source table, as `statements[].tables` and `summary.tables` name it"
    )
    row_count: int | None = Field(
        None,
        ge=0,
        description=(
            "Rows, as the source's catalogue records them. None when the "
            "collection has no count for the table, so 0 is an empty table"
        ),
    )
    row_count_basis: RowCountBasis | None = Field(
        None,
        description=(
            "How `row_count` was recorded: exact (MySQL-family MyISAM and Aria) "
            "or estimate (PostgreSQL reltuples, Oracle NUM_ROWS, InnoDB TABLE_ROWS, SQL "
            "Server sys.partitions.rows). None when `row_count` is None or the collection "
            "does not say"
        ),
    )
    size_mb: float | None = Field(
        None,
        ge=0,
        description=(
            "Size in megabytes, data and indexes, as collected. None when the "
            "collection reports no size for the table"
        ),
    )
    columns: list[TableColumn] = Field(
        default_factory=list, description="Its columns, in the table's column order"
    )
    primary_key: list[str] = Field(
        default_factory=list, description="Its primary key columns, in order; empty for none"
    )
    foreign_keys: list[TableForeignKey] = Field(
        default_factory=list, description="Its foreign keys, one entry per constraint"
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
    post_schema_terminal_count: int = Field(
        0,
        ge=0,
        description=(
            "Statements with `post_schema_terminal` set: a design flagged them and the "
            "post-schema routing could not move them; 0 before 'schema_design'"
        ),
    )
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
            "The engines with a schema design (in `designs` and "
            "`artifacts.schema_designs`), in triage's order; empty before 'schema_design', "
            "and when no engine could be designed"
        ),
    )
    design_note: str | None = Field(
        None,
        description=(
            "Customer-facing sentence; do not parse. Set only when 'schema_design' ran "
            "and no engine could be designed (the result is then a 'recommendation')"
        ),
    )
    table_facts: list[TableFacts] = Field(
        default_factory=list,
        description=(
            "One entry per collected source table, by table_id, from "
            "'analysis' on: its row count, size, columns, primary key and foreign keys"
        ),
    )


class PatternRef(_Strict):
    """A pattern from the pattern catalog (``src/catalog/patterns``)."""

    id: str = Field(..., description="The catalog's pattern id")
    name: str = Field(..., description="The catalog's display name for it")


class PostSchemaTerminal(_Strict):
    """A statement a schema design flagged that the post-schema routing could not move:
    it stays on the flagging engine, and the pipeline's deliverables show it as
    application-layer work there."""

    engine: str = Field(
        ...,
        description="The engine whose design flagged it, by id; the statement stays on it",
    )
    flag_reason: FlagReasonCode = Field(
        ...,
        description=(
            "The flag's reason category: full_scan, client_side_join, missing_feature or other"
        ),
    )
    cause: PostSchemaTerminalCause = Field(
        ...,
        description=(
            "no_kept_engine: no other engine the placement keeps, in the engine scope, can "
            "take it; depth_limit: the post-schema routing reached its last pass"
        ),
    )
    reason: str = Field(..., description="Customer-facing sentence; do not parse")


class AntiPatternRef(_Strict):
    """A penalised anti-pattern finding on a statement (``src/catalog/patterns``)."""

    id: str = Field(..., description="The catalog's pattern id")
    name: str = Field(..., description="The catalog's display name for it")
    intrinsic: bool = Field(
        ...,
        description=(
            "True: it costs the engines it is found against points. False: fixable, a "
            "redesign removes it, so it costs nothing (#1231)"
        ),
    )


class FitGate(_Strict):
    """Why an engine could not be scored for a statement."""

    kind: str = Field(
        ...,
        description=(
            "The gate, for example hard_exclusion, capability or model_flag (an open "
            "list: read it with a default branch)"
        ),
    )
    reason: str = Field(..., description="Customer-facing sentence; do not parse")


class FitPattern(_Strict):
    """The statement's pattern dimension on one engine."""

    model_config = ConfigDict(
        **_Strict.model_config, serialize_by_alias=True, validate_by_name=True
    )

    score: int = Field(..., ge=0, le=100)
    source: str = Field(..., description="Where it comes from: 'statement' (its own class)")
    class_: str = Field(
        ...,
        alias="class",
        description="The access-pattern class whose score it is (after a redesign, if one applies)",
    )


class FitCost(_Strict):
    """The statement's cost dimension on one engine."""

    score: int = Field(..., ge=0, le=100)
    source: str = Field(..., description="Where it comes from: 'statement' (its own cost)")


class FitTable(_Strict):
    """One of the statement's rated tables, as the base score read it."""

    table_id: str
    complexity: int | None = Field(
        None, description="The table's complexity dimension; None under an unclassified method"
    )
    performance: int | None = Field(
        None, description="The table's performance dimension; None under an unclassified method"
    )
    weights: dict[str, float] | None = Field(
        None,
        description=(
            "The weight of each dimension (pattern, complexity, performance, cost) for "
            "this table; None under an unclassified method"
        ),
    )
    score: int = Field(
        ...,
        description=(
            "per_table_weighted_mean: the weighted sum of the four dimensions, its "
            "fraction dropped; an unclassified method: the table's confidence"
        ),
    )


class FitPenalty(_Strict):
    """One anti-pattern finding on the statement for one engine."""

    id: str = Field(..., description="The catalog's pattern id")
    name: str = Field(..., description="The catalog's display name for it")
    points: int = Field(..., ge=0, description="What it costs the engine; 0 when fixable")
    intrinsic: bool


class FitBreakdown(_Strict):
    """How the routing scorer reached one engine's `fit` for a statement, written by the
    scorer from the same arithmetic. It explains `fit`; it never decides the engine."""

    gate: FitGate | None = Field(
        None,
        description="Set when the engine is gated: it then has no score and every other field is empty",
    )
    method: str | None = Field(
        None,
        description=(
            "The base score's rule: per_table_weighted_mean, pattern_only_no_class_score, "
            "unclassified_own_tables, unclassified_all_tables or "
            "unclassified_pattern_tables (an open list)"
        ),
    )
    pattern: FitPattern | None = None
    cost: FitCost | None = None
    tables: list[FitTable] = Field(default_factory=list)
    base: int | None = Field(
        None, description="The mean of the tables' scores, its fraction dropped (0 for none)"
    )
    penalties: list[FitPenalty] = Field(default_factory=list)
    fit: int | None = Field(
        None, ge=0, le=100, description="max(0, base minus the largest penalty points)"
    )


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
    calls_per_second: float | None = Field(
        None,
        ge=0,
        description=(
            "Its execution count over the day the collection covers, per second. None "
            "when a raw collection gives no execution count, and 0.0 for a count of 0; a "
            "collector output carries its own figure"
        ),
    )
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
    post_schema_terminal: PostSchemaTerminal | None = Field(
        None,
        description=(
            "Set when a schema design flagged the statement on its engine and the "
            "post-schema routing could not move it (it keeps its engine and reason_code); "
            "None otherwise and before 'schema_design'"
        ),
    )
    query_text: str | None = Field(
        None,
        description=(
            "The statement text the parser read: when rows sharing a digest "
            "merged, the costliest variant's. Collection text: escape it when rendering. "
            "None when the collection gives the row no text"
        ),
    )
    parse_status: ParseStatus | None = Field(
        None,
        description=(
            "How its facts (`query_type`, `join_count`, `tables`) were read: "
            "parsed (the SQL parser read the statement) or fallback (the parser could not, "
            "so they are a pattern-based estimate). None when the collection gives no "
            "statement text"
        ),
    )
    matched_anti_patterns: list[AntiPatternRef] = Field(
        default_factory=list,
        description=(
            "The penalised anti-patterns found on it against any analysed "
            "engine, each once, by id; each engine's points are in `fit_breakdown`"
        ),
    )
    fit_breakdown: dict[str, FitBreakdown] = Field(
        default_factory=dict,
        description=(
            "Per engine the scorer saw: how it reached `fit` (every engine in "
            "`fit` has one, with the same `fit`), or the gate that left it unscored"
        ),
    )
    from_query_id: str | None = Field(
        None,
        description=(
            "Set on a statement part (a caller's rewrite of a statement that "
            "crossed a cut, built by cut_collection): the query_id of the statement it was "
            "cut from, read from the part's id. None for every other statement"
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


# --------------------------------------------------------------------------- designs
#
# The stable design block (#1493): per designed engine, only what a caller needs to
# build on a data model. Every value is copied from that engine's schema design (the
# designer's output, never the deterministic analysis); the full design stays in
# ``artifacts.schema_designs``. Names come from the design (a model wrote them, from
# collection text): treat them as data and escape them when rendering.


class _Design(_Strict):
    validation_passed: bool = Field(
        ...,
        description=(
            "The design's own checks passed. False for a design that failed them (the "
            "reasons are in `artifacts.schema_designs.<engine>.validation_failures`) or "
            "that records no result: check it before you build on the design"
        ),
    )


class DynamoDBKey(_Strict):
    """One key attribute."""

    name: str
    type: Literal["S", "N", "B"] = Field(..., description="DynamoDB attribute type")


class DynamoDBIndex(_Strict):
    """One global secondary index."""

    name: str
    partition_key: list[DynamoDBKey] = Field(
        ..., description="Its partition key attributes, in order (1 to 4)"
    )
    sort_key: list[DynamoDBKey] = Field(
        default_factory=list, description="Its sort key attributes, in order; empty for none"
    )
    projection: Literal["ALL", "KEYS_ONLY", "INCLUDE"]
    projected_attributes: list[str] = Field(
        default_factory=list,
        description="The non-key attributes it projects when projection is INCLUDE; else empty",
    )
    query_ids: list[str] = Field(
        default_factory=list, description="The statements the design serves from this index"
    )


class DynamoDBEntity(_Strict):
    """One entity type an item-collection table holds."""

    source_table: str = Field(..., description="The source table it holds, as the design names it")
    pk_template: str = Field(
        ..., description="Its partition key value template, for example 'USER#{id}'"
    )
    sk_template: str = Field(
        ..., description="Its sort key value template, for example 'ORDER#{order_id}'"
    )


class DynamoDBTable(_Strict):
    """One target table."""

    name: str
    source_tables: list[str] = Field(
        default_factory=list, description="The source tables it holds, as the design names them"
    )
    partition_key: DynamoDBKey
    sort_key: DynamoDBKey | None = None
    gsis: list[DynamoDBIndex] = Field(default_factory=list)
    query_ids: list[str] = Field(
        default_factory=list,
        description="The statements the design serves from the table itself (not an index)",
    )
    aggregate_pattern: Literal["identifying_relationship", "item_collection", "separate"] | None = (
        Field(
            None,
            description=(
                "How the design shares the table among its source tables; None when the "
                "design does not say"
            ),
        )
    )
    entities: list[DynamoDBEntity] = Field(
        default_factory=list,
        description=(
            "An item collection's entity types, each with its key templates; "
            "empty for any other table"
        ),
    )


class DynamoDBDesign(_Design):
    """A DynamoDB design: its target tables, their keys and indexes."""

    engine: Literal["dynamodb"]
    tables: list[DynamoDBTable] = Field(default_factory=list)


class AuroraTable(_Strict):
    """One target table."""

    name: str
    primary_key: list[str] = Field(default_factory=list, description="Its primary key columns")
    indexes: list[str] = Field(
        default_factory=list, description="Its secondary indexes, as the design's CREATE INDEX DDL"
    )


class AuroraDesign(_Design):
    """An Aurora MySQL or Aurora PostgreSQL design: its target tables and indexes."""

    engine: Literal["aurora_mysql", "aurora_postgresql"]
    tables: list[AuroraTable] = Field(default_factory=list)
    ddl_artifact_path: str = Field(
        ...,
        description=(
            "Where the full generated DDL is: a dotted path in the result (with "
            "include_artifacts=True), for example "
            "'artifacts.schema_designs.aurora_mysql.generated_ddl'"
        ),
    )


class DocumentDBEmbed(_Strict):
    """How one source table is kept in a collection's documents."""

    source_table: str
    path: str = Field(..., description="Where in the parent document it is kept")
    strategy: Literal["embed", "reference", "hybrid"] = Field(
        ...,
        description=(
            "embed: kept inside the parent document; reference: kept apart and referenced "
            "by id; hybrid: some fields embedded, the rest referenced"
        ),
    )


class DocumentDBIndexKey(_Strict):
    field: str
    direction: int = Field(..., description="1 ascending, -1 descending")


class DocumentDBIndex(_Strict):
    name: str
    keys: list[DocumentDBIndexKey] = Field(..., description="Its key fields, in order")
    type: Literal["compound", "single", "text", "partial", "unique"]
    query_ids: list[str] = Field(
        default_factory=list, description="The statements the design names for this index"
    )


class DocumentDBCollection(_Strict):
    """One target collection."""

    name: str
    source_tables: list[str] = Field(default_factory=list)
    embedded: list[DocumentDBEmbed] = Field(
        default_factory=list, description="The embed or reference choice per child table"
    )
    referenced_collections: list[str] = Field(
        default_factory=list, description="Other collections its documents reference by id"
    )
    indexes: list[DocumentDBIndex] = Field(default_factory=list)
    query_ids: list[str] = Field(
        default_factory=list, description="The statements the design serves from it"
    )


class DocumentDBDesign(_Design):
    """A DocumentDB design: its collections, their indexes and embed choices."""

    engine: Literal["documentdb"]
    collections: list[DocumentDBCollection] = Field(default_factory=list)


class CacheKey(_Strict):
    """One cache key pattern and what it caches."""

    pattern: str = Field(..., description="The key pattern, for example 'wp_options:{option_name}'")
    data_type: str = Field(..., description="The Valkey data type, for example 'string' or 'hash'")
    ttl_seconds: int | None = Field(None, ge=0, description="Its TTL; None means no expiry")
    source_tables: list[str] = Field(
        default_factory=list, description="The source tables whose rows it caches"
    )
    query_ids: list[str] = Field(
        default_factory=list, description="The statements whose reads it serves"
    )


class ElastiCacheDesign(_Design):
    """An ElastiCache design: its key patterns, TTLs and what each key caches."""

    engine: Literal["elasticache"]
    keys: list[CacheKey] = Field(default_factory=list)


class OpenSearchField(_Strict):
    """One mapped field."""

    name: str = Field(..., description="The field name, for example 'post_title' or '@timestamp'")
    type: str = Field(
        ...,
        description=(
            "Its OpenSearch field type, as the design names it, for example 'text', "
            "'keyword', 'long', 'date' or 'nested'"
        ),
    )


class OpenSearchIndex(_Strict):
    """One search index."""

    name: str
    source_tables: list[str] = Field(
        default_factory=list, description="The source tables it holds, as the design names them"
    )
    fields: list[OpenSearchField] = Field(
        default_factory=list, description="Its mapped fields, in the design's order"
    )
    aliases: list[str] = Field(default_factory=list, description="Its index aliases")
    query_ids: list[str] = Field(
        default_factory=list, description="The statements the design serves from this index"
    )


class OpenSearchDataStream(_Strict):
    """One data stream (a time-series table), with the fields of its index template."""

    name: str
    source_tables: list[str] = Field(
        default_factory=list, description="The source tables it holds, as the design names them"
    )
    fields: list[OpenSearchField] = Field(
        default_factory=list,
        description="The mapped fields of its index template, in the design's order",
    )
    query_ids: list[str] = Field(
        default_factory=list, description="The statements the design serves from this data stream"
    )


class OpenSearchDesign(_Design):
    """An OpenSearch design: its indexes and data streams, their mapped fields and what
    each serves."""

    engine: Literal["opensearch"]
    indexes: list[OpenSearchIndex] = Field(default_factory=list)
    data_streams: list[OpenSearchDataStream] = Field(default_factory=list)


#: One designed engine's stable design, discriminated by ``engine``. Adding an engine
#: is a MINOR change of this contract: read it with a default branch.
EngineDesign = Annotated[
    DynamoDBDesign | AuroraDesign | DocumentDBDesign | ElastiCacheDesign | OpenSearchDesign,
    Field(discriminator="engine"),
]


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


class _NotCompared:
    """A value a result holds for the library's own use, outside the result's value: it
    is never serialised, and any two (or one and None) compare equal."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value

    def __eq__(self, other: object) -> bool:
        return other is None or isinstance(other, _NotCompared)

    __hash__ = None  # type: ignore[assignment]


class AssessmentResult(_VersionChecked):
    """One assessment's result: where each statement runs, which statements move
    together and which engine owns each table, and, for a designed result, each
    designed engine's data model (``designs``).

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

    contract_version: ContractVersion = Field(
        CONTRACT_VERSION,
        description=(
            "The contract version the result is written for: 1.3, the only version "
            "this release reads and writes. A result naming a retired version (1.0, 1.1 "
            "or 1.2) is refused with RetiredContractVersionError"
        ),
    )
    label: ResultLabel = Field(
        ...,
        description=(
            "analysis_only: scores, nothing placed; recommendation: engine placement, not "
            "a data model (also a 'schema_design' or 'synthesis' run with no engine to "
            "design: see summary.design_note); designed: engine placement and a schema "
            "design's data model for summary.designed_engines (in `designs`; the full "
            "designs are in `artifacts`)"
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
    designs: list[EngineDesign] | None = Field(
        None,
        description=(
            "Set only when label is 'designed' (None otherwise): one entry per engine in "
            "summary.designed_engines, in that order, each copied from the engine's "
            "schema design"
        ),
    )
    artifacts: Artifacts | None = None

    # The collector output the run read, kept for ``cut_collection`` (#1633). Never
    # written as JSON and never compared: two results that differ only here are equal.
    _cut_source: _NotCompared | None = PrivateAttr(None)

    def keep_cut_source(self, collector_output: dict[str, Any]) -> None:
        """Keep the run's collector output, for ``cut_collection`` (library use)."""
        self._cut_source = _NotCompared(collector_output)

    def cut_source(self) -> dict[str, Any] | None:
        """The run's collector output: the one kept by ``assess()``, else the one in
        ``artifacts`` (a result read from JSON, with ``include_artifacts=True``)."""
        if self._cut_source is not None:
            kept: dict[str, Any] = self._cut_source.value
            return kept
        carried = getattr(self.artifacts, "collector_output", None) if self.artifacts else None
        return carried if isinstance(carried, dict) else None

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
        if (self.designs is not None) != (self.label == "designed"):
            raise ValueError("designs is set exactly when label is 'designed'")
        for statement in self.statements:
            _check_fit_breakdown(statement)
        if self.designs is not None:
            expected_engines = [
                e for e in self.summary.designed_engines if e in DESIGN_ENTRY_ENGINES
            ]
            listed = [d.engine for d in self.designs]
            if listed != expected_engines:
                raise ValueError(
                    f"designs must have one entry per designed engine with an entry, in "
                    f"summary.designed_engines order: expected {expected_engines}, got {listed}"
                )
        return self


def _check_fit_breakdown(statement: StatementResult) -> None:
    """Every engine in ``fit`` has an ungated breakdown with the same
    ``fit``; every other entry is a gate only (no score, nothing else set), for an engine
    with no ``fit`` that ``excluded`` lists."""
    for engine, score in statement.fit.items():
        entry = statement.fit_breakdown.get(engine)
        if entry is None or entry.gate is not None or entry.fit != score:
            raise ValueError(
                f"statement {statement.query_id}: fit[{engine!r}] is {score}, but its "
                f"fit_breakdown entry is {entry.fit if entry else 'missing'}"
                f"{' (gated)' if entry and entry.gate else ''}"
            )
    for engine, entry in statement.fit_breakdown.items():
        if engine in statement.fit:
            continue
        if entry.gate is None:
            raise ValueError(
                f"statement {statement.query_id}: {engine!r} has no fit, so its "
                "fit_breakdown entry must be a gate"
            )
        only_a_gate = (
            entry.fit is None
            and entry.base is None
            and entry.method is None
            and entry.pattern is None
            and entry.cost is None
            and not entry.tables
            and not entry.penalties
        )
        if not only_a_gate or engine not in statement.excluded:
            raise ValueError(
                f"statement {statement.query_id}: {engine!r} is gated in fit_breakdown, so "
                "its entry must hold only the gate, and excluded must list it"
            )
