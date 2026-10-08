"""Cache-layer policy shared by routing, analysis, waves and deliverables.

These are the engines the assessment currently uses only as cache overlays.
This describes our routing policy, not the durability capabilities of a service.
MemoryDB is a durable database and is not a cache-only engine in this policy.
The UI copy in cacheLayer.js is checked against this set by a sync test.
"""

CACHE_ENGINES: frozenset[str] = frozenset({"elasticache"})

# Hot-read floor (calls/s), inclusive. Tune with workload evidence.
HOT_READ_MIN_CALLS_PER_SECOND = 1.0
