# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2185: one product-name rule for every Python check, ``tests/branding.py``.

The UI e2e check kept its own copy of the rule with no word boundary on
"transform", so the results page failed on model prose ("Add an ETL
transformation that writes enabled as N") that every deliverable check passes.
Each check now calls ``tests.branding``; no other test file spells the rule.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.branding import hits

TESTS = Path(__file__).resolve().parents[2]
# The rule's own spelling: a product name with a regex gap between its words.
_RULE_SPELLING = re.compile(r"AWS\\s[+*]\s*Transform|\\bATX\\b")


def test_no_test_file_keeps_its_own_product_name_rule() -> None:
    copies = [
        p.relative_to(TESTS).as_posix()
        for p in TESTS.rglob("*.py")
        if p.name != "branding.py"
        and p != Path(__file__).resolve()
        and _RULE_SPELLING.search(p.read_text(encoding="utf-8"))
    ]
    assert copies == [], f"use tests.branding.hits instead of a local rule: {copies}"


def test_the_ui_check_uses_the_shared_rule() -> None:
    text = (TESTS / "e2e" / "test_ui.py").read_text(encoding="utf-8")
    assert "from tests.branding import" in text
    body = text.split("def test_ui_shows_only_the_product_name", 1)[1].split("\ndef ", 1)[0]
    assert "product_name_hits(" in body


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "text",
    [
        # The sentence the results page showed (discourse, batch11121734).
        "Add an ETL transformation that writes enabled as N (1 or 0) on the item.",
        "The stream consumer transforms each change before the write.",
        "Values are transformed to strings.",
    ],
)
def test_the_model_prose_the_page_showed_passes(text: str) -> None:
    assert hits(text) == []


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "text",
    [
        "Open the job in ATX.",
        "Sent to AWS Transform.",
        "Sent to aws  transform.",
        "Transform job 3",
    ],
)
def test_the_product_name_still_fails(text: str) -> None:
    assert hits(text) != []
