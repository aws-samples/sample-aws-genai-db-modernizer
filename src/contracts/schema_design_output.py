# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Schema Design Output — Base Contract

Shared envelope fields for all engine-specific schema design output contracts.
Engine-specific contracts (DynamoDB, DocumentDB, ElastiCache, etc.) extend this
base with their own structural models.

The referee/synthesis step reads the common fields (validation_passed, trade_offs)
without needing to understand engine-specific internals.

Version History:
- 1.0 (2026-03-23): Initial version
- 1.1 (2026-11-04, #1221): Added optional ``TradeOff.topic`` from the closed set
  ``TradeOffTopic`` (default ``other``), the pipeline-only ``TradeOff.note_kind``
  (``TradeOffNoteKind``: ``review``, ``warning``; left out of the JSON schema) and
  ``TradeOffGroupTopic``, the topics plus the ``warning`` and ``review_note`` groups the
  deliverables list.
"""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic.json_schema import SkipJsonSchema


def null_lists_as_empty(model: type[BaseModel], data: Any) -> Any:
    """``data`` with an explicit ``null`` for each of ``model``'s optional lists as ``[]`` (#1675).

    A designer model answering through the structured-output tool sometimes writes
    ``null`` for a list it has nothing to put in (a TTL-only cache invalidation with
    no write queries), next to fields of the same objects that are nullable. A list
    field that defaults to empty means the same thing by ``null``, so it reads as
    empty instead of failing validation and costing a whole re-asked turn. Required
    lists, and lists typed ``list | None``, are unchanged; so is the JSON schema. A
    model opts in with a ``mode="before"`` model validator that calls this.
    """
    if not isinstance(data, dict):
        return data
    nulled = [
        key
        for name, field in model.model_fields.items()
        if field.default_factory is list
        for key in {name, field.alias or name}
        if key in data and data[key] is None
    ]
    return {**data, **dict.fromkeys(nulled, [])} if nulled else data


# The topics a schema designer files a design trade-off under (#1221); labels and
# grouping live in ``src/shared/trade_off_topics.py``.
TradeOffTopic = Literal[
    "item_size",
    "id_generation",
    "indexing",
    "key_design",
    "query_translation",
    "consistency",
    "uniqueness",
    "data_types",
    "capacity",
    "cost",
    "operational",
    "other",
]

# What the pipeline itself appended as a trade-off (``TradeOff.note_kind``): the design
# review's notes and the schema agents' own warnings. Never a designer's.
TradeOffNoteKind = Literal["review", "warning"]

# The groups the deliverables list (``facts.trade_off_topics[].topic``): each designer
# topic, then the pipeline's warnings and the design review notes.
TradeOffGroupTopic = Literal[TradeOffTopic, "warning", "review_note"]

TRADE_OFF_TOPIC_GUIDE = (
    "The one topic this trade-off is about, so repeated ones are listed together: "
    "item_size (item, document or value size limits, large columns kept inline or "
    "offloaded); id_generation (ids the database generated, such as AUTO_INCREMENT, that "
    "the application now generates); indexing (secondary indexes added, dropped or not "
    "created, their projections and write cost); key_design (partition and sort keys, "
    "item collections, embedding, denormalization, table or index layout); "
    "query_translation (a source query served another way: joins, counts, IN lists, "
    "ordering or filters done by the application or several requests); consistency "
    "(eventual consistency, staleness, TTLs, cache invalidation, transactions and "
    "atomicity); uniqueness (unique constraints, conditional writes, sentinel items); "
    "data_types (type mappings and value formats); capacity (hot partitions or keys, "
    "throughput limits, shards, memory); cost (billed units and their price); "
    "operational (running it: engine versions, monitoring, sync pipelines, index "
    "builds, routing to replicas); other (none of these)."
)


class TradeOff(BaseModel):
    """A structured trade-off from schema design.

    Links a design decision to the specific tables and queries it affects,
    so the UI can show trade-offs inline with the access patterns they relate to.
    """

    description: str = Field(..., description="What changed and why")
    impact: str = Field(
        ...,
        description=(
            "What this means in practice for a team coming from a relational database. "
            "Written for a CTO, not a database engineer."
        ),
    )
    source_tables: list[str] = Field(
        default_factory=list, description="Source tables affected (e.g. wp_posts, wp_postmeta)"
    )
    target_tables: list[str] = Field(
        default_factory=list,
        description="Target tables/indexes involved (e.g. WpPosts, posts-index)",
    )
    query_ids: list[str] = Field(
        default_factory=list, description="Query IDs affected (links to assignment query_id)"
    )
    engine: str = Field(default="", description="Target engine (dynamodb, opensearch, etc.)")
    # Null among the values, read as other, so the Bedrock tool schema lists null in the
    # enum instead of a nullable-typed enum, which Bedrock refuses (#1281).
    topic: Literal[TradeOffTopic, None] = Field(default="other", description=TRADE_OFF_TOPIC_GUIDE)
    # Set only by the pipeline when it appends a note of its own (a design review note,
    # a schema agent's warning). Left out of the JSON schema, so no designer is offered
    # it; ``finalize_schema_design`` refuses a designer's response that sets it.
    note_kind: SkipJsonSchema[TradeOffNoteKind | None] = Field(
        default=None, description="What the pipeline appended this as; None for a design's own"
    )

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _null_lists(cls, data: Any) -> Any:
        return null_lists_as_empty(cls, data)

    @field_validator("topic", mode="before")
    @classmethod
    def _null_topic_is_other(cls, v: object) -> object:
        # A null topic reads as the default, like an absent one (#1221).
        return "other" if v is None else v


class SchemaDesignOutputBase(BaseModel):
    """Base envelope for all schema design output contracts."""

    contract_version: str = Field(
        ...,
        pattern=r"^\d+\.\d+$",
        description="Contract version (MAJOR.MINOR format)",
    )
    job_id: str = Field(..., description="job_id from collector output — end-to-end traceability")
    source_database: str = Field(..., description="database_name from collector metadata")
    target_engine: str = Field(
        ..., description="Target engine identifier (dynamodb, documentdb, etc.)"
    )

    trade_offs: list[TradeOff] = Field(..., min_length=1)
    validation_passed: bool
    validation_failures: list[str] = Field(default_factory=list)

    model_config = ConfigDict(extra="ignore")
