"""
DynamoDB Migration Pattern Catalog

Specialist-curated catalog of migration patterns for DynamoDB.
Each pattern describes a usage pattern that maps well to DynamoDB,
with a specialist-assigned base confidence score.

These scores are expert priors — they get validated and calibrated
through the decision trace feedback loop.

Detection signals are documented as comments above each pattern entry.
They describe what the detection logic in dynamodb_analysis_tools.py
looks for when matching queries to this pattern.
"""

from src.tools.analysis.pattern_catalog_base import CatalogAntiPattern, CatalogPattern

__all__ = ["CatalogAntiPattern", "CatalogPattern"]


# ==========================================================================
# DynamoDB Pattern Catalog
# ==========================================================================

DYNAMODB_PATTERNS: list[CatalogPattern] = [
    # Signals: single_row_select, primary_key_equality, high_frequency, low_latency
    CatalogPattern(
        pattern_id="dynamodb-01",
        pattern_type="key-value-lookup",
        description="Single-item lookups by primary key.",
        base_score=100,
        concerns=[
            "Large item sizes (>400KB) require splitting",
        ],
    ),
    # Signals: partition_key_equality_plus_range, between_clause, order_by_with_limit, bounded_result_set
    CatalogPattern(
        pattern_id="dynamodb-02",
        pattern_type="range-query",
        description="Queries that filter on a partition key with a range condition on a sort key. Maps directly to DynamoDB Query with KeyConditionExpression.",
        base_score=90,
        concerns=[
            "Result sets exceeding 1MB require pagination, which is not the same as SQL",
            "Complex filter expressions evaluated after read (consume RCUs)",
        ],
    ),
    # Signals: high_write_ratio, high_insert_frequency, simple_insert_structure, auto_increment_pk
    CatalogPattern(
        pattern_id="dynamodb-03",
        pattern_type="write-heavy-ingestion",
        description="High-throughput write workloads (INSERT/UPDATE).",
        base_score=85,
        concerns=[
            "Auto-increment PKs create hot partitions — need key redesign",
            "Batch writes limited to 25 items per request",
        ],
    ),
    # Signals: timestamp_in_where_or_order, range_on_datetime_column, insert_dominant, bounded_time_window_reads
    CatalogPattern(
        pattern_id="dynamodb-04",
        pattern_type="time-series-event-log",
        description="Time-ordered data with timestamp-based range reads. Maps to DynamoDB with entity ID as partition key and timestamp as sort key. TTL for automatic expiry.",
        base_score=85,
        concerns=[
            "Hot partitions if all writes go to 'latest' partition — consider time-bucketing",
            "Analytics queries (GROUP BY time) better served by aggregation or export to S3",
        ],
    ),
    # Signals: small_table, high_read_ratio, low_write_frequency, simple_pk_lookup
    CatalogPattern(
        pattern_id="dynamodb-05",
        pattern_type="metadata-config-store",
        description="Small reference tables with frequent reads. Also valid for high read+write metadata stores (e.g., feature flags, config, lookup tables). DynamoDB DAX or on-demand with low RCU cost.",
        base_score=90,
    ),
    # Signals: session_keywords, user_scoped_access, ttl_candidate, high_frequency_read_write
    CatalogPattern(
        pattern_id="dynamodb-06",
        pattern_type="session-store",
        description="User session data with high read/write throughput per user. TTL-based expiry is common but not required — manual deletes or application-managed cleanup are also valid patterns.",
        base_score=90,
        concerns=[
            "Without TTL, application must manage session cleanup to avoid unbounded growth",
        ],
    ),
    # Signals: simple_fk_join, parent_child_relationship, bounded_child_count
    CatalogPattern(
        pattern_id="dynamodb-07a",
        pattern_type="bounded-parent-child",
        description="Parent-child relationship where the cardinality can be reasoned about at design time. DynamoDB supports millions of items per partition — the bound is domain-driven (e.g., order → items, user → addresses), not a DynamoDB limitation. Large item collections may need creative sort key design for efficient range queries.",
        base_score=75,
        concerns=[
            "Requires access pattern analysis to determine embedding vs. item collection",
            "Large item collections need sort key design for efficient range queries",
        ],
    ),
    # Signals: junction_table, composite_fk_pk, many_to_many_relationship
    CatalogPattern(
        pattern_id="dynamodb-07b",
        pattern_type="many-to-many-junction",
        description="Many-to-many relationship via a junction table with composite PK of two FK columns. Needs GSI or adjacency list pattern in DynamoDB — more design work required.",
        base_score=65,
        concerns=[
            "Requires GSI or adjacency list pattern for bidirectional access",
            "May need data duplication for efficient queries from both sides",
        ],
    ),
    # Signals: co_accessed_tables, high_frequency_join, simple_fk_join
    CatalogPattern(
        pattern_id="dynamodb-07c",
        pattern_type="co-accessed-tables",
        description="Tables frequently accessed together in the same queries (co-access frequency > 50 calls/hour). Strong signal for denormalization into a single DynamoDB table.",
        base_score=70,
        concerns=[
            "Denormalization increases write amplification for updates",
        ],
    ),
    # Signals: self_referential_fk, hierarchy_traversal, graph_relationship
    CatalogPattern(
        pattern_id="dynamodb-07d",
        pattern_type="adjacency-list",
        description="Hierarchical or graph-like relationship (self-referential FK or multi-level hierarchy traversal). Maps to DynamoDB adjacency list pattern with PK/SK design.",
        base_score=60,
        concerns=[
            "Complex key design required — needs careful PK/SK modeling",
            "Deep hierarchies may require multiple queries or GSI",
        ],
    ),
    # Signals: low_frequency_write, insert_update_delete, cps_below_threshold
    CatalogPattern(
        pattern_id="dynamodb-08",
        pattern_type="low-frequency-write",
        description="Infrequent write operations (INSERT/UPDATE/DELETE below 0.5 cps). Low frequency does not mean low importance — batch loads, admin operations, and periodic jobs are common. DynamoDB on-demand handles sporadic writes without provisioned capacity waste.",
        base_score=70,
        concerns=[
            "Verify these writes are not part of a larger transaction that spans multiple tables",
            "Batch seed operations may benefit from BatchWriteItem (25 items per request)",
        ],
    ),
    # Signals: low_frequency_read, select_below_threshold, no_pattern_match
    CatalogPattern(
        pattern_id="dynamodb-09",
        pattern_type="low-frequency-read",
        description="Infrequent read operations (SELECT below 0.1 cps) that don't match any specific access pattern. May include admin queries, health checks, or reporting. DynamoDB Scan or Query with GSI depending on access pattern.",
        base_score=55,
        concerns=[
            "Full-table scans (no WHERE clause) require DynamoDB Scan — acceptable at low frequency",
            "COUNT(*) queries need application-level counting or a metadata item",
            "Schema design agent should propose a specific access pattern for each query",
        ],
    ),
]

