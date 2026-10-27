"""An index column's key prefix length, as MySQL records it (#1623)."""

from __future__ import annotations


def prefix_length(sub_part: object) -> int | None:
    """``information_schema.STATISTICS.SUB_PART`` as a positive int, or None.

    SUB_PART is NULL when the whole column is indexed. A value that is not a
    positive whole number (a blank, a bad export) reads as "not recorded".
    """
    if isinstance(sub_part, bool) or sub_part is None:
        return None
    try:
        n = int(str(sub_part).strip())
    except ValueError:
        return None
    return n if n > 0 else None
