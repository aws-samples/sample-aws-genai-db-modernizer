# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The engineering report's opening decision line, cut out for a byte-for-byte golden.

#2460: the engineering report opens with the decision line and its wave's readiness,
right under its title. A golden rendered by main's code before that pins everything
else, so a test compares the render without those lines. On a render with no such
lines this changes nothing.
"""

from __future__ import annotations

import re

TITLE = "# Database Modernization: Engineering Report\n\n"
_OPENING = re.compile(re.escape(TITLE) + r"\*\*[^\n]+\*\*\n\n(?:Wave \d+: [^\n]+\n\n)?")


def without_decision_opening(md: str) -> str:
    """``md`` without the decision line and readiness line under its title."""
    return _OPENING.sub(TITLE, md, count=1)
