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

Reserved pass names (#616): three ``MovePass`` values are never written by any
code and are kept only because removing an enum value is a MAJOR contract
change (a removal proposal is filed separately). ``candidates``: the candidate
step is recorded on every first Move's ``evidence.candidates``, not as a Move
of its own. ``heterogeneous_choice``: the Aurora engine choice (ADR-031
Decision 6) moves no query; it is recorded on
``Assignment.aurora_engine_choice``. ``resettle``: the post-correction
re-settle it named was deleted (#616), since a model flag never re-settles
(ADR-031 Amendment 2). No writer may record a Move with these passes.

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
- 1.2 (2026-10-14, #616, #786): No schema change. The remaining writers
  record Moves, and ``src.agents.referee.move_ledger`` is the only module
  that sets ``assigned_engine`` (ADR-031 Amendment 3, an AST guard). The
  cache owner normalisation writes ``cache_owner_normalize`` /
  ``cache.legacy_owner`` (the owner's shared-scorer score, runner-up,
  candidate ladder, and the tie rule in ``evidence.tie_rule`` when an exact
  tie picked it), in the reality check's version (first, so it replays from
  the stored input), in the customer gate's version, and appended in memory
  to a version synthesis or the edit surface reads. The customer gate writes
  ``customer_pick`` (rule ``customer.pick``, actor ``customer``, the shared
  scorer's score on both engines) for an engine change or a settled tie, and
  ``customer_group_propagation`` (the group's ``group_key``) for each
  co-dependent query moved with a pick. A cache owner
  picked because the customer asked to cache a legacy owner is
  ``cache_owner_normalize`` with actor ``customer``. The post-schema router
  is the one writer left (#489 Track L PR4).
- 1.3 (2026-10-14, #616): Added ``ReasonCode.GROUP_CUSTOMER_PROPAGATION``
  (``group.customer_propagation``), the rule of a ``customer_group_propagation``
  Move: the query moved with a co-dependent query the customer re-routed, and
  was not picked by the customer (additive; 1.2 records still load). Customer
  Moves (``customer_pick``, ``customer_group_propagation``) also carry the
  runner-up: the best-scoring other engine that may own the query. ``tied_engines``
  is owned by the ledger too: ``MoveLedger.tied`` holds each query's current
  tie, and the first assignment builds v1's ties from it.
- 1.4 (2026-10-14, #616, #770): Added ``ReasonCode.ROLLBACK_REDESIGN_FAILED``
  (``rollback.redesign_failed``; additive, 1.3 records still load). The
  post-schema router (#489 Track L PR4) records its Moves, so no writer of
  ``assigned_engine`` or ``tied_engines`` is left outside the ledger. A
  reroute version holds one ``post_schema_flag`` Move per moved query (rule
  ``flag.capability_gap``, actor ``model_flag``) with the same fields the
  reality check's ``model_flag`` Move carries: the flagged engine's own score
  ignoring the flag, the destination's score, the runner-up, the candidate
  ladder (the flagged engine with its ``model_flag`` gate), the tie rule when
  an exact tie picked the destination, and a co-dependency group's
  ``group_key`` and mean scores. A rollback version (the redesign on the
  reroute failed, so it restates the version before it) holds the reverse
  Moves, built from the reroute version's own Moves in reverse ``seq``
  order: pass ``post_schema_flag``, rule ``rollback.redesign_failed``, actor
  ``rule``, back to the engine and tie the query had, with the undone Move's
  ``group_key``. Its ``score_before`` is the undone Move's ``score_after``;
  its ``score_after`` is ``None`` because the flag still gates the engine it
  lands on (the gate is in ``evidence.candidates``, #770 option (a)).
- 1.5 (2026-10-15, #787): Added ``GroupGate``, ``MoveEvidence.group_gates``
  (default empty) and ``ReasonCode.FALLBACK_NO_GROUP_OWNER``
  (``fallback.no_group_owner``); additive, 1.4 records still load. A
  co-dependency group that no analyzed engine can serve (every engine is gated
  for at least one member: the capability or hard-exclusion gate, or the
  ownership gate) takes the Aurora fallback with rule
  ``fallback.no_group_owner`` (it was ``fallback.no_owner``, which now means
  only a single query with no owner candidate, on the cache owner
  normalisation); each member's Move now lists,
  per analyzed engine, the first member (by query id) that ruled it out and
  the gate kind, so a reader can say why the group fell back even for a
  member with real, ungated scores of its own.
- 1.6 (2026-10-17, #976): ``Move``, ``MoveEvidence``, ``EngineScore``,
  ``CandidateScore`` and ``GroupGate``: Unknown fields are accepted and kept
  (``extra="allow"``, was ``"forbid"``): a file written by a later MINOR loads in
  older code (ADR-008 Decision 5, #976).
- 1.7 (2026-10-18, #992): Added ``ReasonCode.WRITE_GROUP`` (``group.write``) and
  ``ReasonCode.WRITE_GROUP_CUSTOMER_PROPAGATION``
  (``group.write_customer_propagation``); additive, 1.6 records still load. A
  table's writes and locking reads land on one engine, its source of truth
  (ADR-031 Amendment 9): a unit holding a table's write group is placed with
  ``group.write`` (pass ``co_dependency``), and a query the customer gate moves
  with a write group the customer re-routed carries
  ``group.write_customer_propagation``, and a saved assignment that split a
  table's writes, re-routed when loaded for approval, ``group.write_reroute``.
- 1.8 (2026-10-18, #1003, ADR-031 Decision 12 Amendment 10): Added
  ``MovePass.AUTO_PICK`` (``auto_pick``) and ``ReasonCode.TIE_DECIDED_FOR_YOU``
  (``tie.decided_for_you``), and the optional ``MoveEvidence`` fields
  ``choice_id``, ``choice_scores``, ``choice_unrounded_scores`` and
  ``choice_rule``; additive, 1.7 records still load. ``--auto`` ("decide for
  me") settles each pending engine choice (one per table) with one Move per
  query: actor ``rule``, the alternative as the runner-up, and both engines'
  whole-point and unrounded choice scores in the evidence.
- 1.9 (2026-10-20, #377): Added ``ReasonCode.KEY_SCOPED_COUNT``
  (``gate.key_scoped_count``), ``CapabilityWaiver`` and the optional
  ``MoveEvidence.capability_waivers``: a key-scoped COUNT placed on the engine its
  table's writes are on, once a capability waiver lets that engine score it (ADR-031
  Decision 2 Amendment 12).
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
    # --auto's "decide for me" pick for a pending engine choice (Decision 12
    # Amendment 10, #1003).
    AUTO_PICK = "auto_pick"


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
    # A pending engine choice --auto decided for the customer (#1003): higher
    # unrounded score, then the engine the plan uses more, then break_tie.
    TIE_DECIDED_FOR_YOU = "tie.decided_for_you"
    GROUP_MEAN = "group.mean"
    # A unit holding a table's write group: all of a table's writes share one
    # engine, its source of truth (ADR-031 Amendment 9, #992), placed on the unit's mean.
    WRITE_GROUP = "group.write"
    OVERRIDE_TEXT_SEARCH = "override.text_search"
    OVERRIDE_UTILITY = "override.utility"
    FALLBACK_NO_SCORE = "fallback.no_score"
    # A single query with no engine able to own it at all (the cache owner
    # normalisation's "no owner candidate" fallback, #786), kept apart from
    # no_score (every candidate gated).
    FALLBACK_NO_OWNER = "fallback.no_owner"
    # A co-dependency group with no engine able to serve every member (each
    # engine gated for at least one member; evidence.group_gates names them,
    # #787).
    FALLBACK_NO_GROUP_OWNER = "fallback.no_group_owner"
    CONSOLIDATE_NO_UNIQUE_VALUE = "consolidate.no_unique_value"
    CONSOLIDATE_SOURCE_COMPATIBLE_SURVIVES = "consolidate.source_compatible_survives"
    FLOOR_OPENSEARCH = "floor.opensearch"
    FLOOR_COST_SHARE = "floor.cost_share"
    SWEEP_ORPHAN = "sweep.orphan"
    FLAG_CAPABILITY_GAP = "flag.capability_gap"
    CUSTOMER_PICK = "customer.pick"
    # A query moved with a co-dependent query the customer re-routed (Decision 4);
    # the customer did not pick it themselves.
    GROUP_CUSTOMER_PROPAGATION = "group.customer_propagation"
    # A query moved with a write of the same table the customer re-routed
    # (Amendment 9): a table's writes stay on one engine.
    WRITE_GROUP_CUSTOMER_PROPAGATION = "group.write_customer_propagation"
    # A saved assignment that split a table's writes, re-routed to one source of
    # truth when it is loaded for approval or resumed (Amendment 9, #992).
    WRITE_GROUP_REROUTE = "group.write_reroute"
    CACHE_LEGACY_OWNER = "cache.legacy_owner"
    # A post-schema rollback: the redesign on a reroute version failed, so the
    # next version restates the one before the reroute and each moved query
    # goes back (#770).
    ROLLBACK_REDESIGN_FAILED = "rollback.redesign_failed"
    # A key-scoped COUNT placed once its table's writes are: DynamoDB-style Query with
    # Select = COUNT, whose aggregation gate the catalog waives when the table's
    # source of truth is the engine (ADR-031 Decision 2 Amendment 12, #377).
    KEY_SCOPED_COUNT = "gate.key_scoped_count"


class MoveActor(str, Enum):
    """Who caused the Move: a deterministic rule, the customer, or a model's flag."""

    RULE = "rule"
    CUSTOMER = "customer"
    MODEL_FLAG = "model_flag"


class EngineScore(BaseModel):
    """One engine and its score, e.g. the runner-up of a Move."""

    model_config = ConfigDict(extra="allow")

    engine: str = Field(..., description="Engine id")
    score: float | None = Field(
        None, description="The shared scorer's value; None when gated or never scored"
    )


class CandidateScore(BaseModel):
    """One rung of a query's candidate ladder at the moment of a Move."""

    model_config = ConfigDict(extra="allow")

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
            "(hard_exclusion, capability, model_flag) or write_gate (an engine that is not a system "
            "of record cannot own a write). None when the engine was eligible"
        ),
    )


class GroupGate(BaseModel):
    """Why one engine could not host a co-dependency group (#787)."""

    model_config = ConfigDict(extra="allow")

    engine: str = Field(..., description="The analyzed engine that was ruled out")
    query_id: str = Field(
        ..., description="The first member (by query id) the engine could not take"
    )
    gate: str = Field(
        ...,
        description=(
            "The gate that ruled it out for that member: the scorer's gate kind "
            "(hard_exclusion, capability, model_flag) or write_gate (not a system of record)"
        ),
    )


class CapabilityWaiver(BaseModel):
    """A capability the catalog waived for the destination engine (``gates.toml``
    ``[[waivers]]``, ADR-031 Decision 2 Amendment 12, #377)."""

    model_config = ConfigDict(extra="allow")

    rule_id: str = Field(..., description="The waiver's rule id in gates.toml")
    capability: str = Field(..., description="The capability waived")
    table: str = Field(..., description="The one table the query reads")
    source_of_truth: str = Field(
        ..., description="The engine the table's writes are on (its source of truth)"
    )


class MoveEvidence(BaseModel):
    """What the pass saw when it recorded the Move. Every field is optional."""

    model_config = ConfigDict(extra="allow")

    candidates: list[CandidateScore] = Field(
        default_factory=list,
        description="Every analyzed engine's score or gate for this query, sorted by engine",
    )
    group_scores: dict[str, float] = Field(
        default_factory=dict,
        description="For a co-dependency group: each eligible engine's mean member score",
    )
    group_gates: list[GroupGate] = Field(
        default_factory=list,
        description=(
            "For a co-dependency group no engine can serve (rule fallback.no_group_owner): "
            "each analyzed engine, sorted, with the member and gate that ruled it out. "
            "Empty otherwise"
        ),
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
    choice_id: str | None = Field(
        None,
        description=(
            "For an auto_pick Move: the pending engine choice it settled (its table). "
            "None otherwise"
        ),
    )
    choice_scores: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "For an auto_pick Move: each tied engine's whole-point choice score "
            "(the score routing compared). Empty otherwise"
        ),
    )
    choice_unrounded_scores: dict[str, float] = Field(
        default_factory=dict,
        description=(
            "For an auto_pick Move: each tied engine's unrounded choice score. Empty otherwise"
        ),
    )
    choice_rule: str | None = Field(
        None,
        description=(
            "For an auto_pick Move: the decide-for-me rule that picked the engine "
            "(unrounded_score, settled_workload or tie_break). None otherwise"
        ),
    )
    previous_tied_engines: list[str] = Field(
        default_factory=list,
        description=(
            "For a Move that changes the query's tie: its tied_engines before the "
            "Move (sorted). Empty otherwise"
        ),
    )
    capability_waivers: list[CapabilityWaiver] = Field(
        default_factory=list,
        description=(
            "For a gate.key_scoped_count Move: each capability the catalog waived for "
            "the destination engine, with the table and its source of truth. Empty otherwise"
        ),
    )


class Move(BaseModel):
    """One engine change of one query (#616).

    ``pass`` is a Python keyword, so the attribute is ``pass_``; it reads and
    writes as ``pass`` in JSON (alias, serialized by alias by default;
    ``serialize_by_alias`` needs pydantic 2.11, the project floor).
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True, serialize_by_alias=True)

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
