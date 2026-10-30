# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The one rule for product names in rendered deliverables (#954).

A deliverable may not name another product: "ATX", "AWS Transform", or the
standalone word "transform". Whole words only, so ordinary prose such as
"transformation" or "transforms" (which a model writes) passes. Inline script and
style bodies are removed from HTML first: they hold browser API names (the canvas
``setTransform`` call, the CSS ``transform`` property), not text anyone reads.
"""

from __future__ import annotations

import re

FORBIDDEN = re.compile(r"\bATX\b|AWS\s+Transform|\btransform\b", re.IGNORECASE)
_SCRIPT_OR_STYLE = re.compile(r"<(script|style)\b.*?</\1>", re.DOTALL | re.IGNORECASE)


def visible_html(html: str) -> str:
    """``html`` with the bodies of its script and style elements removed."""
    return _SCRIPT_OR_STYLE.sub(" ", html)


def hits(text: str) -> list[str]:
    """Each forbidden match in ``text`` with 40 characters of context."""
    return [
        f"...{text[max(0, m.start() - 40) : m.end() + 40]}..." for m in FORBIDDEN.finditer(text)
    ]
