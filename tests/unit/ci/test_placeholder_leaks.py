# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The engineering report's placeholder check flags leaked values, not prose (#987)."""

from __future__ import annotations

import pytest

from tests.placeholders import placeholder_leaks


@pytest.mark.parametrize(
    "md",
    [
        "| Engine | null |",
        "| null | 12 |",
        "Monthly cost: None",
        "Key design: `undefined`",
        "Score: NaN, rounded",
        "null",
        "Engine\tnull\tRetained",
    ],
)
def test_a_placeholder_standing_as_a_value_is_a_leak(md: str) -> None:
    assert placeholder_leaks(md) != []


@pytest.mark.parametrize(
    "md",
    [
        "Posts with a zero date (0000-00-00) are stored as null or omitted.",
        "None of these queries scan the table.",
        "Return undefined behaviour is avoided by checking the type first.",
        "A NaN comparison is never true, so the filter is rewritten.",
    ],
)
def test_the_word_in_a_sentence_is_not_a_leak(md: str) -> None:
    assert placeholder_leaks(md) == []
