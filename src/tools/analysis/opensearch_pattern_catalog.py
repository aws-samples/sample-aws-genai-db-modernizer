"""
OpenSearch Migration Pattern Catalog

Specialist-curated catalog of migration patterns for Amazon OpenSearch Service.
Supports two workload types: Search and Time-series (Log Analytics).

Each pattern has a specialist-assigned base_score that directly influences the
pattern_match_score dimension via the catalog-driven override in analysis tools.
Detection signals are documented as comments above each pattern's
``[engines.opensearch]`` table.

Anti-patterns use severity_weight (float 0.0–1.0), NOT the Severity enum.

The entries live in ``src/catalog/patterns/<pattern_type>.toml`` (ADR-031 Decision 8,
#492): ``catalog_patterns``/``catalog_anti_patterns`` build the lists below from them,
under the names the analysis tools import. Edit the TOML file, not this module.
"""

from src.shared.catalog.patterns import profile_pattern_types
from src.tools.analysis.pattern_catalog_base import (
    CatalogAntiPattern,
    CatalogPattern,
    catalog_anti_patterns,
    catalog_patterns,
)

# ==========================================================================
# OpenSearch Pattern Catalog — Search workload
# ==========================================================================

OPENSEARCH_PATTERNS: list[CatalogPattern] = catalog_patterns("opensearch")


# ==========================================================================
# OpenSearch Anti-Pattern Catalog
# ==========================================================================

OPENSEARCH_ANTI_PATTERNS: list[CatalogAntiPattern] = catalog_anti_patterns("opensearch")


# ==========================================================================
# Lookup helpers
# ==========================================================================

PATTERN_BY_ID: dict[str, CatalogPattern] = {p.pattern_id: p for p in OPENSEARCH_PATTERNS}
ANTI_PATTERN_BY_ID: dict[str, CatalogAntiPattern] = {
    ap.pattern_id: ap for ap in OPENSEARCH_ANTI_PATTERNS
}


# ==========================================================================
# Score weights (per-workload)
# ==========================================================================

# Search: pattern_match weighted highest — catalog scores directly reflect search fitness
OPENSEARCH_SEARCH_WEIGHTS: dict[str, float] = {
    "pattern_match": 0.45,
    "complexity": 0.25,
    "performance": 0.20,
    "cost": 0.10,
}

# Time-series: performance weighted higher — ingest throughput and aggregation latency are critical
OPENSEARCH_TIMESERIES_WEIGHTS: dict[str, float] = {
    "pattern_match": 0.40,
    "complexity": 0.20,
    "performance": 0.30,
    "cost": 0.10,
}


# The high-ingest threshold is shared with every engine: pattern_catalog_base.


# ==========================================================================
# Pattern type sets for classification lookups
# ==========================================================================

# Each OpenSearch fit pattern's ``profile`` in its pattern file.
SEARCH_PATTERN_TYPES: frozenset[str] = profile_pattern_types("search")

TIMESERIES_PATTERN_TYPES: frozenset[str] = profile_pattern_types("timeseries")


# ==========================================================================
# Keyword tuples for detection. The query-text keywords (full-text, wildcard,
# regex, fuzzy, timestamp, time range, time functions) live in
# ``src.tools.sql.query_facts`` (#491); the table-name keywords stay here.
# ==========================================================================

STALENESS_TABLE_KEYWORDS: tuple[str, ...] = (
    "log",
    "event",
    "metric",
    "trace",
    "telemetry",
    "audit_log",
)
