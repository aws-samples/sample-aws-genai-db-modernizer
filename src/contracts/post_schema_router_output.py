"""Post-Schema Router Output Contract.

Defines the output of the deterministic post-schema router: queries a model
flagged as unsupported on their engine (a PE reviewer's routing flag or a
designer's unsupported pattern), each re-routed by the shared scorer to the best
remaining engine (#629). The destination is always the scorer's choice.

Each routing also carries what its Move records (#616, #489 Track L PR4): the
runner-up, the evidence (candidate ladder, tie rule, a group's mean scores)
and a co-dependency group's key, and the tie the move leaves (#811). The
orchestrator writes the routings as ``post_schema_flag`` Moves
(``move_ledger.apply_to_assignment``); these fields are optional, so a router
output written before them still loads.

Version History:
- 1.0 (2026-10-17, #972): First versioned: ``RouterOutput``
  (``router_output.json``) carries ``contract_version`` (ADR-008 Decision 1).
  A file written before has none, and still loads.
- 1.1 (2026-10-17, #992): Added ``held_queries``: flagged queries the router kept
  in place because their table's write group holds a customer-pinned write (a
  table has one writer, ADR-031 Amendment 9); the flag stands as a note on the
  unit, not a move. Defaults to empty, so a 1.0 file still loads.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from src.contracts.move_ledger import EngineScore, MoveEvidence


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
    runner_up: EngineScore | None = Field(
        None, description="The best remaining engine the scorer did not pick, for the Move"
    )
    evidence: MoveEvidence | None = Field(
        None,
        description=(
            "What the router saw, for the Move: the query's candidate ladder, the tie "
            "rule when an exact tie picked to_engine, a co-dependency group's mean scores"
        ),
    )
    group_key: str | None = Field(
        None,
        description="The co-dependency group the query moved with (move_ledger.group_key)",
    )
    tied_engines: list[str] = Field(
        default_factory=list,
        description=(
            "The tie the move leaves (sorted): the query's tie minus the flagged engine "
            "when the scorer leaves it unresolved, empty when it is settled (#811)"
        ),
    )


class RouterOutput(BaseModel):
    """Output contract for the post-schema router."""

    contract_version: str = Field(
        default="1.1", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
    )

    job_id: str = Field(..., description="Job ID for traceability")
    routings: list[QueryRouting] = Field(
        default_factory=list, description="Queries being routed to a different engine"
    )
    terminal_queries: list[str] = Field(
        default_factory=list,
        description="Query IDs that no engine can serve — application-layer only",
    )
    held_queries: list[str] = Field(
        default_factory=list,
        description=(
            "Flagged queries kept on their engine because their table's write group holds "
            "a customer-pinned write (#992): the flag is a note on the unit, not a move"
        ),
    )
    cascade_depth: int = Field(default=0, description="Current depth of the routing cascade")
