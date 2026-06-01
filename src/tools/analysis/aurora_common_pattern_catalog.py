"""
Aurora Common Relational Pattern Catalog

Shared relational patterns detected by both Aurora PostgreSQL and Aurora MySQL
analysis agents. These represent workloads where a relational engine excels —
complex joins, aggregations, transactions, referential integrity.

Anti-patterns identify queries that are better served by a purpose-built engine
(DynamoDB, ElastiCache, OpenSearch).

Each pattern has a specialist-assigned base_score that directly influences the
pattern_match_score dimension via the catalog-driven override in analysis tools.
"""

import re

from src.tools.analysis.pattern_catalog_base import CatalogAntiPattern, CatalogPattern

# ==========================================================================
# Aurora Common Pattern Catalog — Shared relational patterns
# ==========================================================================

AURORA_COMMON_PATTERNS: list[CatalogPattern] = [
    # Signals: 3+ table JOINs, multiple join conditions
    CatalogPattern(
        pattern_id="aurora-common-01",
        pattern_type="complex-join",
        description="Queries joining 3+ tables with multiple join conditions. Aurora's query optimizer excels at complex join planning — hash joins, merge joins, and nested loop selection are automatic and performant at scale.",
        base_score=90,
        concerns=[
            "Very large join result sets may benefit from materialized views or denormalization.",
        ],
    ),
    # Signals: GROUP BY + HAVING, SUM/COUNT/AVG, subquery aggregates
    CatalogPattern(
        pattern_id="aurora-common-02",
        pattern_type="aggregation-analytics",
        description="Aggregation queries with GROUP BY, HAVING, and aggregate functions (SUM, COUNT, AVG). Aurora handles analytical aggregations efficiently with parallel query execution.",
        base_score=85,
        concerns=[
            "Very large aggregation result sets may benefit from pre-computed summary tables.",
        ],
    ),
    # Signals: Multi-table INSERT/UPDATE in transaction context, FK-constrained writes
    CatalogPattern(
        pattern_id="aurora-common-03",
        pattern_type="transactional-write",
        description="Multi-table write operations within transaction context with FK constraints. Aurora provides full ACID compliance with multi-statement transactions — NoSQL engines cannot replicate this without application-level coordination.",
        base_score=90,
        concerns=[
            "Long-running transactions may cause lock contention under high concurrency.",
        ],
    ),
    # Signals: FK density > 2 on tables, CASCADE operations
    CatalogPattern(
        pattern_id="aurora-common-04",
        pattern_type="referential-integrity",
        description="Tables with high foreign key density (>2 FKs) and CASCADE operations. Relational engines enforce referential integrity at the database level — migrating to NoSQL would require application-level enforcement.",
        base_score=85,
        concerns=[
            "High FK density increases migration complexity if partial migration is planned.",
        ],
    ),
    # Signals: Large result sets, date-range filters, ORDER BY + LIMIT on aggregates
    CatalogPattern(
        pattern_id="aurora-common-05",
        pattern_type="reporting-query",
        description="Reporting queries with large result sets, date-range filters, and ordered/limited aggregate results. Aurora's read replicas can offload reporting workloads without impacting transactional performance.",
        base_score=80,
        concerns=[
            "Very heavy reporting may benefit from a dedicated analytics engine (Redshift).",
        ],
    ),
    # Signals: EXISTS/NOT EXISTS, IN (SELECT ...), correlated references
    CatalogPattern(
        pattern_id="aurora-common-06",
        pattern_type="correlated-subquery",
        description="Correlated subqueries using EXISTS, NOT EXISTS, or IN (SELECT ...). Aurora's optimizer can decorrelate and flatten these into efficient join plans.",
        base_score=80,
        concerns=[
            "Deeply nested correlated subqueries may benefit from CTE refactoring.",
        ],
    ),
    # Signals: UPDATE/DELETE with complex WHERE (not simple PK), batch operations
    CatalogPattern(
        pattern_id="aurora-common-07",
        pattern_type="multi-row-mutation",
        description="UPDATE or DELETE operations with complex WHERE clauses affecting multiple rows. Aurora handles bulk mutations with row-level locking and MVCC, maintaining consistency without full-table locks.",
        base_score=75,
        concerns=[
            "Large batch mutations may benefit from chunking to avoid long lock holds.",
        ],
    ),
    # Signals: OFFSET/LIMIT, cursor-based keyset pagination, COUNT(*) for total pages
    CatalogPattern(
        pattern_id="aurora-common-08",
        pattern_type="pagination",
        description="Pagination patterns using OFFSET/LIMIT or cursor-based keyset pagination (WHERE id > ? ORDER BY id LIMIT N). Aurora handles pagination efficiently with index-based seeks — NoSQL engines handle pagination poorly.",
        base_score=85,
        concerns=[
            "Deep OFFSET pagination (offset > 10000) degrades; prefer keyset pagination.",
        ],
    ),
    # Signals: 2-table JOIN (simpler than complex-join but still relational)
    CatalogPattern(
        pattern_id="aurora-common-09",
        pattern_type="simple-join",
        description="Queries joining 2 tables. While simpler than multi-table complex joins, these still require a relational engine with a query optimizer — NoSQL engines cannot perform server-side joins.",
        base_score=70,
        concerns=[],
    ),
    # Signals: Standard single-table CRUD with active query load
    CatalogPattern(
        pattern_id="aurora-common-10",
        pattern_type="standard-crud",
        description="Standard single-table CRUD operations (SELECT/INSERT/UPDATE/DELETE) at moderate frequency. These are valid relational workloads — Aurora handles them efficiently with connection pooling and buffer cache.",
        base_score=60,
        concerns=[],
    ),
    # Signals: WHERE col IN (...) with multiple values
    CatalogPattern(
        pattern_id="aurora-common-11",
        pattern_type="batch-in-list",
        description="Batch lookups using WHERE column IN (...) with multiple values. Aurora optimizes IN-list queries with index range scans — more flexible than DynamoDB's BatchGetItem (100-item limit, PK-only).",
        base_score=70,
        concerns=[
            "Very large IN-lists (>1000 values) may benefit from temporary tables or joins.",
        ],
    ),
]


