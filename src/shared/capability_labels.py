"""Display strings for the hard capabilities ``capability_registry`` can require.

Shared between ``src.agents.referee.assignment_resolver`` (the "lacks required
capability: ..." reason it builds when a hard capability gate zeroes an engine,
#572 review suggestion 7) and ``src.agents.referee.synthesis_report``
(``_capability_pin_reason``, which reads that same reason text back out to
explain why an Aurora engine's queries stay relational, #393) -- one map, so
the two can never show a different name, or a raw id, for the same capability.
Same shape as ``src.shared.signal_labels.SIGNAL_NOUNS``/``signal_noun``.

Every capability ``capability_registry.detect_required_capabilities`` can put
in a "lacks required capability" reason needs an entry here (#393 review:
``inverted_index`` and ``scan_engine`` had no entry, so a query pinned to
Aurora for either leaked the raw capability id into customer text instead of
reading as prose; ``tests/unit/agents/referee/
test_synthesis_capability_pin_display_coverage.py`` asserts this dict covers
every capability name the registry can produce). ``multi_doc_acid`` is in
``ENGINE_CAPABILITIES`` but ``detect_required_capabilities`` never emits it
today (its own ``CAPABILITY_FACTS`` entry is an empty, structural-only
placeholder); kept here anyway so this never silently regresses if that
changes.
"""

from __future__ import annotations

from collections.abc import Iterable

CAPABILITY_DISPLAY_NAMES: dict[str, str] = {
    "aggregation": "aggregation",
    "complex_joins": "multi-table joins",
    "computed_join": "a join on a computed expression",
    "sql_admin": "a utility or DDL statement",
    "inverted_index": "full-text or pattern search",
    "scan_engine": "window or recursive queries",
    "multi_doc_acid": "multi-document transactions",
}


def capability_label(name: str) -> str:
    """``complex_joins`` -> ``multi-table joins``; unknown capabilities read as
    their name with underscores turned to spaces, same fallback shape as
    ``src.shared.signal_labels.signal_noun``."""
    return CAPABILITY_DISPLAY_NAMES.get(name, name.replace("_", " "))


# How a query that requires a capability reads after "it" in a customer-facing
# rejection (#801 review): "it is a utility or DDL statement", never "it needs
# a utility or DDL statement". A capability not listed here reads as "needs
# <capability_label>", which is right for every noun-shaped label.
_CAPABILITY_GAP_CLAUSES: dict[str, str] = {
    "sql_admin": "is a utility or DDL statement",
    "computed_join": "joins on a computed expression",
    "scan_engine": "uses a window function or a recursive query",
}


def _join_and(items: list[str]) -> str:
    """``[a]`` -> ``a``; ``[a, b]`` -> ``a and b``; ``[a, b, c]`` -> ``a, b and c``."""
    return items[0] if len(items) == 1 else f"{', '.join(items[:-1])} and {items[-1]}"


def capability_gap_phrase(names: Iterable[str]) -> str:
    """The capabilities a query requires, as a verb phrase that reads after
    "it" or "query q2 in that group": ``["aggregation", "complex_joins"]`` ->
    ``needs aggregation and multi-table joins``; ``["sql_admin"]`` -> ``is a
    utility or DDL statement``. Clauses come in sorted capability order,
    needs last, joined with ", and "."""
    unique = sorted(set(names))
    clauses = [_CAPABILITY_GAP_CLAUSES[n] for n in unique if n in _CAPABILITY_GAP_CLAUSES]
    needs = [capability_label(n) for n in unique if n not in _CAPABILITY_GAP_CLAUSES]
    if needs:
        clauses.append(f"needs {_join_and(needs)}")
    return ", and ".join(clauses)
