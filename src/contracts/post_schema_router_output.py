"""Post-Schema Router Output Contract.

Defines the output of the deterministic post-schema router that reads
PE-confirmed unsupported patterns and routes them to the next-best engine.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class QueryRouting(BaseModel):
    """A single query routing decision from the post-schema router."""

    query_id: str = Field(..., description="Source query ID being rerouted")
    from_engine: str = Field(..., description="Engine that declared this query unsupported")
    to_engine: str | None = Field(
        None,
        description="Target engine to receive this query. None means application-layer handling.",
    )
    reason: str = Field(..., description="Why this query cannot be served by from_engine")
    cascade_depth: int = Field(
        default=0, description="How many routing passes this query has been through"
    )


class RouterOutput(BaseModel):
    """Output contract for the post-schema router."""

    job_id: str = Field(..., description="Job ID for traceability")
    routings: list[QueryRouting] = Field(
        default_factory=list, description="Queries being routed to a different engine"
    )
    terminal_queries: list[str] = Field(
        default_factory=list,
        description="Query IDs that no engine can serve — application-layer only",
    )
    cascade_depth: int = Field(default=0, description="Current depth of the routing cascade")
