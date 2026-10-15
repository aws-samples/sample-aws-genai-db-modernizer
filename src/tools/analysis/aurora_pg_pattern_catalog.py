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
# Detection: the PostgreSQL regexes live in ``src.tools.sql.query_facts``
# (#491), one engine-neutral definition per fact.
# ==========================================================================
