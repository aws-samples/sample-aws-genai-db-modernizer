# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The cutover message: a fixed template, never model text (#1800, #2062)."""

from __future__ import annotations

from pathlib import Path

from src.shared.cutover_message import (
    CUTOVER_MESSAGE,
    CUTOVER_MESSAGE_ID,
    CUTOVER_MESSAGES,
    cutover_message_text,
)

REPO = Path(__file__).resolve().parents[3]


def test_the_message_is_the_one_sentence_and_nothing_more():
    assert cutover_message_text() == "The cutover is your responsibility."
    assert CUTOVER_MESSAGE_ID in CUTOVER_MESSAGES


def test_a_gate_saved_with_the_earlier_message_id_is_shown_the_one_sentence():
    assert cutover_message_text("cutover-decision-v1") == CUTOVER_MESSAGE


def test_the_message_lives_in_one_module():
    owners = [
        p.relative_to(REPO).as_posix()
        for p in (REPO / "src").rglob("*.py")
        if CUTOVER_MESSAGE in p.read_text()
    ]
    assert owners == ["src/shared/cutover_message.py"]


def test_the_routine_texts_take_their_cutover_sentence_from_the_one_module():
    from src.shared.routine_facts import CUTOVER_SENTENCE

    assert CUTOVER_SENTENCE == "the cutover is your responsibility"
    assert CUTOVER_SENTENCE[:1].upper() + CUTOVER_SENTENCE[1:] + "." == CUTOVER_MESSAGE