# ==========================================================================
# Aurora Common Anti-Pattern Catalog
# ==========================================================================

AURORA_COMMON_ANTI_PATTERNS: list[CatalogAntiPattern] = [
    # Signals: Single-row SELECT by PK at >100 cps
    CatalogAntiPattern(
        pattern_id="aurora-anti-01",
        pattern_type="high-frequency-pk-lookup",
        description="Single-row SELECT by primary key at very high frequency (>100 calls/second). DynamoDB provides single-digit millisecond latency for key-value lookups at any scale — Aurora adds unnecessary overhead for this access pattern.",
        severity_weight=0.8,
        guidance="Migrate high-frequency PK lookups to DynamoDB for predictable sub-millisecond latency at scale.",
    ),
    # Signals: High-freq reads of small/static data, TTL-like patterns
    CatalogAntiPattern(
        pattern_id="aurora-anti-02",
        pattern_type="simple-cache-read",
        description="High-frequency reads of small, rarely-changing data (config, sessions, feature flags). ElastiCache provides microsecond latency for cached reads without database connection overhead.",
        severity_weight=0.7,
        guidance="Consider ElastiCache/Redis for session stores, config caches, and frequently read reference data.",
    ),
    # Signals: LIKE '%term%', text search patterns
    CatalogAntiPattern(
        pattern_id="aurora-anti-03",
        pattern_type="full-text-search-candidate",
        description="Full-text search patterns using LIKE '%term%' or similar constructs. OpenSearch provides purpose-built inverted indexes with relevance scoring, tokenization, and stemming.",
        severity_weight=0.6,
        guidance="Offload full-text search to OpenSearch for better relevance ranking and scalability. Keep Aurora as source of truth with async sync.",
    ),
    # Signals: Append-only INSERT at very high rate with no complex reads
    CatalogAntiPattern(
        pattern_id="aurora-anti-04",
        pattern_type="unbounded-time-series-ingest",
        description="Append-only INSERT workload at very high rate with no complex read queries. DynamoDB or OpenSearch time-series indices are better suited for high-throughput append-only ingestion without relational query needs.",
        severity_weight=0.7,
        guidance="Consider DynamoDB (with TTL) or OpenSearch data streams for high-throughput append-only workloads that don't require complex relational queries.",
    ),
    # aurora-anti-05
    CatalogAntiPattern(
        pattern_id="aurora-anti-05",
        pattern_type="no-relational-need",
        description="Table with no foreign keys and no join participation — all queries are single-table CRUD. This workload has no structural relational requirement and could run on a simpler, purpose-built engine (DynamoDB for key-value, ElastiCache for hot lookups).",
        severity_weight=0.6,
        guidance="Evaluate whether this table benefits from Aurora's relational features. If it's purely key-value access, DynamoDB offers better cost/performance. If it's a hot lookup, ElastiCache is more appropriate.",
    ),
    # aurora-anti-06
    CatalogAntiPattern(
        pattern_id="aurora-anti-06",
        pattern_type="single-access-pattern-table",
        description="Table accessed by at most 2 distinct query patterns, all single-table, all by primary key or single column filter. This is a key-value workload wearing a relational costume — no query optimizer benefit.",
        severity_weight=0.5,
        guidance="Consider DynamoDB for simple key-value access or ElastiCache for sub-millisecond lookups. Aurora adds connection overhead and query parsing cost for access patterns that don't need them.",
    ),
    # aurora-anti-07
    CatalogAntiPattern(
        pattern_id="aurora-anti-07",
        pattern_type="high-volume-text-search",
        description="High-frequency text search patterns (LIKE '%', tsvector, ILIKE) on a text-heavy table (>40% text columns). While Aurora can handle text search, OpenSearch provides purpose-built inverted indexes, relevance scoring, and horizontal scaling for search-dominant workloads.",
        severity_weight=0.7,
        guidance="Offload text search to OpenSearch for better relevance, scalability, and latency. Keep Aurora as source of truth with async replication. Consider this especially if search latency or result quality matters.",
    ),
]


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
# Detection keyword tuples
# ==========================================================================

