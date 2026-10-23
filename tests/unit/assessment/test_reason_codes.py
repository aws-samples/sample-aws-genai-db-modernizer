"""Every internal reason code maps onto one public ``reason_code`` (#1182)."""

from __future__ import annotations

from typing import get_args

import pytest

from src.assessment.models import PublicReasonCode
from src.assessment.reason_codes import REASON_CODES, public_reason_code, unmapped_codes
from src.contracts.move_ledger import UNSERVABLE, Move, MovePass, ReasonCode


def test_every_internal_code_is_mapped() -> None:
    """A new ``move_ledger.ReasonCode`` must be given its public code in the same change."""
    assert unmapped_codes() == []
    assert set(REASON_CODES) == set(ReasonCode)


@pytest.mark.parametrize("code", list(ReasonCode))
def test_a_removed_mapping_is_reported(code: ReasonCode) -> None:
    without = {k: v for k, v in REASON_CODES.items() if k != code}
    assert unmapped_codes(without) == [code]


def test_a_mapping_outside_the_public_list_is_reported() -> None:
    wrong = {**REASON_CODES, ReasonCode.HIGHEST_SCORE: "best_score"}
    assert unmapped_codes(wrong) == [ReasonCode.HIGHEST_SCORE]


def test_public_codes_are_the_agreed_list() -> None:
    assert get_args(PublicReasonCode) == (
        "highest_score",
        "tie_rule",
        "co_dependency",
        "write_group",
        "override",
        "fallback",
        "consolidated",
        "engine_floor",
        "sweep",
        "capability_flag",
        "customer_choice",
        "post_schema_reroute",
        "unservable",
    )


@pytest.mark.parametrize(
    ("code", "public"),
    [
        (ReasonCode.HIGHEST_SCORE, "highest_score"),
        (ReasonCode.TIE_STABILITY, "tie_rule"),
        (ReasonCode.TIE_DECIDED_FOR_YOU, "tie_rule"),
        (ReasonCode.GROUP_MEAN, "co_dependency"),
        (ReasonCode.WRITE_GROUP, "write_group"),
        (ReasonCode.OVERRIDE_UTILITY, "override"),
        (ReasonCode.FALLBACK_NO_GROUP_OWNER, "fallback"),
        (ReasonCode.CONSOLIDATE_NO_UNIQUE_VALUE, "consolidated"),
        (ReasonCode.CONSOLIDATE_SOURCE_COMPATIBLE_SURVIVES, "consolidated"),
        (ReasonCode.FLOOR_COST_SHARE, "engine_floor"),
        (ReasonCode.SWEEP_ORPHAN, "sweep"),
        (ReasonCode.CUSTOMER_PICK, "customer_choice"),
        # A key-scoped COUNT is a read in no write group.
        (ReasonCode.KEY_SCOPED_COUNT, "highest_score"),
    ],
)
def test_agreed_mappings(code: ReasonCode, public: str) -> None:
    assert REASON_CODES[code] == public


def _move(pass_: MovePass, code: ReasonCode) -> Move:
    return Move(seq=1, query_id="q", pass_=pass_, to_engine="dynamodb", reason_code=code)


def test_a_model_flag_in_the_reality_check_is_a_capability_flag() -> None:
    move = _move(MovePass.MODEL_FLAG, ReasonCode.FLAG_CAPABILITY_GAP)
    assert public_reason_code(move) == "capability_flag"


@pytest.mark.parametrize(
    "code", [ReasonCode.FLAG_CAPABILITY_GAP, ReasonCode.ROLLBACK_REDESIGN_FAILED]
)
def test_post_schema_moves_are_reroutes(code: ReasonCode) -> None:
    assert public_reason_code(_move(MovePass.POST_SCHEMA_FLAG, code)) == "post_schema_reroute"


def test_a_move_to_unservable_is_unservable() -> None:
    move = Move(
        seq=1,
        query_id="q",
        pass_=MovePass.UNSERVABLE,
        to_engine=UNSERVABLE,
        reason_code=ReasonCode.FALLBACK_NO_GROUP_OWNER,
    )
    assert public_reason_code(move) == "unservable"
