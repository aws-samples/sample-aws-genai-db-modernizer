"""
Aurora PostgreSQL Pattern Catalog

PG-specific patterns that complement the shared relational patterns from
aurora_common_pattern_catalog.py. These detect PostgreSQL-native features
(CTEs, window functions, JSONB, arrays, LATERAL, tsvector) that make
Aurora PostgreSQL the ideal target.

The entries live in ``src/catalog/patterns/<pattern_type>.toml`` (ADR-031 Decision 8,
#492): ``catalog_patterns``/``catalog_anti_patterns`` build the lists below from them,
under the names the analysis tools import. Edit the TOML file, not this module.
"""

import re

from src.tools.analysis.pattern_catalog_base import CatalogPattern, catalog_patterns

# ==========================================================================
# Aurora PG-Specific Pattern Catalog
# ==========================================================================

AURORA_PG_PATTERNS: list[CatalogPattern] = catalog_patterns("aurora_pg")


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
