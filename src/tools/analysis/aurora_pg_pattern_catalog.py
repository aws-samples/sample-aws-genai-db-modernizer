"""
Aurora PostgreSQL Pattern Catalog

PG-specific patterns that complement the shared relational patterns from
aurora_common_pattern_catalog.py. These detect PostgreSQL-native features
(CTEs, window functions, JSONB, arrays, LATERAL, tsvector) that make
Aurora PostgreSQL the ideal target.
"""

import re

from src.tools.analysis.pattern_catalog_base import CatalogPattern

# ==========================================================================
# Aurora PG-Specific Pattern Catalog
# ==========================================================================

AURORA_PG_PATTERNS: list[CatalogPattern] = [
    # Signals: WITH RECURSIVE, hierarchical data traversal
    CatalogPattern(
        pattern_id="aurora-pg-01",
        pattern_type="cte-recursive",
        description="Recursive CTEs (WITH RECURSIVE) for hierarchical data traversal — org charts, category trees, bill of materials. Aurora PG executes these natively; no NoSQL engine provides equivalent functionality without application-level graph walks.",
        base_score=95,
        concerns=[
            "Deep recursion (>100 levels) may require LIMIT on recursion depth.",
        ],
    ),
    # Signals: OVER(), PARTITION BY, ROW_NUMBER/RANK/LAG/LEAD
    CatalogPattern(
        pattern_id="aurora-pg-02",
        pattern_type="window-function",
        description="Window functions (OVER, PARTITION BY, ROW_NUMBER, RANK, LAG, LEAD) for running totals, rankings, and row-level analytics. Aurora PG executes these in a single pass without self-joins.",
        base_score=90,
        concerns=[
            "Large partition sizes may increase memory usage during window computation.",
        ],
    ),
    # Signals: ->>, @>, jsonb_agg, jsonb_build_object
    CatalogPattern(
        pattern_id="aurora-pg-03",
        pattern_type="jsonb-operations",
        description="JSONB operations (->>, @>, jsonb_agg, jsonb_build_object) for semi-structured data within relational context. Aurora PG provides GIN-indexed JSONB queries — combining document flexibility with relational joins.",
        base_score=85,
        concerns=[
            "Deeply nested JSONB paths may not be indexable; consider flattening hot paths.",
        ],
    ),
    # Signals: ANY(ARRAY[...]), array_agg, unnest
    CatalogPattern(
        pattern_id="aurora-pg-04",
        pattern_type="array-operations",
        description="Array operations (ANY, array_agg, unnest) for multi-value attributes. Aurora PG handles array containment queries with GIN indexes — avoiding junction tables for simple multi-value cases.",
        base_score=80,
        concerns=[
            "Very large arrays (>1000 elements) may degrade GIN index performance.",
        ],
    ),
    # Signals: to_tsvector, ts_query, @@ operator
    CatalogPattern(
        pattern_id="aurora-pg-05",
        pattern_type="full-text-native",
        description="Native full-text search using to_tsvector, ts_query, and @@ operator. Aurora PG provides built-in full-text with GIN indexes — suitable for moderate search workloads without a separate search engine.",
        base_score=75,
        concerns=[
            "High-volume search workloads may still benefit from dedicated OpenSearch.",
            "Custom dictionaries and language-specific stemming require configuration.",
        ],
    ),
    # Signals: LATERAL, complex subquery in FROM
    CatalogPattern(
        pattern_id="aurora-pg-06",
        pattern_type="lateral-join",
        description="LATERAL joins for correlated subqueries in the FROM clause. Aurora PG executes these efficiently — each row from the left side feeds the lateral subquery, enabling top-N-per-group patterns.",
        base_score=85,
        concerns=[
            "Large outer tables with expensive lateral subqueries may need index support.",
        ],
    ),
    # Signals: ON CONFLICT DO UPDATE, RETURNING
    CatalogPattern(
        pattern_id="aurora-pg-07",
        pattern_type="upsert-conflict",
        description="UPSERT with ON CONFLICT DO UPDATE and RETURNING clause. Aurora PG handles atomic insert-or-update in a single statement with row-level locking — no read-modify-write cycle needed.",
        base_score=80,
        concerns=[
            "High-concurrency upserts on the same key may cause retry loops.",
        ],
    ),
]


