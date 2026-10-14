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

CACHE_ENGINES: frozenset[str] = frozenset({"elasticache"})

# Hot-read floor (calls/s), inclusive: a read at exactly this rate qualifies.
# Starting value from #296; tune with data. On the two reference samples every
# overlay candidate is either >= 1.26 calls/s or <= 0.95.
HOT_READ_MIN_CALLS_PER_SECOND = 1.0
