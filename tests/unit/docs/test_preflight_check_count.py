# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The step 1 preflight docs never pin a stale check count.

``CHECK_ORDER`` is the one list of preflight checks. A page that writes a number next to
"checks" must write ``len(CHECK_ORDER)``; a page that lists the checks needs no number.
"""

from __future__ import annotations

import re
from pathlib import Path

from src.contracts.dms_preflight import CHECK_ORDER

REPO = Path(__file__).resolve().parents[3]
README = REPO / "infrastructure/cloudformation/data-movement/README.md"
WORDS = ("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
         "eleven", "twelve")  # fmt: skip
COUNT = re.compile(
    r"\b(\d+|" + "|".join(WORDS) + r")\s+(?:read-only\s+|preflight\s+)*checks\b", re.I
)


def _number(token: str) -> int:
    return int(token) if token.isdigit() else WORDS.index(token.lower())


def test_the_preflight_readme_states_no_count_that_differs_from_check_order():
    text = README.read_text(encoding="utf-8")
    found = [_number(m.group(1)) for m in COUNT.finditer(text)]
    assert all(n == len(CHECK_ORDER) for n in found), (found, len(CHECK_ORDER))
