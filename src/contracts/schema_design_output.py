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

from pydantic import BaseModel, ConfigDict, Field


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
