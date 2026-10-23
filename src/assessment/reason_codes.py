"""Routing's internal reason codes, mapped onto the public ``reason_code`` list.

Routing records why every engine change happened as a Move with an internal
``ReasonCode`` (``src.contracts.move_ledger``). Those rules are finer-grained than
the public contract and change as routing evolves, so each one maps onto exactly one
public code here. A new internal code must be added to :data:`REASON_CODES` in the
same change that adds it: ``tests/unit/assessment/test_reason_codes.py`` fails on an
unmapped code.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import get_args

from src.assessment.models import PublicReasonCode
from src.contracts.move_ledger import UNSERVABLE, Move, MovePass, ReasonCode

R = ReasonCode

#: Every internal reason code and the public code it reports as.
REASON_CODES: Mapping[ReasonCode, PublicReasonCode] = {
    R.HIGHEST_SCORE: "highest_score",
    R.TIE_SOURCE_COMPATIBLE: "tie_rule",
    R.TIE_STABILITY: "tie_rule",
    R.TIE_PLACEHOLDER_SETTLED_WORKLOAD: "tie_rule",
    R.TIE_PLACEHOLDER_HASH: "tie_rule",
    R.TIE_CLEARED: "tie_rule",
    R.TIE_DECIDED_FOR_YOU: "tie_rule",
    R.GROUP_MEAN: "co_dependency",
    R.GROUP_CUSTOMER_PROPAGATION: "co_dependency",
    R.WRITE_GROUP: "write_group",
    # Moved with a write of the same table: a table's writes share one engine.
    R.WRITE_GROUP_CUSTOMER_PROPAGATION: "write_group",
    R.WRITE_GROUP_REROUTE: "write_group",
    # A key-scoped COUNT, a read in no write group: once a capability waiver lets the
    # engine of its table's writes score it, it goes to its best-scoring engine.
    R.KEY_SCOPED_COUNT: "highest_score",
    R.OVERRIDE_TEXT_SEARCH: "override",
    R.OVERRIDE_UTILITY: "override",
    R.FALLBACK_NO_SCORE: "fallback",
    R.FALLBACK_NO_OWNER: "fallback",
    R.FALLBACK_NO_GROUP_OWNER: "fallback",
    R.CONSOLIDATE_NO_UNIQUE_VALUE: "consolidated",
    R.CONSOLIDATE_SOURCE_COMPATIBLE_SURVIVES: "consolidated",
    R.FLOOR_OPENSEARCH: "engine_floor",
    R.FLOOR_COST_SHARE: "engine_floor",
    R.SWEEP_ORPHAN: "sweep",
    R.FLAG_CAPABILITY_GAP: "capability_flag",
    R.CUSTOMER_PICK: "customer_choice",
    # A legacy cache owner moved to the best-scoring engine that may own the query.
    R.CACHE_LEGACY_OWNER: "highest_score",
    # Undoes a post-schema reroute whose redesign failed.
    R.ROLLBACK_REDESIGN_FAILED: "post_schema_reroute",
}

#: Internal codes whose Moves count as a reality-check consolidation.
CONSOLIDATION_CODES = frozenset(
    code for code, public in REASON_CODES.items() if public == "consolidated"
)


def unmapped_codes(
    mapping: Mapping[ReasonCode, str] = REASON_CODES,
    codes: Iterable[ReasonCode] = ReasonCode,
) -> list[ReasonCode]:
    """The internal codes ``mapping`` gives no public code (or one outside the list)."""
    public = set(get_args(PublicReasonCode))
    return [code for code in codes if mapping.get(code) not in public]


def public_reason_code(move: Move) -> PublicReasonCode:
    """The public code of the Move that put a statement where it is.

    A Move to ``unservable`` (no engine in the engine scope can serve the statement)
    reports as ``unservable``; the post-schema router's Moves (pass
    ``post_schema_flag``: a reroute after a flag, or the rollback of one) report as
    ``post_schema_reroute``, whatever their rule; every other Move reports its rule's
    public code.
    """
    if move.pass_ == MovePass.UNSERVABLE or move.to_engine == UNSERVABLE:
        return "unservable"
    if move.pass_ == MovePass.POST_SCHEMA_FLAG:
        return "post_schema_reroute"
    return REASON_CODES[move.reason_code]
