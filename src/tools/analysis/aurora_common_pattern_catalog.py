"""
Aurora Common Relational Pattern Catalog

Shared relational patterns detected by both Aurora PostgreSQL and Aurora MySQL
analysis agents. These represent workloads where a relational engine excels —
complex joins, aggregations, transactions, referential integrity.

Anti-patterns identify queries that are better served by a purpose-built engine
(DynamoDB, ElastiCache, OpenSearch).

Each pattern has a specialist-assigned base_score that directly influences the
pattern_match_score dimension via the catalog-driven override in analysis tools.

The entries live in ``src/catalog/patterns/<pattern_type>.toml`` (ADR-031 Decision 8,
#492): ``catalog_patterns``/``catalog_anti_patterns`` build the lists below from them,
under the names the analysis tools import. Edit the TOML file, not this module.
"""

from src.shared.catalog.patterns import catalog_entries
from src.tools.analysis.pattern_catalog_base import (
    CatalogAntiPattern,
    CatalogPattern,
    catalog_anti_patterns,
    catalog_patterns,
)

# ==========================================================================
# Aurora Common Pattern Catalog — Shared relational patterns
# ==========================================================================

AURORA_COMMON_PATTERNS: list[CatalogPattern] = catalog_patterns("aurora_common")


# ==========================================================================
# Aurora Common Anti-Pattern Catalog
# ==========================================================================

AURORA_COMMON_ANTI_PATTERNS: list[CatalogAntiPattern] = catalog_anti_patterns("aurora_common")

# Sentences of the Aurora anti-pattern catalog that name, or lean toward, an engine other
# than Aurora (each entry's ``engine_hint`` in its pattern file). They stay separate so the
# catalog composes its text from them and report text can drop them
# (synthesis_report._without_hedge, #649/#668): a risk in a deliverable is attributed to
# the engine its queries are assigned to, and must not advise another one. The analysis
# output keeps the full catalog wording.
_ANTI = dict(catalog_entries("aurora_common", "anti_pattern"))


def _hint(pattern_type: str) -> str:
    return _ANTI[pattern_type].engine_hint or ""


def _guidance(pattern_type: str) -> str:
    return _ANTI[pattern_type].guidance


HIGH_FREQUENCY_PK_LOOKUP_ENGINE_HINT = _hint("high-frequency-pk-lookup")
SIMPLE_CACHE_READ_ENGINE_HINT = _hint("simple-cache-read")
FULL_TEXT_SEARCH_ENGINE_HINT = _hint("full-text-search-candidate")
TIME_SERIES_INGEST_ENGINE_HINT = _hint("unbounded-time-series-ingest")
NO_RELATIONAL_NEED_ENGINE_HINT = _hint("no-relational-need")
SINGLE_ACCESS_PATTERN_ENGINE_HINT = _hint("single-access-pattern-table")
HIGH_VOLUME_TEXT_SEARCH_ENGINE_HINT = _hint("high-volume-text-search")

# In catalog order (aurora-anti-01 .. aurora-anti-07).
CATALOG_ENGINE_HINTS: tuple[str, ...] = tuple(
    support.engine_hint for support in _ANTI.values() if support.engine_hint
)

# The `guidance` of those entries is an alternative-engine suggestion as a whole
# (``guidance_names_engine`` in the pattern file).
HIGH_FREQUENCY_PK_LOOKUP_GUIDANCE = _guidance("high-frequency-pk-lookup")
SIMPLE_CACHE_READ_GUIDANCE = _guidance("simple-cache-read")
FULL_TEXT_SEARCH_GUIDANCE = _guidance("full-text-search-candidate")
TIME_SERIES_INGEST_GUIDANCE = _guidance("unbounded-time-series-ingest")
NO_RELATIONAL_NEED_GUIDANCE = _guidance("no-relational-need")
SINGLE_ACCESS_PATTERN_GUIDANCE = _guidance("single-access-pattern-table")
HIGH_VOLUME_TEXT_SEARCH_GUIDANCE = _guidance("high-volume-text-search")

CATALOG_ENGINE_GUIDANCE: frozenset[str] = frozenset(
    support.guidance for support in _ANTI.values() if support.guidance_names_engine
)


# ==========================================================================
# Lookup helpers
# ==========================================================================

PATTERN_BY_ID: dict[str, CatalogPattern] = {p.pattern_id: p for p in AURORA_COMMON_PATTERNS}
ANTI_PATTERN_BY_ID: dict[str, CatalogAntiPattern] = {
    ap.pattern_id: ap for ap in AURORA_COMMON_ANTI_PATTERNS
}


# ==========================================================================
# Score weights
# ==========================================================================

AURORA_PG_SCORE_WEIGHTS: dict[str, float] = {
    "pattern_match": 0.40,
    "complexity": 0.30,
    "performance": 0.20,
    "cost": 0.10,
}

AURORA_MYSQL_SCORE_WEIGHTS: dict[str, float] = {
    "pattern_match": 0.40,
    "complexity": 0.30,
    "performance": 0.20,
    "cost": 0.10,
}


# ==========================================================================
# Detection
#
# The keyword tables the Aurora analysis's detectors read (aggregation,
# subquery, pagination, IN-list and text-search keywords) live in
# ``src.tools.sql.query_facts`` (#491): one engine-neutral definition per fact.
# ==========================================================================

TRANSACTION_WRITE_TYPES: frozenset[str] = frozenset({"INSERT", "UPDATE", "DELETE"})

# Query types to skip during analysis (admin/metadata commands, not application queries).
# OTHER is not one of them (#733, #734): the collector types OTHER any statement
# whose first word is not a DML keyword, application queries included, so an
# OTHER query is skipped only when ``is_utility_statement`` says so.
SKIP_QUERY_TYPES: frozenset[str] = frozenset({"SHOW", "DESCRIBE", "EXPLAIN", "SET"})

# Minimum CPS threshold for standard-crud pattern (avoids noise from rarely-used queries)
STANDARD_CRUD_MIN_CPS: float = 0.5

# Baseline pattern_match_score floor for tables with queries but no detected patterns.
# Represents "valid relational workload, nothing exceptional" — prevents zero scores
# that make tables look unsuitable when they're perfectly fine on Aurora.
BASELINE_PATTERN_MATCH_FLOOR: int = 45

# Thresholds
HIGH_FREQUENCY_PK_LOOKUP_CPS: float = 30.0
# The high-ingest threshold is shared with every engine: pattern_catalog_base.
CACHE_READ_CPS_THRESHOLD: float = 20.0
HIGH_VOLUME_TEXT_SEARCH_CPS: float = 10.0
TEXT_COLUMN_RATIO_THRESHOLD: float = 0.40

# Text data types for column ratio calculation
TEXT_DATA_TYPES: frozenset[str] = frozenset(
    {"text", "varchar", "character varying", "longtext", "clob", "nvarchar", "citext"}
)