# ==========================================================================
# Lookup helpers
# ==========================================================================

PG_PATTERN_BY_ID: dict[str, CatalogPattern] = {p.pattern_id: p for p in AURORA_PG_PATTERNS}


# ==========================================================================
# Detection compiled regex patterns
# ==========================================================================

# CTE RECURSIVE: WITH followed by optional whitespace then RECURSIVE keyword
CTE_RECURSIVE_RE: re.Pattern = re.compile(
    r"\bWITH\s+RECURSIVE\b",
    re.IGNORECASE,
)

# Window functions: named window functions OR ) OVER ( OR OVER (PARTITION|ORDER BY
WINDOW_FUNCTION_RE: re.Pattern = re.compile(
    r"""
    \b(?:ROW_NUMBER|RANK|DENSE_RANK|LAG|LEAD|NTILE|FIRST_VALUE|LAST_VALUE|CUME_DIST|PERCENT_RANK)\s*\(
    |
    \)\s*OVER\s*\(
    |
    \bOVER\s*\(\s*(?:PARTITION\s+BY|ORDER\s+BY)
    """,
    re.IGNORECASE | re.VERBOSE,
)

# JSONB operations:
#   - ->> operator
#   - jsonb_* function names
#   - ::jsonb cast
#   - @> followed by a JSON object/string literal (single-quoted, NOT ARRAY)
JSONB_RE: re.Pattern = re.compile(
    r"""
    ->>
    |
    \bjsonb_(?:agg|build_object|build_array|each|each_text|array_elements|array_elements_text|
               populate_record|typeof|strip|pretty|set|insert|delete|path_exists|path_query)\s*\(
    |
    ::jsonb\b
    |
    @>\s*'
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Array operations:
#   - ANY(ARRAY[...])
#   - array_agg(
#   - unnest(
#   - ARRAY[...]
#   - @> ARRAY[...] (array containment, NOT json)
ARRAY_RE: re.Pattern = re.compile(
    r"""
    \bANY\s*\(\s*ARRAY\b
    |
    \barray_agg\s*\(
    |
    \bunnest\s*\(
    |
    \bARRAY\s*\[
    |
    @>\s*ARRAY\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Full-text search: tsvector/tsquery functions and @@ operator in tsquery context
TSVECTOR_RE: re.Pattern = re.compile(
    r"""
    \b(?:to_tsvector|to_tsquery|plainto_tsquery|phraseto_tsquery|websearch_to_tsquery)\s*\(
    |
    \btsvector\b
    |
    \bts_rank\s*\(
    |
    @@
    """,
    re.IGNORECASE | re.VERBOSE,
)

# LATERAL joins: requires JOIN LATERAL or comma LATERAL or LATERAL( with word boundary
LATERAL_RE: re.Pattern = re.compile(
    r"""
    \bJOIN\s+LATERAL\s*\(
    |
    ,\s*LATERAL\s*\(
    |
    \bLATERAL\s*\(
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Upsert: ON CONFLICT keyword OR RETURNING used as a SQL clause.
# RETURNING as a SQL clause follows DML (not preceded by AS keyword).
# Matches: RETURNING id, RETURNING *, RETURNING col1, col2
# Does NOT match: AS returning FROM (alias usage)
UPSERT_RE: re.Pattern = re.compile(
    r"""
    \bON\s+CONFLICT\b
    |
    (?<!AS\s)(?<!\bAS\b)\bRETURNING\s+[\w*]
    """,
    re.IGNORECASE | re.VERBOSE,
)
