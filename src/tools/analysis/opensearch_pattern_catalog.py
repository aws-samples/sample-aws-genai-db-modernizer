"""
OpenSearch Migration Pattern Catalog

Specialist-curated catalog of migration patterns for Amazon OpenSearch Service.
Supports two workload types: Search and Time-series (Log Analytics).

Each pattern has a specialist-assigned base_score that directly influences the
pattern_match_score dimension via the catalog-driven override in analysis tools.
Detection signals are documented as comments above each pattern entry.

Anti-patterns use severity_weight (float 0.0–1.0), NOT the Severity enum.
"""

# TODO: CatalogPattern/CatalogAntiPattern are reused from the DynamoDB catalog
# for Phase 0 expediency. Refactor to a shared module (e.g. scoring.py) later.
from src.tools.analysis.pattern_catalog_base import CatalogAntiPattern, CatalogPattern

# ==========================================================================
# OpenSearch Pattern Catalog — Search workload
# ==========================================================================

OPENSEARCH_PATTERNS: list[CatalogPattern] = [
    # Signals: tsvector, tsquery, to_tsvector, plainto_tsquery, @@, match against
    CatalogPattern(
        pattern_id="os-01",
        pattern_type="full-text-search",
        description=(  # nosemgrep: string-concat-in-list
            "Full-text search using PostgreSQL tsvector/tsquery operators or MySQL MATCH AGAINST. "
            "OpenSearch's inverted index is purpose-built for this workload — tokenization, "
            "stemming, and relevance ranking are native."
        ),
        base_score=95,
        concerns=[
            "Ranking differences: OpenSearch uses BM25; PostgreSQL uses ts_rank. Results may differ.",
            "Custom analyzers may be needed to match existing text normalization.",
        ],
    ),
    # Signals: like %, ilike %
    CatalogPattern(
        pattern_id="os-02",
        pattern_type="wildcard-search",
        description=(  # nosemgrep: string-concat-in-list
            "LIKE/ILIKE wildcard queries on text columns. OpenSearch wildcard queries are more "
            "scalable than SQL LIKE on large datasets — no full-table scan."
        ),
        base_score=80,
        concerns=[
            "Leading wildcard queries (LIKE '%term') are expensive in both SQL and OpenSearch.",
            "For prefix queries, consider edge-ngram tokenizer for better performance.",
        ],
    ),
    # Signals: regexp, rlike, ~ , ~*
    CatalogPattern(
        pattern_id="os-03",
        pattern_type="regex-search",
        description=(  # nosemgrep: string-concat-in-list
            "Regex pattern matching (REGEXP, RLIKE, PostgreSQL ~ operator). OpenSearch supports "
            "regexp queries with finite automaton execution, avoiding full-scan overhead."
        ),
        base_score=75,
        concerns=[
            "Complex regex patterns can be expensive even with inverted index.",
            "Requires custom analyzer configuration for optimal performance.",
        ],
    ),
    # Signals: pg_trgm, similarity(
    CatalogPattern(
        pattern_id="os-04",
        pattern_type="fuzzy-search",
        description=(  # nosemgrep: string-concat-in-list
            "Fuzzy/similarity search via pg_trgm or similarity(). OpenSearch fuzzy queries use "
            "Levenshtein distance natively — no extension required."
        ),
        base_score=85,
        concerns=[
            "Fuzzy search performance depends on fuzziness parameter tuning.",
            "Trigram-based similarity may require custom tokenizer in OpenSearch.",
        ],
    ),
    # Signals: timestamp keywords + range operators (between, >=, <=)
    CatalogPattern(
        pattern_id="os-05",
        pattern_type="time-range-query",
        description=(  # nosemgrep: string-concat-in-list
            "Range queries on timestamp columns (created_at, event_time, etc.). OpenSearch "
            "time-series indices with date histograms excel at this access pattern."
        ),
        base_score=85,
        concerns=[
            "Hot shard risk if all documents are in the same time bucket.",
            "Index lifecycle management (ILM) policy needed for retention.",
        ],
    ),
    # Signals: date_trunc, date(, hour(, extract(
    CatalogPattern(
        pattern_id="os-06",
        pattern_type="time-aggregation",
        description=(  # nosemgrep: string-concat-in-list
            "Time-based aggregations using DATE_TRUNC, DATE(), HOUR(), or EXTRACT(). OpenSearch "
            "date histogram aggregations run against pre-sorted time fields — very efficient."
        ),
        base_score=90,
        concerns=[
            "Deep aggregation nesting (>3 levels) can cause memory pressure.",
            "High cardinality fields in sub-aggregations may require field data cache tuning.",
        ],
    ),
    # Signals: aggregate INSERT CPS >= 10 per table
    CatalogPattern(
        pattern_id="os-07",
        pattern_type="high-ingest",
        description=(  # nosemgrep: string-concat-in-list
            "High-throughput INSERT workload (>= 10 calls/second per table). OpenSearch bulk "
            "indexing API is designed for sustained high-ingest — auto-sharding and refresh "
            "interval tuning support this pattern."
        ),
        base_score=90,
        concerns=[
            (  # nosemgrep: string-concat-in-list
                "Refresh interval tuning needed: increase to 30s–60s for high-ingest to reduce "
                "segment merge overhead."
            ),
            "Shard sizing: aim for 10–50 GB per shard for balanced performance.",
        ],
    ),
]


