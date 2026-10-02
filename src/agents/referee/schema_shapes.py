"""Which target engines are caches.

Design normalisation — reading each engine's own design field (``key_designs``,
``collections``, ``index_designs``, …) — lives in
``synthesis_report.schema_table_defs``. This module only answers whether an
engine is a cache fronting a primary store, which synthesis needs in several
places: a cache is never a source table's migration destination, it counts
towards HYBRID_WITH_CACHE, and it has its own role in the architecture.
"""

from __future__ import annotations

# Membership, not a substring test on the engine name: "cache" in "elasticache"
# happens to hold but says nothing about memorydb.
CACHE_ENGINES = frozenset({"elasticache", "memorydb"})


def is_cache_engine(engine: str) -> bool:
    """Whether *engine* is a cache fronting a primary store."""
    name = engine.strip().lower()
    return name in CACHE_ENGINES or "cache" in name
