# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
DynamoDB Model Output Contract

Version History:
- 1.0 (2026-03-23): Initial version — access-pattern-first design output contract
                    with full attribute-level migration lineage (join, calculation,
                    transformation specs) for ETL pipeline consumption.
- 1.1 (2026-10-15): AccessPattern.condition_expression (optional): the
                    ConditionExpression a write carries, so the group-draft
                    merge reads conditional writes from a field instead of
                    matching words in the pattern's text (#810).
- 1.2 (2026-10-25, #1509): AccessPattern.returned_attributes (optional): the attributes
  a read returns, so the cost check charges a GSI fetch only when the projection does
  not cover them (#1509)
- 1.3 (2026-11-02, #1531): CalculationDefinition gains ConstantCalculation (``type:
  "constant"``, ``value``): an attribute that holds the same literal on every item of
  its entity (an entity-type label, a flag every item carries) names no source column;
  AttributeDefinition.source_column becomes optional for a constant and stays required
  for every other attribute
- 1.4 (2026-11-03, #802): Added optional ``unresolved_merge_conflicts``
  (UnresolvedMergeConflict): each group-merge failure with its access patterns and
  queries and a required ``status`` (unserved, at_risk, none_named, not_recorded); empty
  for a clean merge and in a design written before it, which still validates.
- 1.5 (2026-11-03, #1780): Trade-offs are DynamoDBTradeOff: the group merge's overlap
  review records optional ``authoritative_home`` and ``derived_copies`` (#1780), the
  choice it made, so readers never rank the tables again. Backward compatible: None on a
  review merged before.
- 1.6 (2026-11-04, #1221): Added optional ``trade_offs[].topic`` (a closed set the
  designer picks, default ``other``) and the pipeline-only ``trade_offs[].note_kind``
  (``review`` for the design review notes, ``warning`` for the schema agents' warnings;
  left out of the JSON schema), so repeated trade-offs group by topic and the pipeline's
  notes are listed apart.
"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .schema_design_output import TradeOff

# ---------------------------------------------------------------------------
# Enums (as str Enum via Literal in discriminated unions)
# ---------------------------------------------------------------------------


class DynamoDBOperation(str):
    pass


DYNAMODB_OPERATIONS = Literal[
    "GetItem",
    "PutItem",
    "UpdateItem",
    "DeleteItem",
    "Query",
    "BatchGetItem",
    "BatchWriteItem",
    "TransactWriteItems",
    "TransactGetItems",
]

DYNAMODB_ATTRIBUTE_TYPES = Literal["S", "N", "B", "SS", "NS", "BS", "M", "L", "BOOL", "NULL"]

PROJECTION_TYPES = Literal["KEYS_ONLY", "INCLUDE", "ALL"]

AGGREGATE_PATTERNS = Literal["identifying_relationship", "item_collection", "separate"]

UNSUPPORTED_PATTERN_TYPES = Literal["text_search", "aggregation"]

STORED_OBJECT_TYPES = Literal["procedure", "view", "trigger"]

PARTITION_OPERATIONS = Literal["read", "write"]


# ---------------------------------------------------------------------------
# Attribute migration lineage — joins
# ---------------------------------------------------------------------------


class SelfJoin(BaseModel):
    """Hierarchical data within the same table (e.g. categories.parent_id → categories.name)."""

    type: Literal["self-join"]
    join_alias: str = Field(
        ..., description="Alias for the self-referencing join, e.g. 'parent_cat'"
    )
    join_condition: str = Field(
        ..., description="SQL join condition, e.g. 'parent_cat.id = categories.parent_id'"
    )
    select_column: str = Field(..., description="Column to select from the joined alias")
    null_value: str | None = Field(None, description="Value to use when join returns NULL")


class ForeignKeyJoin(BaseModel):
    """Simple FK lookup — resolves an ID to a human-readable value."""

    type: Literal["foreign-key"]
    target_table: str = Field(..., description="Source table to join against")
    join_condition: str = Field(
        ..., description="SQL join condition, e.g. 'categories.id = products.category_id'"
    )
    select_column: str = Field(..., description="Column to select from target_table")


class MultiColumnJoin(BaseModel):
    """Composite FK join — join on two or more columns."""

    type: Literal["multi-column"]
    target_table: str
    join_conditions: list[str] = Field(
        ..., min_length=2, description="All join conditions (AND'd together)"
    )
    select_column: str


class ConditionalJoin(BaseModel):
    """Optional or polymorphic FK — only joins when a condition is met."""

    type: Literal["conditional"]
    condition: str = Field(
        ..., description="SQL condition that must be true for the join to execute"
    )
    target_table: str
    join_condition: str
    select_column: str
    else_value: str = Field(..., description="Value to use when condition is false")


class ChainJoinStep(BaseModel):
    target_table: str
    join_condition: str
    select_column: str


class ChainJoin(BaseModel):
    """Multi-hop join — traverses A → B → C to reach the final value."""

    type: Literal["chain"]
    joins: list[ChainJoinStep] = Field(
        ...,
        min_length=2,
        description="Ordered join steps; last step's select_column is the final value",
    )
    chain_separator: str | None = Field(
        None,
        description="When set, concatenates all intermediate select_column values with this separator",
    )


class LookupTableJoin(BaseModel):
    """Many-to-many via junction table — resolves junction FK to a display value."""

    type: Literal["lookup-table"]
    target_table: str
    join_condition: str
    select_column: str


class JsonConstructionDetail(BaseModel):
    type: Literal["array", "object"]
    select_columns: dict[str, str] = Field(
        ..., description="Mapping of DynamoDB attribute name → source column expression"
    )
    limit: int | None = Field(None, description="Maximum number of items for array construction")
    order_by: str | None = Field(
        None, description="SQL ORDER BY expression, e.g. 'reviews.created_at DESC'"
    )


class JsonConstructionJoin(BaseModel):
    """Denormalizes a related collection into a DynamoDB map (M) or list (L) attribute."""

    type: Literal["json-construction"]
    target_table: str
    join_condition: str
    construction: JsonConstructionDetail


class ExistsCheckJoin(BaseModel):
    """
    Checks whether at least one matching row exists in a related table.
    Produces a BOOL attribute — no value is selected, only existence is tested.

    Use when: the source query uses EXISTS(...), COUNT(*) > 0, or a left join
    checking for NULL to produce a boolean flag (e.g. has_upvoted, is_member).
    The AttributeDefinition.type must be 'BOOL'.
    """

    type: Literal["exists-check"]
    target_table: str = Field(..., description="Table to check for existence")
    join_condition: str = Field(..., description="SQL condition that constitutes a match")


class AggregatedListJoin(BaseModel):
    """
    Collects a single scalar column from all matching rows into a DynamoDB
    set (SS, NS) or list (L) attribute.

    Use when: the source query aggregates scalar values from a related table
    (e.g. all tag names for a discussion, all role names for a user).
    Differs from json-construction which produces maps — this produces flat sets/lists.
    The AttributeDefinition.type must be SS, NS, or L.
    """

    type: Literal["aggregated-list"]
    target_table: str
    join_condition: str
    select_column: str = Field(..., description="Scalar column to collect, e.g. 'tags.name'")
    order_by: str | None = Field(
        None, description="SQL ORDER BY when order matters (use L, not SS/NS)"
    )
    limit: int | None = Field(None, description="Maximum items to collect")


class PolymorphicLookupBranch(BaseModel):
    """One branch of a polymorphic dispatch."""

    when_type_value: str = Field(
        ..., description="Value of the type discriminator column that triggers this branch"
    )
    target_table: str
    join_condition: str
    select_column: str


class PolymorphicLookupJoin(BaseModel):
    """
    Resolves an attribute whose source table depends on the value of a type
    discriminator column in the same row (Rails-style polymorphic association).

    Use when: a table has a <entity>_type column alongside <entity>_id and the
    correct join target varies per row (e.g. commentable_type = 'Post' | 'Discussion').
    """

    type: Literal["polymorphic-lookup"]
    type_column: str = Field(
        ..., description="Discriminator column that determines which table to join"
    )
    branches: list[PolymorphicLookupBranch] = Field(
        ..., min_length=2, description="One branch per possible type_column value"
    )
    else_value: str | None = Field(
        None, description="Value when type_column doesn't match any branch"
    )


# Discriminated union — Pydantic resolves the correct model via the `type` field
JoinDefinition = Annotated[
    SelfJoin
    | ForeignKeyJoin
    | MultiColumnJoin
    | ConditionalJoin
    | ChainJoin
    | LookupTableJoin
    | JsonConstructionJoin
    | ExistsCheckJoin
    | AggregatedListJoin
    | PolymorphicLookupJoin,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Attribute migration lineage — calculations
# ---------------------------------------------------------------------------


class AggregateCalculation(BaseModel):
    """Computes a SQL aggregate (SUM, COUNT, etc.) from a related table."""

    type: Literal["aggregate"]
    target_table: str
    join_condition: str
    operation: Literal["SUM", "COUNT", "AVG", "MIN", "MAX"]
    select_column: str = Field(
        ...,
        description="Column or expression to aggregate, e.g. 'order_items.price * order_items.quantity'",
    )


class CaseEntry(BaseModel):
    when: str = Field(..., description="SQL WHEN condition")
    then: str = Field(..., description="SQL THEN value (quoted string or column reference)")


class CaseCalculation(BaseModel):
    """Maps source values to target values via CASE WHEN logic."""

    type: Literal["case"]
    cases: list[CaseEntry] = Field(..., min_length=1)
    else_value: str | None = Field(
        None, description="ELSE value; if absent the attribute is omitted when no case matches"
    )


class ConstantCalculation(BaseModel):
    """The same literal value on every item of the entity: an entity-type label, or a
    flag every item carries.

    A constant is on every item, so it never makes a sparse GSI sparse within its entity:
    a marker only some rows carry is a ``case`` calculation with no ``else_value``. It
    reads no source column, so a constant attribute names none (#1531). The value is
    data the designer wrote: it is never SQL, and its JSON type matches the attribute's
    DynamoDB type (``S`` a string, ``N`` a number, ``BOOL`` a boolean).
    """

    type: Literal["constant"]
    value: str | int | float | bool = Field(
        ..., description="The literal written to every item, e.g. 'wp_comments' or true"
    )


#: The JSON type of a constant's value for each DynamoDB type a constant can have.
_CONSTANT_VALUE_TYPES: dict[str, tuple[str, tuple[type, ...]]] = {
    "S": ("string", (str,)),
    "N": ("number", (int, float)),
    "BOOL": ("boolean", (bool,)),
}


CalculationDefinition = Annotated[
    AggregateCalculation | CaseCalculation | ConstantCalculation,
    Field(discriminator="type"),
]


# ---------------------------------------------------------------------------
# Attribute migration lineage — transformations
# ---------------------------------------------------------------------------


class Transformation(BaseModel):
    """Post-join value transformation applied before writing to DynamoDB."""

    type: Literal["date-format", "string-format", "number-format", "json-parse"]
    format: str | None = Field(None, description="Target format, e.g. 'YYYY-MM-DD' or '%.2f'")
    source_format: str | None = Field(
        None, description="Source format when explicit parsing is needed, e.g. 'timestamp'"
    )


# ---------------------------------------------------------------------------
# Attribute definition — the per-attribute ETL spec
# ---------------------------------------------------------------------------


class AttributeDefinition(BaseModel):
    """
    Describes one DynamoDB attribute and exactly how to populate it from the
    source MySQL schema. Exactly one of: direct column copy, join, or calculation.
    A transformation may be applied on top of any of those.

    A ``constant`` calculation is the one attribute with no source column: it names no
    ``source_column``, and its ``source_table`` is the table whose rows become the items
    that carry it. Every other attribute names its source column, as before (#1531).

    The contract refuses only a missing (``None``) ``source_column``. An empty one (``""``
    or ``[]``) still loads, because designs written before constants existed used it for a
    literal; the source-column check (``src/tools/schema/dynamodb_attribute_columns.py``)
    fails it where a design is finalized, so a new design declares a constant instead.
    """

    name: str = Field(..., description="DynamoDB attribute name")
    type: DYNAMODB_ATTRIBUTE_TYPES = Field(..., description="DynamoDB attribute type")
    source_table: str = Field(..., description="MySQL source table this attribute originates from")
    source_column: str | list[str] | None = Field(
        None,
        description=(
            "Source column(s). List only for multi-column joins. Otherwise a single column "
            "name. Required unless the calculation is a constant, which names none."
        ),
    )
    join: JoinDefinition | None = Field(
        None, description="Join spec to resolve the attribute value from another table"
    )
    calculation: CalculationDefinition | None = Field(
        None, description="Calculation spec for derived/computed values"
    )
    transformation: Transformation | None = Field(
        None, description="Post-resolution value transformation"
    )
    denormalized: bool = Field(
        default=False,
        description="True when this attribute duplicates data from another entity to avoid a lookup",
    )
    justification: str | None = Field(
        None,
        description="Required when denormalized=True. States which access pattern drives the denormalization.",
    )

    @model_validator(mode="after")
    def validate_denormalization_justification(self) -> "AttributeDefinition":
        if self.denormalized and not self.justification:
            raise ValueError("justification is required when denormalized=True")
        return self

    @model_validator(mode="after")
    def validate_source_or_constant(self) -> "AttributeDefinition":
        """A constant names no source column and no join; every other attribute names its
        source column (#1531)."""
        calc = self.calculation
        if not isinstance(calc, ConstantCalculation):
            if self.source_column is None:
                raise ValueError("source_column is required unless the calculation is a constant")
            return self
        if self.source_column:
            raise ValueError(
                "a constant names no source_column: its value is the literal, not a column"
            )
        if self.join is not None:
            raise ValueError("a constant takes no join: its value is the literal")
        expected = _CONSTANT_VALUE_TYPES.get(self.type)
        if expected is None:
            raise ValueError(f"a constant of type {self.type} is not supported: use S, N or BOOL")
        kind, allowed = expected
        value = calc.value
        # bool is an int subclass: a boolean is never a number constant.
        if not isinstance(value, allowed) or (isinstance(value, bool) and bool not in allowed):
            raise ValueError(f"a constant of type {self.type} needs a {kind} value")
        return self


# ---------------------------------------------------------------------------
# Entity definition — one entity type within an item collection
# ---------------------------------------------------------------------------


class EntityDefinition(BaseModel):
    """
    Describes one entity type stored in an item collection (multi-entity DynamoDB table).
    Maps to one source MySQL table and defines the SK prefix and key generation templates.
    """

    entity_type: str = Field(
        ..., description="SK prefix identifier, e.g. 'USER', 'ORDER', 'ADDRESS'"
    )
    source_table: str = Field(..., description="MySQL source table_id for this entity type")
    pk_template: str = Field(
        ...,
        description="PK value template using source column references, e.g. 'USER#{id}'. "
        "Tokens in braces are replaced with source column values.",
    )
    sk_template: str = Field(
        ...,
        description="SK value template, e.g. 'PROFILE' or 'ADDRESS#{type}'. "
        "Static prefixes or templates with source column tokens.",
    )
    attributes: list[AttributeDefinition] = Field(
        ..., min_length=1, description="All DynamoDB attributes for this entity type"
    )


# ---------------------------------------------------------------------------
# Key and index definitions
# ---------------------------------------------------------------------------


class KeyDefinition(BaseModel):
    attribute_name: str = Field(..., description="DynamoDB attribute name")
    attribute_type: Literal["S", "N", "B"] = Field(..., description="DynamoDB key attribute type")


class GSIDefinition(BaseModel):
    """Complete definition of one Global Secondary Index."""

    gsi_name: str = Field(..., description="Descriptive GSI name, e.g. 'DiscussionsByTag'")
    partition_key: list[KeyDefinition] = Field(
        ...,
        min_length=1,
        max_length=4,
        description="Multi-attribute GSI partition key (1–4 attributes). Never use composite strings.",
    )
    sort_key: list[KeyDefinition] | None = Field(
        None,
        min_length=1,
        max_length=4,
        description="Multi-attribute GSI sort key. Equality attributes must come before range attributes.",
    )
    projection: PROJECTION_TYPES = Field(..., description="GSI projection type")
    projected_attributes: list[str] | None = Field(
        None,
        description="Projected attribute names when projection=INCLUDE. List only what access patterns actually read.",
    )
    sparse_attribute: str | None = Field(
        None, description="Attribute whose presence controls index inclusion (sparse GSI)."
    )
    item_count: int = Field(..., ge=0)
    item_size_bytes: int = Field(
        ..., ge=0, description="Average projected item size. Must be ≤ table item_size_bytes."
    )


# ---------------------------------------------------------------------------
# Table definition — one DynamoDB table
# ---------------------------------------------------------------------------


class TableDefinition(BaseModel):
    """
    DynamoDB table schema and full ETL spec. One entry per DynamoDB table —
    which may consolidate multiple MySQL source tables.

    - Single-entity tables: populate `attributes` at this level.
    - Item collections (multi-entity): populate `entities`; each entity carries
      its own attribute list, pk_template, and sk_template.
    Exactly one of `attributes` or `entities` must be provided.
    """

    table_name: str = Field(..., description="DynamoDB table name")
    aggregate_pattern: AGGREGATE_PATTERNS = Field(
        ..., description="Design pattern used to consolidate source tables"
    )
    source_tables: list[str] = Field(
        ..., min_length=1, description="MySQL table_ids consolidated into this DynamoDB table"
    )
    partition_key: KeyDefinition
    sort_key: KeyDefinition | None = None

    # Single-entity: attributes at table level
    attributes: list[AttributeDefinition] | None = Field(
        None,
        description="Attribute definitions for single-entity tables. "
        "Mutually exclusive with `entities`.",
    )

    # Multi-entity (item collection): each entity carries its own attribute list
    entities: list[EntityDefinition] | None = Field(
        None,
        description="Entity definitions for item collection tables (aggregate_pattern=item_collection). "
        "Mutually exclusive with `attributes`.",
    )

    gsis: list[GSIDefinition] = Field(default_factory=list)
    item_count: int = Field(
        ..., ge=0, description="Total items across all consolidated source entities"
    )
    item_size_bytes: int = Field(
        ..., ge=1, description="Average item size in bytes across all item types"
    )

    @model_validator(mode="after")
    def validate_attributes_xor_entities(self) -> "TableDefinition":
        has_attrs = self.attributes is not None and len(self.attributes) > 0
        has_entities = self.entities is not None and len(self.entities) > 0
        if has_attrs == has_entities:
            raise ValueError(
                "Exactly one of 'attributes' (single-entity) or 'entities' (item_collection) must be provided."
            )
        if has_entities and self.aggregate_pattern != "item_collection":
            raise ValueError("'entities' is only valid when aggregate_pattern='item_collection'.")
        return self


# ---------------------------------------------------------------------------
# Access pattern — the atomic unit
# ---------------------------------------------------------------------------


class AccessPattern(BaseModel):
    """
    Single access pattern derived from a source query. This is the atomic unit
    of the contract — table definitions exist to back these patterns.
    """

    pattern_id: str = Field(
        ...,
        description="Stable identifier for this access pattern, prefixed with engine shorthand, e.g. 'DDB-AP-1', 'DDB-AP-2'",
    )
    pattern_group: str = Field(
        ...,
        description="Human-readable group label for UI consolidation, e.g. 'Discussion CRUD', "
        "'Post reads', 'User lookups'. Patterns with the same group are displayed "
        "together in the UI. Group by entity + operation type.",
    )
    analysis_pattern_ids: list[str] = Field(
        default_factory=list,
        description="pattern_ids from AgentAnalysisInput.patterns_detected that informed this design decision",
    )
    query_ids: list[str] = Field(
        ...,
        min_length=1,
        description="query_ids from collector.json that map to this access pattern",
    )
    source_tables: list[str] = Field(
        ..., description="MySQL table_ids accessed by the original queries"
    )
    description: str = Field(..., description="Plain-English description of what this pattern does")
    operation: DYNAMODB_OPERATIONS = Field(..., description="DynamoDB API operation")
    table_name: str = Field(..., description="DynamoDB table this pattern executes against")
    gsi_name: str | None = Field(None, description="GSI name if this pattern uses a GSI")
    key_condition: str = Field(
        ...,
        description="Key condition expression, e.g. 'PK=discussion_id AND SK begins_with POST#'. "
        "For writes, describe the item being written.",
    )
    design_rps: float = Field(
        ..., ge=0, description="calls_per_second × peak_to_avg_ratio × growth_multiplier"
    )
    avg_items_returned: float | None = Field(
        None, ge=0, description="Average items per request. None for writes."
    )
    item_size_bytes: int = Field(..., ge=1)
    strongly_consistent: bool = Field(default=False)
    returned_attributes: list[str] | None = Field(
        None,
        description="For a read, the attribute names it returns, from the source query's select "
        "list: [] when it returns only key attributes. The cost check charges a base-table fetch "
        "per returned item when the pattern's GSI does not project one of them (KEYS_ONLY with "
        "any non-key attribute, INCLUDE missing one, never ALL). Null for writes; a read with "
        "null is taken to return the full item on a KEYS_ONLY index (designs before #1509).",
    )
    condition_expression: str | None = Field(
        None,
        description="The DynamoDB ConditionExpression this write carries, e.g. "
        "'attribute_not_exists(email)' for a uniqueness guard or 'version = :expected' for "
        "optimistic locking. Set it on every conditional PutItem, UpdateItem, DeleteItem or "
        "TransactWriteItems, even when key_condition or description already says it in words; "
        "null for an unconditional write and for reads. The group-draft merge never replaces "
        "the table key a conditional write depends on.",
    )
    in_scope: bool = Field(default=True)
    out_of_scope_reason: str | None = Field(None, description="Required when in_scope=False.")


# ---------------------------------------------------------------------------
# Supporting sections
# ---------------------------------------------------------------------------


class UnsupportedPattern(BaseModel):
    query_ids: list[str] = Field(..., min_length=1)
    pattern_type: UNSUPPORTED_PATTERN_TYPES
    recommendation: str = Field(
        ..., description="e.g. 'requires OpenSearch for LIKE full-text search'"
    )


class MigrationNote(BaseModel):
    object_name: str
    object_type: STORED_OBJECT_TYPES
    source_table: str | None = None
    application_logic_required: str


class HotPartitionEntry(BaseModel):
    table_name: str
    gsi_name: str | None = None
    operation: PARTITION_OPERATIONS
    rcu_or_wcu_per_second: float = Field(..., ge=0)
    partition_limit: float = Field(..., description="3000 for reads, 1000 for writes")
    utilization_pct: float = Field(..., ge=0, le=100)
    at_risk: bool = Field(..., description="True when utilization_pct > 80")
    contributing_patterns: list[str] = Field(
        ..., description="query_ids driving this partition's load"
    )
    mitigation: str | None = Field(None, description="Required when at_risk=True")

    @model_validator(mode="after")
    def validate_mitigation_when_at_risk(self) -> "HotPartitionEntry":
        if self.at_risk and not self.mitigation:
            raise ValueError("mitigation is required when at_risk=True")
        return self


# ---------------------------------------------------------------------------
# Root contract
# ---------------------------------------------------------------------------


class UnresolvedMergeConflict(BaseModel):
    """One DynamoDB merge failure and the access patterns it leaves unserved (#802)."""

    failure: str = Field(..., description="The merge failure, as in validation_failures")
    status: Literal["unserved", "at_risk", "none_named", "not_recorded"] = Field(
        ...,
        description=(
            "What pattern_ids are: unserved (the merged design runs them on a table or "
            "GSI not designed for them), at_risk (they run on a table the failure "
            "concerns, which cannot be built as designed until it is resolved), "
            "none_named (the failure names no access pattern), or not_recorded (a "
            "design merged before the merge recorded them)"
        ),
    )
    table_name: str | None = Field(
        None,
        description=(
            "The merged table the unserved access patterns run on (the kept design of a "
            "table two groups designed differently); None when the failure leaves no "
            "pattern unserved"
        ),
    )
    pattern_ids: list[str] = Field(
        default_factory=list,
        description="The access patterns the merged design does not serve (its own IDs)",
    )
    query_ids: list[str] = Field(
        default_factory=list, description="The queries those access patterns serve"
    )

    model_config = ConfigDict(extra="ignore")


class DynamoDBTradeOff(TradeOff):
    """A DynamoDB design trade-off. The group merge's overlap review (``DynamoDB merge
    review: ``, #333) also records the choice it made (#1780), so every reader states
    the home the merge chose and never ranks the tables again."""

    authoritative_home: str | None = Field(
        None,
        description=(
            "Set by the group merge on its overlap review only: the target table that "
            "takes every write to source_tables. None on any other trade-off, and on a "
            "review merged before the field"
        ),
    )
    derived_copies: list[str] | None = Field(
        None,
        description=(
            "Set by the group merge on its overlap review only: the other target tables, "
            "kept in sync from the authoritative home's DynamoDB Streams"
        ),
    )


class DynamoDBModelOutputContract(BaseModel):
    """
    Output contract for the DynamoDB data modeling agent.

    Access patterns are the atomic unit — table definitions exist to support them.
    Table definitions carry full attribute-level ETL lineage (join, calculation,
    transformation specs) so a migration pipeline can populate every DynamoDB
    attribute directly from MySQL without additional design work.
    """

    contract_version: str = Field(
        default="1.6",
        pattern=r"^\d+\.\d+$",
        description="Contract version (MAJOR.MINOR format)",
    )
    job_id: str = Field(..., description="job_id from collector output — end-to-end traceability")
    source_database: str = Field(..., description="database_name from collector metadata")
    target_engine: str = Field(default="dynamodb", description="Target engine identifier")

    # Primary — access patterns are the atomic unit
    access_patterns: list[AccessPattern] = Field(
        ...,
        min_length=1,
        description="All in-scope access patterns. One per query_type != OTHER from collector.json.",
    )

    # Secondary — table schemas with full ETL lineage
    table_definitions: list[TableDefinition] = Field(
        ...,
        min_length=1,
        description="One entry per DynamoDB table. Multiple MySQL source tables may be consolidated here.",
    )

    # Out-of-scope queries
    unsupported_patterns: list[UnsupportedPattern] = Field(
        default_factory=list,
        description="Queries excluded from DynamoDB scope (text search, aggregations).",
    )

    # Stored object migration notes
    migration_notes: list[MigrationNote] = Field(
        default_factory=list,
        description="Stored procedures, views, and triggers that must become application logic.",
    )

    # Capacity analysis
    hot_partition_analysis: list[HotPartitionEntry] = Field(
        ..., description="Capacity analysis per table/GSI at design RPS."
    )

    trade_offs: list[DynamoDBTradeOff] = Field(
        ..., min_length=1, description="Design trade-off decisions"
    )
    validation_passed: bool = Field(..., description="Whether all validation checks passed")
    validation_failures: list[str] = Field(
        default_factory=list, description="Validation check failures when validation_passed=false"
    )
    unresolved_merge_conflicts: list[UnresolvedMergeConflict] = Field(
        default_factory=list,
        description=(
            "The group merge's failures (#802), each with the access patterns and queries "
            "the merged design leaves unserved; empty for a clean merge, and in a design "
            "written before the field"
        ),
    )

    model_config = ConfigDict(extra="ignore")
