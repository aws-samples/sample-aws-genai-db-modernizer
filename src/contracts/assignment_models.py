"""
Assignment Contract Models

Data models for query-level engine assignment: assignment status tracking,
query assignments, table assignments, versioned assignment artifacts,
and validation results.

Version History:
- 1.0 (2026-04-01): Initial version — Phase 1A assignment models
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class AssignmentStatus(str, Enum):
    """Status of an assignment artifact indicating its origin."""

    AUTO_GENERATED = "auto_generated"
    CUSTOMER_APPROVED = "customer_approved"
    CUSTOMER_MODIFIED = "customer_modified"


class QueryAssignment(BaseModel):
    """Single query-to-engine assignment with confidence and scope metadata."""

    query_id: str = Field(..., description="Unique query identifier from collector output")
    assigned_engine: str = Field(..., description="Target engine this query is assigned to")
    confidence: int = Field(
        ...,
        ge=0,
        le=100,
        description="Assignment confidence score (0-100 integer scale)",
    )
    source_tables: list[str] = Field(..., description="Tables this query accesses")
    assignment_reason: str = Field(..., description="Why this engine was chosen for the query")
    in_scope: bool = Field(
        default=True,
        description="False when customer excluded this query from current iteration",
    )
    customer_override: bool = Field(
        default=False,
        description="True if customer changed the assignment",
    )
    warnings: list[str] = Field(
        default_factory=list,
        description="Warnings associated with this query assignment",
    )


class TableAssignment(BaseModel):
    """Derived table-to-engine assignment.

    A table can span multiple engines when different queries against the
    same table have different access patterns. The primary_engine is the
    engine with the most assigned queries for this table.
    """

    table_id: str = Field(..., description="Table identifier from collector output")
    primary_engine: str = Field(
        ..., description="Engine with the most assigned queries for this table"
    )
    engines: list[str] = Field(
        ..., description="All engines that have queries referencing this table"
    )
    query_count: int = Field(
        ..., ge=0, description="Total number of queries referencing this table"
    )
    multi_engine_reason: str | None = Field(
        None,
        description="Reason this table spans multiple engines (set when engines has 2+ entries)",
    )


class Assignment(BaseModel):
    """Complete versioned assignment artifact."""

    job_id: str = Field(..., description="Unique job identifier")
    version: int = Field(..., ge=1, description="Monotonically increasing version number")
    status: AssignmentStatus = Field(..., description="Origin status of this assignment")
    timestamp: datetime = Field(
        ..., description="ISO 8601 timestamp when this assignment was created"
    )
    query_assignments: list[QueryAssignment] = Field(
        ..., description="Per-query engine assignments"
    )
    table_assignments: list[TableAssignment] = Field(
        ..., description="Derived per-table engine assignments"
    )
    co_dependency_groups: list[list[str]] = Field(
        ...,
        description="Groups of query IDs sharing significant JOIN relationships",
    )
    validation_warnings: list[str] = Field(..., description="Warnings from assignment validation")
    previous_version: int | None = Field(
        None,
        description="Version number of the previous assignment (None for first version)",
    )


class ValidationResult(BaseModel):
    """Result of validating an assignment."""

    valid: bool = Field(..., description="Whether the assignment passed validation")
    warnings: list[str] = Field(
        default_factory=list,
        description="Validation warnings (assignment may still be accepted)",
    )
    errors: list[str] = Field(
        default_factory=list,
        description="Validation errors (assignment is rejected)",
    )
