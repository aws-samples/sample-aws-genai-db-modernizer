"""
DocumentDB Migration Pattern Catalog

Specialist-curated catalog of migration patterns for Amazon DocumentDB.
Informed by the Relational DB to MongoDB Migration Guide — DocumentDB Edition
and validated against real-world customer PoC results (March 2026).

Key insight: DocumentDB should denormalize more aggressively than native MongoDB
because $lookup lacks correlated subqueries, and $ne/$nin cause full scans.
"""

from src.tools.analysis.pattern_catalog_base import CatalogAntiPattern, CatalogPattern

__all__ = ["CatalogAntiPattern", "CatalogPattern"]


DOCUMENTDB_PATTERNS: list[CatalogPattern] = [
    # Signals: sparse_columns (>30% nullable), type_discriminator_column, text/blob columns
    CatalogPattern(
        pattern_id="documentdb-01",
        pattern_type="content-management",
        description=(  # nosemgrep: string-concat-in-list
            "Polymorphic content tables with variable schemas — articles, pages, media "
            "with different field sets per type. Natural fit for flexible document model."
        ),
        base_score=90,
        concerns=["16MB document limit for large text/blob content"],
    ),
    # Signals: many_columns (>15), many_nullable, json_columns, parent_child_variants
    CatalogPattern(
        pattern_id="documentdb-02",
        pattern_type="product-catalog",
        description=(  # nosemgrep: string-concat-in-list
            "Product tables with many optional attributes that vary by category. "
            "Relational schemas use wide tables or EAV; DocumentDB uses flexible documents."
        ),
        base_score=85,
        concerns=[
            "Wildcard indexes ($**) not supported — create targeted indexes per queryable attribute",
        ],
    ),
    # Signals: sparse_columns (>30% nullable), type/category discriminator, eav_tables
    CatalogPattern(
        pattern_id="documentdb-03",
        pattern_type="polymorphic-data",
        description=(  # nosemgrep: string-concat-in-list
            "Tables with sparse columns or EAV pattern indicating polymorphic entities. "
            "DocumentDB stores each variant as a document with only its relevant fields."
        ),
        base_score=80,
        concerns=[
            (  # nosemgrep: string-concat-in-list
                "Wildcard indexes ($**) not supported in any DocumentDB version — "
                "flatten queryable attributes to top-level with targeted compound indexes"
            ),
        ],
    ),
    # Signals: parent_child_fk, bounded_child_rows, co_access, json_extract
    CatalogPattern(
        pattern_id="documentdb-04",
        pattern_type="nested-document",
        description=(  # nosemgrep: string-concat-in-list
            "Parent-child FK relationships with bounded child cardinality suitable for "
            "embedding as nested arrays. Eliminates JOINs entirely."
        ),
        base_score=85,
        concerns=[
            "Embedded arrays must stay bounded — unbounded growth hits 16MB limit",
            "Updates to embedded items rewrite the entire parent document",
        ],
    ),
    # Signals: group_by + aggregate_functions (SUM/COUNT/AVG), reporting queries
    CatalogPattern(
        pattern_id="documentdb-05",
        pattern_type="aggregation-pipeline",
        description=(  # nosemgrep: string-concat-in-list
            "Queries using GROUP BY with aggregate functions that map to DocumentDB's "
            "aggregation pipeline ($group, $match, $project, $sort)."
        ),
        base_score=75,
        concerns=[
            "$stdDevPop/$stdDevSamp not supported — compute in application layer",
            "$facet not supported — run multiple pipelines in parallel instead",
            "$setWindowFields not supported — use Computed Pattern or application layer",
        ],
    ),
    # Signals: json/jsonb columns, high column count with many nullable
    CatalogPattern(
        pattern_id="documentdb-06",
        pattern_type="flexible-schema",
        description=(  # nosemgrep: string-concat-in-list
            "Tables with JSON/JSONB columns or high column count with many nullable "
            "fields — data is already semi-structured and maps directly to documents."
        ),
        base_score=80,
        concerns=[
            "JSON_EXTRACT at query time should be replaced by top-level fields at write time"
        ],
    ),
    # Signals: joins with small reference tables (row_count < 1000) for display names
    CatalogPattern(
        pattern_id="documentdb-07",
        pattern_type="extended-reference",
        description=(  # nosemgrep: string-concat-in-list
            "Queries joining large transactional tables with small reference/lookup tables "
            "(country, merchant, payment_type) just to get display names. Embed the few referenced "
            "fields directly — especially valuable in DocumentDB where $lookup lacks correlated subqueries."
        ),
        base_score=85,
        concerns=[
            "Denormalized fields must be updated when reference data changes (rare for lookups)"
        ],
    ),
    # Signals: multiple SUM/COUNT/MIN/MAX + JOIN, high read:write ratio on aggregated data
    CatalogPattern(
        pattern_id="documentdb-08",
        pattern_type="write-time-aggregation",
        description=(  # nosemgrep: string-concat-in-list
            "Complex aggregation queries (multiple SUM/COUNT/MIN/MAX with JOINs) that can "
            "be pre-computed at write time via CDC pipeline and stored as documents. Eliminates "
            "real-time aggregation entirely. Validated: 65x speedup in AnyCompany PoC."
        ),
        base_score=90,
        concerns=[
            "Requires CDC pipeline (DMS/Lambda) to maintain pre-aggregated documents",
            "Stale data window between write and CDC propagation",
        ],
    ),
]