JOIN_KEYWORDS: tuple[str, ...] = (
    "join",
    "inner join",
    "left join",
    "right join",
    "full join",
    "cross join",
)

AGGREGATION_KEYWORDS: tuple[str, ...] = (
    "group by",
    "having",
    "sum(",
    "count(",
    "avg(",
    "min(",
    "max(",
)

SUBQUERY_KEYWORDS: tuple[str, ...] = (
    "exists",
    "not exists",
    "in (select",
    "in(select",
)

SUBQUERY_RE: re.Pattern = re.compile(
    r"""
    \bEXISTS\s*\(               # EXISTS (subquery)
    |
    \bNOT\s+EXISTS\s*\(         # NOT EXISTS (subquery)
    |
    \bIN\s*\(\s*SELECT\b        # IN (SELECT ...)
    """,
    re.IGNORECASE | re.VERBOSE,
)

PAGINATION_KEYWORDS: tuple[str, ...] = (
    "limit",
    "offset",
    "fetch first",
    "fetch next",
)

IN_LIST_KEYWORDS: tuple[str, ...] = (
    " in (",
    " in(",
)

TRANSACTION_WRITE_TYPES: frozenset[str] = frozenset({"INSERT", "UPDATE", "DELETE"})

# Query types to skip during analysis (admin/metadata commands, not application queries)
SKIP_QUERY_TYPES: frozenset[str] = frozenset({"OTHER", "SHOW", "DESCRIBE", "EXPLAIN", "SET"})

# Minimum CPS threshold for standard-crud pattern (avoids noise from rarely-used queries)
STANDARD_CRUD_MIN_CPS: float = 0.5

# Baseline pattern_match_score floor for tables with queries but no detected patterns.
# Represents "valid relational workload, nothing exceptional" — prevents zero scores
# that make tables look unsuitable when they're perfectly fine on Aurora.
BASELINE_PATTERN_MATCH_FLOOR: int = 45

# Thresholds
HIGH_FREQUENCY_PK_LOOKUP_CPS: float = 30.0
HIGH_INGEST_CPS_THRESHOLD: float = 50.0
CACHE_READ_CPS_THRESHOLD: float = 20.0
HIGH_VOLUME_TEXT_SEARCH_CPS: float = 10.0
TEXT_COLUMN_RATIO_THRESHOLD: float = 0.40

# Text data types for column ratio calculation
TEXT_DATA_TYPES: frozenset[str] = frozenset(
    {"text", "varchar", "character varying", "longtext", "clob", "nvarchar", "citext"}
)

# Text search indicators (for volume-based high-volume-text-search detection)
TEXT_SEARCH_KEYWORDS: tuple[str, ...] = (
    "like '%",
    "like N'%",
    "ilike '%",
    "to_tsvector",
    "plainto_tsquery",
    "match(",
    "match (",
)
