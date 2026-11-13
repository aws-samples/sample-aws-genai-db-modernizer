# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A saved Move that breaks the contract is named, never dropped (#2154).

Before #2154 ``move_ledger._moves_by_query`` and ``query_trace.version_moves`` skipped
a Move that failed validation, so the confidence, pin and trace read back from the
ledger silently left it out. Both now go through ``move_ledger.read_moves``.
"""

from __future__ import annotations

import pytest

from src.agents.referee.move_ledger import UnreadableMove, follow_moves, read_moves
from src.shared.query_trace import ledger_moves, version_moves


def _move(seq: int, query_id: str, to_engine: str, **extra) -> dict:  # noqa: ANN003
    return {
        "seq": seq,
        "query_id": query_id,
        "pass": "score",
        "from_engine": None,
        "to_engine": to_engine,
        "score_after": 80.0,
        "reason_code": "highest_score",
        **extra,
    }


def _version(moves: list[dict]) -> dict:
    return {
        "version": 2,
        "query_assignments": [
            {"query_id": "q1", "assigned_engine": "dynamodb", "confidence": 10},
        ],
        "moves": moves,
    }


BROKEN = {"seq": 2, "query_id": "q1", "pass": "score", "to_engine": "dynamodb"}


def test_follow_moves_raises_on_a_move_that_breaks_the_contract() -> None:
    version = _version([_move(1, "q1", "dynamodb"), BROKEN])
    with pytest.raises(UnreadableMove) as raised:
        follow_moves(version, [version])
    assert str(raised.value) == (
        "assignment v2: Move 2 does not match the Move contract (reason_code), "
        "so its ledger cannot be read"
    )
    # Nothing was corrected from a partial ledger.
    assert version["query_assignments"][0]["confidence"] == 10


def test_the_trace_readers_raise_on_it_too() -> None:
    version = _version([_move(1, "q1", "dynamodb"), BROKEN])
    with pytest.raises(UnreadableMove):
        version_moves(version)
    with pytest.raises(UnreadableMove):
        ledger_moves([version])


def test_the_message_names_fields_never_stored_values() -> None:
    """The stored record is customer-derived data: the message carries field paths only."""
    planted = {**_move(1, "q1", "dynamodb"), "seq": "SELECT planted FROM t"}
    with pytest.raises(UnreadableMove) as raised:
        read_moves(_version([planted]))
    assert "planted" not in str(raised.value)
    assert "(seq)" in str(raised.value)


@pytest.mark.passes_on_main
def test_a_valid_ledger_and_a_version_before_the_ledger_still_read() -> None:
    version = _version([_move(1, "q1", "dynamodb")])
    follow_moves(version, [version])
    assert version["query_assignments"][0]["confidence"] == 80
    legacy = {k: v for k, v in version.items() if k != "moves"}
    assert version_moves(legacy) == []


def test_a_stored_dict_key_never_reaches_the_message_the_logs_or_stderr(
    caplog: pytest.LogCaptureFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    """#2154 review: pydantic's error location holds dict keys, and a key is stored data
    (a query text as a ``group_scores`` key). Only contract field names are shown; any
    other segment, and a version that is not a number, reads ``[*]`` / ``?``."""
    planted = "SELECT card_number FROM wp_secret_cards"
    move = _move(1, "q1", "dynamodb", evidence={"group_scores": {planted: "high"}})
    version = {**_version([move]), "version": planted}
    with caplog.at_level("DEBUG"), pytest.raises(UnreadableMove) as raised:
        read_moves(version)
    message = str(raised.value)
    assert message == (
        "assignment v?: Move 1 does not match the Move contract "
        "(evidence.group_scores.[*]), so its ledger cannot be read"
    )
    captured = capsys.readouterr()
    for text in (message, caplog.text, captured.out, captured.err):
        assert "card_number" not in text and "wp_secret_cards" not in text
