# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""One check for placeholder values a broken renderer leaks into a deliverable.

"undefined", "NaN", "null" or "None" count as a leak only when they stand as a whole
value: a table cell (Markdown pipes or a tab-separated rendered cell), the value after
": ", a code span, or a line of their own. Model prose may use the same words in a
sentence ("stored as null or omitted"), which is not a leak.
"""

from __future__ import annotations

import re

PLACEHOLDER_WORDS = ("undefined", "NaN", "null", "None")
_VALUE_START = r"(?:^|\||\t|:\s+|`)\s*"
_VALUE_END = r"\s*(?:\||\t|$|`|,)"


def placeholder_leaks(text: str, words: tuple[str, ...] = PLACEHOLDER_WORDS) -> list[str]:
    """Placeholder words that stand as a whole value anywhere in ``text``."""
    return [
        w
        for w in words
        if re.search(rf"{_VALUE_START}{re.escape(w)}{_VALUE_END}", text, re.MULTILINE)
    ]
