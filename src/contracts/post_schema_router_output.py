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
- 1.2 (2026-10-18, #1003): No field of its own. The embedded ``MoveEvidence``
  (move ledger 1.8) gained the optional ``choice_id``, ``choice_scores``,
  ``choice_unrounded_scores`` and ``choice_rule``, and ``tie_rule`` the value
  ``tie.decided_for_you``. A 1.1 file still loads.
- 1.3 (2026-10-18, #888): ``RouterOutput.terminal`` (``TerminalQuery``): each
  terminal query with the flag that made it terminal (the flagged engine and
  the reason category) and why it is terminal (no kept engine can take it, or
  the cascade hit its depth limit), so synthesis can flag it in every
  deliverable. ``terminal_queries`` keeps the ids. Backward compatible:
  ``terminal`` defaults to empty.
- 1.4 (2026-10-18, #1033): ``RouterOutput.held`` (``HeldQuery``): each held query
  with the flag it was held under (the flagged engine and the reason category), so
  synthesis can state it in every deliverable. ``held_queries`` keeps the ids.
  Backward compatible: ``held`` defaults to empty.
"""

from __future__ import annotations

from typing import Literal

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


class TerminalQuery(BaseModel):
    """A flagged query the router could not move (#888): it stays on its engine."""

    query_id: str = Field(..., description="The flagged query")
    flagged_engine: str = Field(..., description="The engine the flag judged unable (id)")
    reason_category: str | None = Field(
        None, description="The flag's reason category (ModelFlag.reason_category)"
    )
    cause: Literal["no_kept_engine", "depth_limit"] = Field(
        ...,
        description=(
            "no_kept_engine: no other kept engine can take it; depth_limit: the routing "
            "cascade reached its last pass"
        ),
    )


class HeldQuery(BaseModel):
    """A flagged query kept on its engine by a customer pin (#1033)."""

    query_id: str = Field(..., description="The flagged query")
    flagged_engine: str = Field(..., description="The engine the flag judged unable (id)")
    reason_category: str | None = Field(
        None, description="The flag's reason category (ModelFlag.reason_category)"
    )


class RouterOutput(BaseModel):
    """Output contract for the post-schema router."""

    contract_version: str = Field(
        default="1.4", pattern=r"^\d+\.\d+$", description="Contract version (MAJOR.MINOR format)"
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
    held: list[HeldQuery] = Field(
        default_factory=list,
        description="Each held query with the flag it was held under (#1033)",
    )
    terminal: list[TerminalQuery] = Field(
        default_factory=list,
        description="Each terminal query with the flag that made it terminal (#888)",
    )
    cascade_depth: int = Field(default=0, description="Current depth of the routing cascade")