# ==========================================================================
# OpenSearch Anti-Pattern Catalog
# ==========================================================================

OPENSEARCH_ANTI_PATTERNS: list[CatalogAntiPattern] = [
    # Signals: has_joins == True AND join_count >= 2
    CatalogAntiPattern(
        pattern_id="os-ap-01",
        pattern_type="multi-index-joins",
        description=(  # nosemgrep: string-concat-in-list
            "Queries joining multiple tables (join_count >= 2). OpenSearch does not support "
            "cross-index joins natively — these must be denormalized into a single index or "
            "handled in the application layer."
        ),
        severity_weight=0.8,
        guidance=(  # nosemgrep: string-concat-in-list
            "Denormalize into a single OpenSearch index, or handle joins in the application "
            "layer by fetching related documents separately."
        ),
    ),
    # Signals: UPDATE or DELETE query_type with CPS > 1
    CatalogAntiPattern(
        pattern_id="os-ap-02",
        pattern_type="acid-transactions",
        description=(  # nosemgrep: string-concat-in-list
            "Frequent UPDATE/DELETE operations (> 1 CPS) indicate mutable data with ACID "
            "transactional requirements. OpenSearch is optimized for immutable, append-only "
            "workloads."
        ),
        severity_weight=0.6,
        guidance=(  # nosemgrep: string-concat-in-list
            "Keep transactional data in the relational database. Use OpenSearch for read-heavy "
            "search/analytics queries only, with async sync from the source of truth."
        ),
    ),
    # Signals: timestamp column in equality check only (no range operators alongside)
    CatalogAntiPattern(
        pattern_id="os-ap-03",
        pattern_type="audit-columns-only",
        description=(  # nosemgrep: string-concat-in-list
            "Timestamp columns (created_at, updated_at, event_time, etc.) used only in equality "
            "checks — not range queries. This is an audit/metadata pattern, not a time-series "
            "workload. Guard rail to prevent false time-series classification."
        ),
        severity_weight=0.1,
        guidance=(  # nosemgrep: string-concat-in-list
            "Tables with timestamp columns used only for audit/metadata purposes are not "
            "time-series candidates. Evaluate for search suitability instead."
        ),
    ),
]


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


# ==========================================================================
# Constants
# ==========================================================================

# INSERT CPS threshold for high-ingest pattern detection (per table, summed across all queries)
HIGH_INGEST_CPS_THRESHOLD: float = 10.0


# ==========================================================================
# Pattern type sets for classification lookups
# ==========================================================================

SEARCH_PATTERN_TYPES: frozenset[str] = frozenset(
    {"full-text-search", "wildcard-search", "regex-search", "fuzzy-search"}
)

TIMESERIES_PATTERN_TYPES: frozenset[str] = frozenset(
    {"time-range-query", "time-aggregation", "high-ingest"}
)


# ==========================================================================
# Keyword tuples for detection
# ==========================================================================

FULLTEXT_KEYWORDS: tuple[str, ...] = (
    "tsvector",
    "tsquery",
    "to_tsvector",
    "plainto_tsquery",
    "@@",
    "match against",
)

WILDCARD_KEYWORDS: tuple[str, ...] = (
    "like ",
    "ilike ",
)

REGEX_KEYWORDS: tuple[str, ...] = (
    "regexp",
    "rlike",
    "~ ",
    "~* ",
)

FUZZY_KEYWORDS: tuple[str, ...] = (
    "pg_trgm",
    "similarity(",
)

TIMESTAMP_KEYWORDS: tuple[str, ...] = (
    "timestamp",
    "created_at",
    "updated_at",
    "event_time",
    "log_time",
    "ingested_at",
)

TIME_RANGE_KEYWORDS: tuple[str, ...] = (
    "between",
    ">=",
    "<=",
)

TIME_AGG_KEYWORDS: tuple[str, ...] = (
    "date_trunc",
    "date(",
    "hour(",
    "extract(",
)

STALENESS_TABLE_KEYWORDS: tuple[str, ...] = (
    "log",
    "event",
    "metric",
    "trace",
    "telemetry",
    "audit_log",
)
