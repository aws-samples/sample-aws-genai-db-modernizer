"""Move Ledger Contract

One record per change of one query's engine or of its unresolved tie
(``tied_engines``) (#616, ADR-031). Every pass that can
set or change a query's engine records a ``Move`` through one function
(``src.agents.referee.move_ledger.apply``) instead of keeping its own record
format. The first assignment writes at least one Move per query (``from_engine``
is ``None``, the candidate ladder is in ``evidence``); later passes record the
engine the query left, its score there, the score where it lands and the
runner-up.

``Assignment.moves`` (``src.contracts.assignment_models``) carries only the
Moves that produced that version (per-version, never a copy of an earlier
version's), in ``seq`` order. Replaying v1's rebuilds v1's ``assigned_engine``
and ``tied_engines`` exactly (``move_ledger.replay``); v1 plus each later
version's own Moves rebuilds that version.

Scores are the shared scorer's values at the moment of the move
(``query_scoring.score_query_for_engine``, the same number the pass compared).
A score is ``None`` only when the engine was gated or never scored, and the
gate is then in ``evidence``.

Version History:
- 1.0 (2026-10-14, #616): Initial version. Only the first assignment
  (``AssignmentResolver.resolve``) writes Moves; the reality check, customer
  overrides and the post-schema router follow in later changes.
- 1.1 (2026-10-14, #616, #755): The reality check writes Moves too:
  ``settle`` (Phase 1 removed the query's engine), ``place`` (another kept
  engine scores it higher, or only its tie changed), ``floor``, ``model_flag``
  (actor ``model_flag``), ``sweep`` and ``customer_pick`` (the
  customer-override restore, actor ``customer``). Their scores are the ones
  each pass compared and its reason states; a co-dependency group's are the
  group's mean scores (``evidence.group_scores``), and a floor Move's runner-up
  is ranked the way its winner was. ``evidence.candidates`` may also carry the
  scorer's ``model_flag`` gate. One reason-code rule for every reality-check
  pass: an engine change's ``reason_code`` is the pass's own rule (why the
  query left), and when an exact tie picked the destination the tie rule is in
  ``evidence.tie_rule``; a Move that changes only the tie (``from_engine ==
  to_engine``) has the tie's rule as its code (``tie.cleared`` when the tie
  cleared) and the earlier tie in ``evidence.previous_tied_engines``. Added
  ``ReasonCode.TIE_CLEARED`` and the two ``MoveEvidence`` fields (optional,
  so 1.0 records still load). One engine change is not a Move yet: the
  handler's ``normalize_cache_owners`` (an assignment written before #296
  with ElastiCache owners); Track L PR3 records it as ``cache_owner_normalize``.
  The customer gate and the post-schema router follow in later changes.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class MovePass(str, Enum):
    """The routing pass that recorded a Move, each tied to an ADR-031 decision.

    Only some are written today (the first assignment's); the rest are the
    passes the later ledger changes route through ``move_ledger.apply``.
    """

    CANDIDATES = "candidates"  # Decision 2 step 1
    SCORE = "score"  # Decision 2 steps 2-5
    SIGNAL_OVERRIDE = "signal_override"  # Decision 2, listed override (search pinning)
    UTILITY_PIN = "utility_pin"  # Decision 2, listed override (administration statements)
    AURORA_FALLBACK = "aurora_fallback"  # Decision 2, no engine scored the query
    HETEROGENEOUS_CHOICE = "heterogeneous_choice"  # Decision 6
    CO_DEPENDENCY = "co_dependency"  # Decision 4
    TIE = "tie"  # Decisions 2 and 12
    SETTLE = "settle"  # Decision 7
    PLACE = "place"  # Decision 7
    FLOOR = "floor"  # Decision 7
    MODEL_FLAG = "model_flag"  # Decision 11
    RESETTLE = "resettle"  # Decisions 7 and 11
    SWEEP = "sweep"  # Decision 7
    CUSTOMER_PICK = "customer_pick"
    CUSTOMER_GROUP_PROPAGATION = "customer_group_propagation"  # Decision 4
    CACHE_OWNER_NORMALIZE = "cache_owner_normalize"
    POST_SCHEMA_FLAG = "post_schema_flag"  # Decision 11


class ReasonCode(str, Enum):
    """Why a Move landed where it did (also the rule ids of the rule catalog)."""

    HIGHEST_SCORE = "highest_score"
    TIE_SOURCE_COMPATIBLE = "tie.source_compatible"
    TIE_STABILITY = "tie.stability"
    # Decision 12's placeholder, split by which of its two rules decided it.
    TIE_PLACEHOLDER_SETTLED_WORKLOAD = "tie.placeholder.settled_workload"
    TIE_PLACEHOLDER_HASH = "tie.placeholder.hash"
    # A Move that only clears a query's unresolved tie (the engine stays).
    TIE_CLEARED = "tie.cleared"
    GROUP_MEAN = "group.mean"
    OVERRIDE_TEXT_SEARCH = "override.text_search"
    OVERRIDE_UTILITY = "override.utility"
    FALLBACK_NO_SCORE = "fallback.no_score"
    # A co-dependency group with no engine able to own every member: the
    # resolver's "no analysis available" fallback, kept apart from no_score.
    FALLBACK_NO_OWNER = "fallback.no_owner"
    CONSOLIDATE_NO_UNIQUE_VALUE = "consolidate.no_unique_value"
    CONSOLIDATE_SOURCE_COMPATIBLE_SURVIVES = "consolidate.source_compatible_survives"
    FLOOR_OPENSEARCH = "floor.opensearch"
    FLOOR_COST_SHARE = "floor.cost_share"
    SWEEP_ORPHAN = "sweep.orphan"
    FLAG_CAPABILITY_GAP = "flag.capability_gap"
    CUSTOMER_PICK = "customer.pick"
    CACHE_LEGACY_OWNER = "cache.legacy_owner"


class MoveActor(str, Enum):
    """Who caused the Move: a deterministic rule, the customer, or a model's flag."""

    RULE = "rule"
    CUSTOMER = "customer"
    MODEL_FLAG = "model_flag"


class EngineScore(BaseModel):
    """One engine and its score, e.g. the runner-up of a Move."""

    model_config = ConfigDict(extra="forbid")

    engine: str = Field(..., description="Engine id")
    score: float | None = Field(
        None, description="The shared scorer's value; None when gated or never scored"
    )


class CandidateScore(BaseModel):
    """One rung of a query's candidate ladder at the moment of a Move."""

    model_config = ConfigDict(extra="forbid")

    engine: str = Field(..., description="Candidate engine id")
    score: float | None = Field(
        None,
        description=(
            "The score the pass compared (0-100); None when a hard gate excluded the engine"
        ),
    )
    gate: str | None = Field(
        None,
        description=(
            "Why the engine could not take the query: the scorer's gate kind "
            "(exclusion, capability) or write_gate (an engine that is not a system "
            "of record cannot own a write). None when the engine was eligible"
        ),
    )


class MoveEvidence(BaseModel):
    """What the pass saw when it recorded the Move. Every field is optional."""

    model_config = ConfigDict(extra="forbid")

    candidates: list[CandidateScore] = Field(
        default_factory=list,
        description="Every analyzed engine's score or gate for this query, sorted by engine",
    )
    group_scores: dict[str, float] = Field(
        default_factory=dict,
        description="For a co-dependency group: each eligible engine's mean member score",
    )
    signal: str | None = Field(None, description="The triage signal of a signal override")
    replaced_engine: str | None = Field(
        None,
        description=(
            "For an override that replaces an engine this same pass set earlier "
            "(the utility pin over a scored pick): the engine it replaced"
        ),
    )
    tie_rule: ReasonCode | None = Field(
        None,
        description=(
            "For a reality-check engine change whose destination an exact tie "
            "decided: the tie rule (tie.*). None when no tie was involved"
        ),
    )
    previous_tied_engines: list[str] = Field(
        default_factory=list,
        description=(
            "For a Move that changes the query's tie: its tied_engines before the "
            "Move (sorted). Empty otherwise"
        ),
    )


class Move(BaseModel):
    """One engine change of one query (#616).

    ``pass`` is a Python keyword, so the attribute is ``pass_``; it reads and
    writes as ``pass`` in JSON (alias, serialized by alias by default;
    ``serialize_by_alias`` needs pydantic 2.11, the project floor).
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True, serialize_by_alias=True)

    seq: int = Field(..., ge=1, description="Order of the Move within the ledger, from 1")
    query_id: str = Field(..., description="The query that moved")
    pass_: MovePass = Field(
        ...,
        alias="pass",
        description="The routing pass that recorded the Move",
    )
    from_engine: str | None = Field(
        None, description="The engine the query left; None for its first assignment"
    )
    to_engine: str = Field(..., description="The engine the query landed on")
    score_before: float | None = Field(
        None, description="The query's score on from_engine; None for a first assignment"
    )
    score_after: float | None = Field(
        None, description="The query's score on to_engine; None when gated or never scored"
    )
    runner_up: EngineScore | None = Field(
        None, description="The best engine the pass did not pick, if any"
    )
    reason_code: ReasonCode = Field(..., description="The rule that decided the Move")
    evidence: MoveEvidence = Field(
        default_factory=MoveEvidence, description="What the pass saw (candidate ladder, ...)"
    )
    group_key: str | None = Field(
        None,
        description=(
            "The co-dependency group the query moved with: a 12-hex key hashed from "
            "the sorted member query ids. None for a query that moved alone"
        ),
    )
    tied_engines: list[str] = Field(
        default_factory=list,
        description=(
            "The engines an exact tie left unresolved (sorted); to_engine is then a "
            "placeholder pending the customer's choice. Empty otherwise"
        ),
    )
    actor: MoveActor = Field(MoveActor.RULE, description="Who caused the Move")
