"""
Aurora MySQL Pattern Catalog

MySQL-specific patterns that complement the shared relational patterns from
aurora_common_pattern_catalog.py. These detect MySQL-native features
(JSON_EXTRACT, FULLTEXT MATCH AGAINST, ON DUPLICATE KEY, GROUP_CONCAT,
stored routines, multi-table UPDATE syntax) that make Aurora MySQL the ideal target.
"""

from src.tools.analysis.pattern_catalog_base import CatalogPattern

# ==========================================================================
# Aurora MySQL-Specific Pattern Catalog
# ==========================================================================

AURORA_MYSQL_PATTERNS: list[CatalogPattern] = [
    # Signals: JSON_EXTRACT, JSON_CONTAINS, ->> operator
    CatalogPattern(
        pattern_id="aurora-mysql-01",
        pattern_type="json-document-query",
        description="JSON document queries using JSON_EXTRACT, JSON_CONTAINS, or ->> operator. Aurora MySQL 8.0+ provides multi-valued indexes on JSON arrays and generated columns for indexing JSON paths.",
        base_score=85,
        concerns=[
            "JSON column indexes require generated columns or multi-valued index syntax.",
        ],
    ),
    # Signals: MATCH ... AGAINST, FULLTEXT index usage
    CatalogPattern(
        pattern_id="aurora-mysql-02",
        pattern_type="fulltext-search",
        description="Full-text search using MATCH ... AGAINST with FULLTEXT indexes. Aurora MySQL provides built-in boolean and natural language full-text modes suitable for moderate search workloads.",
        base_score=80,
        concerns=[
            "High-volume search workloads may still benefit from dedicated OpenSearch.",
            "InnoDB FULLTEXT indexes have higher write overhead than MyISAM.",
        ],
    ),
    # Signals: ON DUPLICATE KEY UPDATE
    CatalogPattern(
        pattern_id="aurora-mysql-03",
        pattern_type="upsert-duplicate-key",
        description="UPSERT using INSERT ... ON DUPLICATE KEY UPDATE. Aurora MySQL handles atomic insert-or-update with row-level locking on the unique/primary key.",
        base_score=80,
        concerns=[
            "Auto-increment gaps may occur with ON DUPLICATE KEY UPDATE.",
        ],
    ),
    # Signals: GROUP_CONCAT, string aggregation patterns
    CatalogPattern(
        pattern_id="aurora-mysql-04",
        pattern_type="group-concat-pivot",
        description="String aggregation using GROUP_CONCAT for pivoting and denormalized output. Aurora MySQL executes this server-side, avoiding multiple round-trips for client-side aggregation.",
        base_score=75,
        concerns=[
            "Default group_concat_max_len (1024) may truncate results; increase as needed.",
        ],
    ),
    # Signals: CALL procedure, DELIMITER patterns
    CatalogPattern(
        pattern_id="aurora-mysql-05",
        pattern_type="stored-routine",
        description="Stored procedures and functions (CALL, CREATE PROCEDURE). Aurora MySQL supports stored routines for encapsulating business logic server-side — reducing network round-trips for multi-step operations.",
        base_score=70,
        concerns=[
            "Stored routines increase operational complexity and are harder to version-control.",
            "Consider migrating complex logic to application layer for testability.",
        ],
    ),
    # Signals: UPDATE t1 JOIN t2 SET ... (MySQL multi-table syntax)
    CatalogPattern(
        pattern_id="aurora-mysql-06",
        pattern_type="multi-table-update",
        description="Multi-table UPDATE using MySQL's JOIN syntax (UPDATE t1 JOIN t2 SET ...). Aurora MySQL executes this atomically in a single statement — no NoSQL equivalent exists.",
        base_score=80,
        concerns=[
            "Large multi-table updates may hold locks for extended periods.",
        ],
    ),
]


# ==========================================================================
# Lookup helpers
# ==========================================================================

MYSQL_PATTERN_BY_ID: dict[str, CatalogPattern] = {p.pattern_id: p for p in AURORA_MYSQL_PATTERNS}


# ==========================================================================
# Detection keyword tuples
# ==========================================================================

JSON_EXTRACT_KEYWORDS: tuple[str, ...] = (
    "json_extract(",
    "json_contains(",
    "json_value(",
    "json_set(",
    "json_array(",
    "json_object(",
    "->>",
    "->",
)

FULLTEXT_MATCH_KEYWORDS: tuple[str, ...] = (
    "match(",
    "match (",
    "against(",
    "against (",
)

DUPLICATE_KEY_KEYWORDS: tuple[str, ...] = ("on duplicate key", "replace into")

GROUP_CONCAT_KEYWORDS: tuple[str, ...] = ("group_concat(",)

STORED_ROUTINE_KEYWORDS: tuple[str, ...] = (
    "call ",
    "delimiter",
    "create procedure",
    "create function",
)

MULTI_TABLE_UPDATE_KEYWORDS: tuple[str, ...] = ("update",)  # combined with join detection
