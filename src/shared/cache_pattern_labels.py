"""Customer-facing labels for the cache overlay's read shapes (#296, #663).

One table for every deliverable that names a cached read's ``cache_pattern``:
the overlay's own ``cache_reason`` text, the executive summary and the
engineering report's "Read shapes" line, so ``top_n`` never renders as "top n"
in one place and "top-N lookup" in another.
"""

from __future__ import annotations

CACHE_PATTERN_LABELS: dict[str, str] = {
    "point_lookup": "point lookup",
    "top_n": "top-N lookup",
    "session_lookup": "session/token lookup",
    "reference_read": "small reference read",
}


def cache_pattern_label(pattern: str) -> str:
    """The label for ``pattern``, or the key with underscores as spaces if unknown."""
    return CACHE_PATTERN_LABELS.get(pattern, pattern.replace("_", " "))
