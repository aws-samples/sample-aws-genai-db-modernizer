"""Post-Schema Router Output Contract.

Defines the output of the deterministic post-schema router: queries a model
flagged as unsupported on their engine (a PE reviewer's routing flag or a
designer's unsupported pattern), each re-routed by the shared scorer to the best
remaining engine (#629). The destination is always the scorer's choice.
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
    reason_category: str | None = Field(
        None, description="The flag's reason category (ModelFlag.reason_category)"
    )
    explanation: str | None = Field(
        None, description="The flag's explanation, in the model's words (untrusted text)"
    )
    score_before: float | None = Field(
        None, description="from_engine's own score for the query, ignoring the flag"
    )
    score_after: float | None = Field(None, description="to_engine's score for the query")


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
