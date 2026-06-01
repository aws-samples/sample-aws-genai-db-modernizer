"""
Schema Revision Loop — Contract Models

Data models for customer-driven schema revision requests, verification results,
changelog tracking, and schema version metadata for the revision loop feature.

Version History:
- 1.0 (2026-05-01): Initial version — schema design revision loop contracts
"""

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class PatternAction(str, Enum):
    """Action to apply to an existing access pattern during schema revision."""

    DROP = "DROP"
    NOTE = "NOTE"
    REASSIGN = "REASSIGN"


class PatternModification(BaseModel):
    """Modification instruction for an existing access pattern."""

    pattern_id: str = Field(..., description="Identifier of the access pattern to modify")
    action: PatternAction = Field(
        ...,
        description="Action to apply: DROP removes it, NOTE annotates it, REASSIGN moves it to another engine",
    )
    note: str | None = Field(
        None,
        description="Annotation text (used with NOTE action, or as context for any action)",
    )
    target_engine: str | None = Field(
        None,
        description="Target engine for REASSIGN action (e.g. opensearch, dynamodb)",
    )


class TableModification(BaseModel):
    """Modification instruction for a source table."""

    table_id: str = Field(..., description="Identifier of the source table to modify")
    action: Literal["drop"] = Field(
        ..., description="Action to apply to the table (currently only 'drop' is supported)"
    )


class NewPattern(BaseModel):
    """A new access pattern to be added during schema revision."""

    description: str = Field(
        ..., description="Human-readable description of what this pattern does"
    )
    target_engine: str = Field(
        ...,
        description="Target engine this pattern should be designed for (e.g. dynamodb, opensearch)",
    )
    source_tables: list[str] = Field(
        ..., description="Source tables this pattern reads from or writes to"
    )
    estimated_reads_per_second: float | None = Field(
        None,
        description="Estimated read throughput for capacity planning (optional)",
    )
    estimated_writes_per_second: float | None = Field(
        None,
        description="Estimated write throughput for capacity planning (optional)",
    )
    context: str | None = Field(
        None,
        description="Additional context about this pattern to guide schema redesign",
    )


class SchemaRevisionRequest(BaseModel):
    """Customer-provided revision request against a specific schema version."""

    base_version: int = Field(..., description="Schema version number this revision is based on")
    pattern_modifications: list[PatternModification] = Field(
        ..., description="Modifications to apply to existing access patterns"
    )
    table_modifications: list[TableModification] = Field(
        ..., description="Modifications to apply to source tables"
    )
    new_patterns: list[NewPattern] = Field(
        ..., description="New access patterns to add to the schema design"
    )


class VerificationIssue(BaseModel):
    """A single issue found during schema revision verification."""

    category: Literal["coverage", "consistency", "conflict", "cost"] = Field(
        ...,
        description=(
            "Category of issue: coverage (uncovered tables/patterns), "
            "consistency (engine mismatch), conflict (incompatible patterns), "
            "cost (budget impact)"
        ),
    )
    severity: Literal["error", "warning"] = Field(
        ...,
        description="Severity: error blocks acceptance, warning is informational",
    )
    message: str = Field(..., description="Human-readable description of the issue")
    affected_patterns: list[str] = Field(
        ..., description="Access pattern IDs affected by this issue"
    )
    affected_tables: list[str] = Field(..., description="Table IDs affected by this issue")
    cost_delta: float | None = Field(
        None,
        description="Estimated monthly cost change in USD (positive = increase, negative = decrease)",
    )
    suggested_resolutions: list[str] = Field(
        default_factory=list,
        description="Suggested actions to resolve this issue",
    )


class VerificationResult(BaseModel):
    """Result of verifying a schema revision request."""

    passed: bool = Field(
        ...,
        description="True when there are no hard errors blocking the revision",
    )
    hard_errors: list[VerificationIssue] = Field(
        ..., description="Errors that must be resolved before the revision can proceed"
    )
    warnings: list[VerificationIssue] = Field(
        ..., description="Non-blocking issues the customer should be aware of"
    )


class ChangelogEntry(BaseModel):
    """A single entry in the schema version changelog."""

    change_type: Literal["added", "removed", "modified", "reassigned"] = Field(
        ..., description="Nature of the change applied"
    )
    entity_type: Literal["access_pattern", "table", "index", "collection"] = Field(
        ..., description="Type of entity that was changed"
    )
    entity_id: str = Field(..., description="Identifier of the changed entity")
    description: str = Field(..., description="Human-readable description of what changed and why")
    from_engine: str | None = Field(
        None,
        description="Previous engine (set for reassigned access patterns)",
    )
    to_engine: str | None = Field(
        None,
        description="New engine (set for reassigned access patterns)",
    )


class SchemaVersionMeta(BaseModel):
    """Metadata for a single schema version, tracking its origin and changes."""

    version: int = Field(..., description="Monotonically increasing schema version number")
    base_version: int | None = Field(
        None,
        description="Version this was derived from (None for the initial system-generated version)",
    )
    initiated_by: Literal["system", "customer"] = Field(
        ...,
        description="Who triggered this version: system (auto-generated) or customer (revision request)",
    )
    timestamp: datetime = Field(..., description="ISO 8601 timestamp when this version was created")
    modifications: SchemaRevisionRequest | None = Field(
        None,
        description="The revision request that produced this version (None for system-generated versions)",
    )
    redesigned_groups: list[str] = Field(
        ...,
        description="Engine groups that were re-run during this revision (e.g. ['dynamodb', 'opensearch'])",
    )
    verification: VerificationResult = Field(
        ..., description="Verification result produced alongside this schema version"
    )
    changelog: list[ChangelogEntry] = Field(
        ..., description="List of changes applied relative to the base version"
    )


class SchemaConfirmation(BaseModel):
    """Customer confirmation of a schema version for a specific engine."""

    confirmed_version: int = Field(
        ..., description="Schema version number the customer is confirming"
    )
    confirmed_at: datetime = Field(
        ..., description="ISO 8601 timestamp when the confirmation was submitted"
    )
    engine: str = Field(
        ..., description="Engine whose schema design is being confirmed (e.g. dynamodb, opensearch)"
    )
