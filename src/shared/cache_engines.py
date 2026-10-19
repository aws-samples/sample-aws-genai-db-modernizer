"""The cache-layer engines and the hot-read floor, declared once (#398).

A cache-layer engine fronts hot reads and owns no query. This set is the single
declaration read by the cache overlay (routing, reality check, validator,
post-schema router), the migration-wave buckets, the report's role labels and,
through a sync test, the UI copy in ``src/ui/src/utils/cacheLayer.js``.

MemoryDB is not a modernizer engine: since Valkey 9.0, ElastiCache offers
native durability through a Multi-AZ transaction log, so the two need no
separate treatment. Whether a durable ElastiCache can be a system of record is
tracked separately (#401); today ElastiCache stays cache-only.
"""

from __future__ import annotations

from src.shared import catalog

# The active engines whose catalog ``role`` is ``cache`` (src/catalog/engines, #492).
CACHE_ENGINES: frozenset[str] = catalog.engine_ids(status=catalog.ACTIVE, role="cache")
# The cache engine a customer cache request uses when the analysis names none (#1081):
# the catalog's one active cache-role engine. The rule is "exactly one": a second cache
# engine needs a catalog field to say which is the default, so it fails here, at import.
if len(CACHE_ENGINES) != 1:
    raise catalog.CatalogError(
        f"exactly one active cache engine is the default cache engine, not {sorted(CACHE_ENGINES)}"
    )
(DEFAULT_CACHE_ENGINE,) = CACHE_ENGINES


def cache_threshold(name: str) -> int | float:
    """The cache layer's ``[thresholds.<name>]`` (its engine file, #921): the one value
    every cache-layer engine gives it (a ``CatalogError`` if two disagree)."""
    values = {catalog.engine_threshold(engine, name) for engine in sorted(CACHE_ENGINES)}
    if len(values) != 1:
        raise catalog.CatalogError(f"cache engines disagree on thresholds.{name}: {values}")
    return values.pop()


# Hot-read floor (calls/s), inclusive: a read at exactly this rate qualifies.
# Starting value from #296; tune with data. On the two reference samples every
# overlay candidate is either >= 1.26 calls/s or <= 0.95.
HOT_READ_MIN_CALLS_PER_SECOND = cache_threshold("hot_read_min_calls_per_second")
