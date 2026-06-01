"""DocumentDB schema design output contract.

Defines the structured output that the DocumentDB schema design agent
produces — collection definitions, indexes, embedding decisions,
access patterns translated to MongoDB query language.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .schema_design_output import TradeOff

# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

EMBED_STRATEGY = Literal["embed", "reference", "hybrid"]
INDEX_TYPE = Literal["compound", "single", "text", "partial", "unique"]
DOCDB_OPERATION = Literal[
    "findOne",
    "find",
    "insertOne",
    "insertMany",
    "updateOne",
    "updateMany",
    "deleteOne",
    "deleteMany",
    "aggregate",
    "countDocuments",
]


class EmbeddedEntity(BaseModel):
    """A child table embedded as a sub-document or array in a parent collection."""

    source_table: str = Field(..., description="Source table_id being embedded")
    embed_path: str = Field(..., description="JSON path in parent document, e.g. 'tags', 'items'")
    strategy: EMBED_STRATEGY = Field(
        ..., description="Embedding strategy: embed, reference, hybrid"
    )
    embedded_fields: list[str] | None = Field(
        None, description="For hybrid: which child fields to embed"
    )
    avg_array_length: float = Field(..., ge=0, description="Average array length for embedded docs")
    max_array_length_estimate: float = Field(
        ..., ge=0, description="Estimated max array length for sizing"
    )
    rationale: str = Field(..., description="Why this embedding strategy was chosen")

    model_config = ConfigDict(extra="ignore")


class IndexDefinition(BaseModel):
    """A DocumentDB index definition."""

    index_name: str = Field(..., description="Index name, e.g. idx_users_email")
    keys: dict[str, int] = Field(
        ..., description="Index key spec, e.g. {'user_id': 1, 'created_at': -1}"
    )
    index_type: INDEX_TYPE = Field(
        default="compound", description="Index type: compound, single, text, partial, unique"
    )
    partial_filter: dict | None = Field(
        None, description="Partial filter expression for sparse indexes"
    )
    purpose: str = Field(..., description="Which access pattern(s) this index serves")
    source_query_ids: list[str] = Field(
        default_factory=list, description="Query IDs from collector that need this index"
    )

    model_config = ConfigDict(extra="ignore")


class CollectionDefinition(BaseModel):
    """A single DocumentDB collection design."""

    collection_name: str = Field(..., description="Target DocumentDB collection name")
    source_tables: list[str] = Field(
        ..., min_length=1, description="Source table_ids consolidated into this collection"
    )
    embedded_entities: list[EmbeddedEntity] = Field(
        default_factory=list, description="Child tables embedded in this collection"
    )
    referenced_collections: list[str] = Field(
        default_factory=list,
        description="Other collections this one references (not embedded)",
    )
    indexes: list[IndexDefinition] = Field(
        default_factory=list, description="Index definitions for this collection"
    )
    document_examples: list[dict] = Field(
        ..., min_length=1, max_length=3, description="Example documents showing data shape"
    )
    estimated_avg_doc_size_kb: float = Field(
        ..., ge=0, description="Estimated average document size in KB"
    )
    estimated_max_doc_size_kb: float = Field(
        ..., ge=0, description="Estimated max document size in KB (must be < 16MB)"
    )

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="after")
    def validate_doc_size(self) -> CollectionDefinition:
        if self.estimated_max_doc_size_kb > 16000:
            raise ValueError(
                f"Collection '{self.collection_name}' estimated max doc size "
                f"{self.estimated_max_doc_size_kb:.0f}KB exceeds DocumentDB 16MB limit"
            )
        return self


class AccessPattern(BaseModel):
    """An access pattern translated to DocumentDB query language."""

    pattern_id: str = Field(
        ...,
        description="Stable identifier prefixed with engine shorthand, e.g. 'DOC-AP-1', 'DOC-AP-2'",
    )
    description: str = Field(..., description="Plain-English description of this pattern")
    operation: DOCDB_OPERATION = Field(..., description="DocumentDB operation type")
    collection_name: str = Field(..., description="Target collection for this pattern")
    query_filter: dict = Field(..., description="MongoDB query filter document")
    projection: dict | None = Field(None, description="Field projection")
    sort: dict | None = Field(None, description="Sort specification")
    index_used: str = Field(..., description="Index name that serves this pattern")

    source_query_ids: list[str] = Field(
        ..., min_length=1, description="Source query IDs from collector"
    )
    source_tables: list[str] = Field(
        ..., min_length=1, description="Source table_ids accessed by the original queries"
    )
    design_rps: float = Field(..., ge=0, description="Computed design RPS for capacity planning")
    pipeline: list[dict] | None = Field(
        None, description="Aggregation pipeline stages (for aggregate operations)"
    )
    in_scope: bool = Field(default=True, description="False for patterns DocumentDB cannot serve")
    out_of_scope_reason: str | None = Field(
        None, description="Reason this pattern is out of scope. Required when in_scope=False."
    )

    model_config = ConfigDict(extra="ignore")


class UnsupportedPattern(BaseModel):
    """A source query pattern that cannot be served by DocumentDB."""

    source_query_ids: list[str] = Field(
        ..., min_length=1, description="Source query IDs that are unsupported"
    )
    reason: str = Field(..., description="Why this pattern is unsupported in DocumentDB")
    workaround: str | None = Field(None, description="Suggested alternative approach")

    model_config = ConfigDict(extra="ignore")


class MigrationNote(BaseModel):
    """Stored object requiring application-layer replacement."""

    object_name: str = Field(..., description="Name of the stored object")
    object_type: Literal["view", "procedure", "function", "trigger"] = Field(
        ..., description="Type of stored object"
    )
    source_table: str | None = Field(None, description="Related source table if applicable")
    application_logic_required: str = Field(
        ..., description="Description of application logic needed to replace this object"
    )

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Top-level contract
# ---------------------------------------------------------------------------


class DocumentDBModelOutputContract(BaseModel):
    """Output contract for the DocumentDB schema design agent."""

    contract_version: str = Field(
        default="1.0",
        pattern=r"^\d+\.\d+$",
        description="Contract version (MAJOR.MINOR format)",
    )
    job_id: str = Field(..., description="job_id from collector output")
    source_database: str = Field(..., description="database_name from collector metadata")
    target_engine: str = Field(default="documentdb", description="Target engine identifier")

    collections: list[CollectionDefinition] = Field(
        ..., min_length=1, description="DocumentDB collection designs"
    )
    access_patterns: list[AccessPattern] = Field(
        ..., min_length=1, description="Access patterns translated to DocumentDB operations"
    )
    unsupported_patterns: list[UnsupportedPattern] = Field(
        default_factory=list, description="Patterns that cannot be served by DocumentDB"
    )
    migration_notes: list[MigrationNote] = Field(
        default_factory=list, description="Stored objects requiring application-layer replacement"
    )

    trade_offs: list[TradeOff] = Field(..., min_length=1, description="Design trade-off decisions")
    validation_passed: bool = Field(..., description="Whether all validation checks passed")
    validation_failures: list[str] = Field(
        default_factory=list, description="Validation failures when validation_passed=false"
    )

    model_config = ConfigDict(extra="ignore")
