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
"""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


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

    model_config = ConfigDict(extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _null_lists(cls, data: Any) -> Any:
        return null_lists_as_empty(cls, data)


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