# ==========================================================================
# DynamoDB Anti-Pattern Catalog
# ==========================================================================

DYNAMODB_ANTI_PATTERNS: list[CatalogAntiPattern] = [
    # Signals: no_where_clause, high_frequency, large_rows_examined
    CatalogAntiPattern(
        pattern_id="dynamodb-ap-01",
        pattern_type="frequent-full-scan",
        description="Source database has queries performing full table scans at high frequency. These queries lack proper indexes or filter conditions in the source database. In DynamoDB, these must be replaced with Query operations using partition keys or GSIs — Scan operations at >1/min are not acceptable.",
        severity_weight=0.5,
        guidance="The schema design agent will replace these scans with targeted Query or GetItem operations. Verify the generated access patterns cover all flagged queries. Remaining scans may need a GSI or access pattern redesign.",
    ),
    # Signals: group_by_multiple_columns, having_clause, multi_table_aggregation
    CatalogAntiPattern(
        pattern_id="dynamodb-ap-02",
        pattern_type="complex-aggregation",
        description="Complex GROUP BY / HAVING / multi-table aggregations. DynamoDB doesn't support server-side aggregation — these need to move to application layer, pre-computed aggregates, or a separate analytics store.",
        severity_weight=0.7,
        guidance="Pre-compute aggregates on write (DynamoDB Streams + Lambda), or export to S3/Athena for analytics.",
    ),
    # Signals: no_limit_clause, high_rows_returned
    CatalogAntiPattern(
        pattern_id="dynamodb-ap-03",
        pattern_type="unbounded-result-set",
        description="Queries returning large unbounded result sets without LIMIT. DynamoDB returns max 1MB per Query/Scan — pagination is required.",
        severity_weight=0.2,
        guidance="Add pagination. DynamoDB handles this natively with LastEvaluatedKey.",
    ),
    # Signals: multi_table_write_in_transaction, high_affected_row_count
    CatalogAntiPattern(
        pattern_id="dynamodb-ap-04",
        pattern_type="wide-transaction",
        description="Transactions spanning many items or tables. DynamoDB TransactWriteItems supports up to 100 items across tables, but at higher latency and cost.",
        severity_weight=0.3,
        guidance="Transactions up to 100 items work well. Larger scopes need saga pattern or eventual consistency design.",
    ),
]


# Lookup helpers
PATTERN_BY_ID: dict[str, CatalogPattern] = {p.pattern_id: p for p in DYNAMODB_PATTERNS}
ANTI_PATTERN_BY_ID: dict[str, CatalogAntiPattern] = {
    ap.pattern_id: ap for ap in DYNAMODB_ANTI_PATTERNS
}


# DynamoDB-specific score weights
# Pattern match weighted highest because catalog scores are specialist-curated
# and pattern fit is the strongest signal for DynamoDB suitability.
# Performance weighted low because DynamoDB value is maintaining performance
# at scale, not necessarily improving already-fast queries.
DYNAMODB_SCORE_WEIGHTS: dict[str, float] = {
    "pattern_match": 0.60,
    "complexity": 0.20,
    "performance": 0.05,
    "cost": 0.15,
}
