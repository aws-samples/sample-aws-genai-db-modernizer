"""
Aurora MySQL Pattern Catalog

MySQL-specific patterns that complement the shared relational patterns from
aurora_common_pattern_catalog.py. These detect MySQL-native features
(JSON_EXTRACT, FULLTEXT MATCH AGAINST, ON DUPLICATE KEY, GROUP_CONCAT,
stored routines, multi-table UPDATE syntax) that make Aurora MySQL the ideal target.

The entries live in ``src/catalog/patterns/<pattern_type>.toml`` (ADR-031 Decision 8,
#492): ``catalog_patterns``/``catalog_anti_patterns`` build the lists below from them,
under the names the analysis tools import. Edit the TOML file, not this module.
"""

from src.tools.analysis.pattern_catalog_base import CatalogPattern, catalog_patterns

# ==========================================================================
# Aurora MySQL-Specific Pattern Catalog
# ==========================================================================

AURORA_MYSQL_PATTERNS: list[CatalogPattern] = catalog_patterns("aurora_mysql")


# ==========================================================================
# Lookup helpers
# ==========================================================================

MYSQL_PATTERN_BY_ID: dict[str, CatalogPattern] = {p.pattern_id: p for p in AURORA_MYSQL_PATTERNS}


# ==========================================================================
# Detection: the MySQL keyword tables live in ``src.tools.sql.query_facts``
# (#491), one engine-neutral definition per fact.
# ==========================================================================
