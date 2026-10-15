"""
DocumentDB Migration Pattern Catalog

Specialist-curated catalog of migration patterns for Amazon DocumentDB.
Informed by the Relational DB to MongoDB Migration Guide — DocumentDB Edition
and validated against real-world customer PoC results (March 2026).

Key insight: DocumentDB should denormalize more aggressively than native MongoDB
because every $lookup is a read-time join (correlated $lookup with let/pipeline
needs 8.0.2+ on an instance-based cluster), and $ne/$nin cause full scans.

Version facts (AWS DocumentDB supported-APIs page): $graphLookup (8.0),
$facet (8.0.2+), $setWindowFields with the rank operators (8.0.2+),
$stdDevPop/$stdDevSamp (8.0.1+) and correlated $lookup (8.0.2+) run only on
8.0 instance-based clusters, not on 5.0 or earlier or on elastic clusters.
The anti-patterns below keep their detection and weights: whether a design
may assume 8.0 is a routing decision for the rule catalog (#489).

The entries live in ``src/catalog/patterns/<pattern_type>.toml`` (ADR-031 Decision 8,
#492): ``catalog_patterns``/``catalog_anti_patterns`` build the lists below from them,
under the names the analysis tools import. Edit the TOML file, not this module.
"""

from src.tools.analysis.pattern_catalog_base import (
    CatalogAntiPattern,
    CatalogPattern,
    catalog_anti_patterns,
    catalog_patterns,
)

__all__ = ["CatalogAntiPattern", "CatalogPattern"]


DOCUMENTDB_PATTERNS: list[CatalogPattern] = catalog_patterns("documentdb")

DOCUMENTDB_ANTI_PATTERNS: list[CatalogAntiPattern] = catalog_anti_patterns("documentdb")

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
