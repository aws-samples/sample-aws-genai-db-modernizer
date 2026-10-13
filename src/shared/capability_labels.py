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
today (its own ``CAPABILITY_DETECTORS`` entry is an empty, structural-only
placeholder); kept here anyway so this never silently regresses if that
changes.
"""

from __future__ import annotations

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
