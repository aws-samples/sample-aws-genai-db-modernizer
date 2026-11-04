# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The cutover message: a fixed template, never model text (#1800, spec section 8)."""

from __future__ import annotations

from pathlib import Path

from src.shared.cutover_message import (
    CUTOVER_BODY,
    CUTOVER_MESSAGE_ID,
    CUTOVER_MESSAGES,
    cutover_message_markdown,
    cutover_message_text,
)

SPEC_TEXT = (
    "**When to cut over is your decision.** The data movement for this step keeps your Aurora copy in sync "
    "with your source and reports the evidence below: replication lag, rows loaded, and validation per table "
    "(with the tables DMS could not validate, and why). These figures help you decide; they are not a "
    "recommendation to switch. Choosing the moment to cut over, and preparing your applications for it, "
    "remains entirely your responsibility."
)
REPO = Path(__file__).resolve().parents[3]


def test_the_message_is_the_spec_text_verbatim():
    assert cutover_message_markdown() == SPEC_TEXT
    assert cutover_message_text() == SPEC_TEXT.replace("**", "")
    assert CUTOVER_MESSAGE_ID in CUTOVER_MESSAGES


def test_the_message_lives_in_one_module():
    sentence = "remains entirely your responsibility"
    owners = [
        p.relative_to(REPO).as_posix()
        for p in (REPO / "src").rglob("*.py")
        if sentence in p.read_text()
    ]
    assert owners == ["src/shared/cutover_message.py"]
    assert CUTOVER_BODY.endswith(sentence + ".")
