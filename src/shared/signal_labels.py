"""Short plural nouns for triage signals, for prose such as "led by full-text search".

Used by the ranking rationale (#152). The deck keeps its own forms
(``pptx_report.SIGNAL_LABEL`` / ``SIGNAL_MODIFIER``) because its sentences differ.
"""

from __future__ import annotations

SIGNAL_NOUNS: dict[str, str] = {
    "aggregations": "aggregations",
    "complex_joins": "complex joins",
    "eav_pattern": "entity-attribute-value reads",
    "json_columns": "JSON-column reads",
    "junction_tables": "junction-table reads",
    "key_value_lookups": "key-value lookups",
    "leaderboard_pattern": "top-N listings",
    "metadata_config": "metadata / config reads",
    "range_queries": "range queries",
    "session_store": "session lookups",
    "status_filters": "status filters",
    "subqueries": "correlated subqueries",
    "text_search": "full-text search",
    "time_series": "time-series reads",
}

# Signals that describe how often a query runs, not what it does. They never lead
# a rationale ("led by low-frequency reads" says nothing about the engine choice).
WORKLOAD_CHARACTERISTIC_SIGNALS = frozenset(
    {"low_frequency_reads", "low_frequency_writes", "high_frequency_reads", "write_heavy"}
)


def signal_noun(name: str) -> str:
    """``text_search`` -> ``full-text search``; unknown signals read as their name."""
    return SIGNAL_NOUNS.get(name, name.replace("_", " "))