DOCUMENTDB_ANTI_PATTERNS: list[CatalogAntiPattern] = [
    # Signals: 3+ table JOINs at high frequency
    CatalogAntiPattern(
        pattern_id="documentdb-anti-01",
        pattern_type="heavy-cross-collection-joins",
        description=(  # nosemgrep: string-concat-in-list
            "Queries joining 3+ tables at high frequency. DocumentDB $lookup only supports "
            "simple equality (no correlated subqueries). Real-world: 4-7 table JOINs caused 72% I/O wait."
        ),
        severity_weight=0.6,
        guidance=(  # nosemgrep: string-concat-in-list
            "Pre-denormalize via Extended Reference Pattern or write-time aggregation. "
            "Never chain $lookup operations."
        ),
    ),
    # Signals: parent-child FK with unbounded child growth
    CatalogAntiPattern(
        pattern_id="documentdb-anti-02",
        pattern_type="unbounded-array-growth",
        description="Embedded arrays that grow without limit hit the 16MB document size limit.",
        severity_weight=0.7,
        guidance="Use Bucket Pattern (bounded arrays) or Reference Pattern for unbounded relationships.",
    ),
    # Signals: high-frequency UPDATE on single fields in large documents
    CatalogAntiPattern(
        pattern_id="documentdb-anti-03",
        pattern_type="high-frequency-small-updates",
        description="Frequent updates to single fields in large documents rewrites the entire document.",
        severity_weight=0.4,
        guidance="Extract frequently-updated fields into a separate small document/collection.",
    ),
    # Signals: self-referential FK, recursive CTE, hierarchical queries
    CatalogAntiPattern(
        pattern_id="documentdb-anti-04",
        pattern_type="graph-traversal-hierarchy",
        description=(  # nosemgrep: string-concat-in-list
            "Self-referential FKs or recursive queries. $graphLookup is NOT supported in "
            "any DocumentDB version (including 8.0)."
        ),
        severity_weight=0.8,
        guidance=(  # nosemgrep: string-concat-in-list
            "Use Materialized Path pattern (store ancestor path as string), or "
            "pre-flatten hierarchies at write time. Consider Neptune for true graph workloads."
        ),
    ),
    # Signals: NOT IN, <>, !=, NOT EXISTS in WHERE clauses
    CatalogAntiPattern(
        pattern_id="documentdb-anti-05",
        pattern_type="negation-heavy-queries",
        description=(  # nosemgrep: string-concat-in-list
            "Queries with NOT IN, <>, !=, NOT EXISTS. These map to $ne/$nin/$nor which "
            "cause full collection scans in DocumentDB (no index usage)."
        ),
        severity_weight=0.4,
        guidance="Redesign hot query paths to use positive equality operators ($eq, $in).",
    ),
    # Signals: multi-dimensional GROUP BY / HAVING with multiple aggregation axes
    CatalogAntiPattern(
        pattern_id="documentdb-anti-06",
        pattern_type="faceted-analytics",
        description=(  # nosemgrep: string-concat-in-list
            "Multi-dimensional analytics queries. $facet is NOT supported in any "
            "DocumentDB version."
        ),
        severity_weight=0.5,
        guidance="Run multiple aggregation pipelines in parallel at application layer.",
    ),
    # Signals: ROW_NUMBER, RANK, DENSE_RANK, LAG, LEAD, running totals
    CatalogAntiPattern(
        pattern_id="documentdb-anti-07",
        pattern_type="window-functions",
        description=(  # nosemgrep: string-concat-in-list
            "SQL window functions (ROW_NUMBER, RANK, running totals). $setWindowFields "
            "is NOT supported in any DocumentDB version."
        ),
        severity_weight=0.5,
        guidance="Use Computed Pattern (pre-calculated fields) or application-layer computation.",
    ),
    # Signals: correlated subqueries (SELECT in SELECT, EXISTS with correlated ref)
    CatalogAntiPattern(
        pattern_id="documentdb-anti-08",
        pattern_type="correlated-subquery-joins",
        description=(  # nosemgrep: string-concat-in-list
            "Queries with correlated subqueries. DocumentDB $lookup does NOT support "
            "let/pipeline syntax for correlated joins. Must be pre-denormalized."
        ),
        severity_weight=0.7,
        guidance=(  # nosemgrep: string-concat-in-list
            "Pre-denormalize at write time. Use Extended Reference Pattern to embed "
            "frequently co-accessed fields."
        ),
    ),
]

PATTERN_BY_ID: dict[str, CatalogPattern] = {p.pattern_id: p for p in DOCUMENTDB_PATTERNS}
ANTI_PATTERN_BY_ID: dict[str, CatalogAntiPattern] = {
    ap.pattern_id: ap for ap in DOCUMENTDB_ANTI_PATTERNS
}

# DocumentDB-specific score weights
# Pattern match weighted high because document patterns are strong signals.
# Complexity weighted moderate — schema flexibility reduces migration effort.
# Performance and cost balanced — instance-based pricing matters.
DOCUMENTDB_SCORE_WEIGHTS: dict[str, float] = {
    "pattern_match": 0.45,
    "complexity": 0.25,
    "performance": 0.15,
    "cost": 0.15,
}
