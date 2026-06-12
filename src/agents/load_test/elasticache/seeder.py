"""ElastiCache (Valkey/Redis) seeder for load testing.

Seeds Redis key designs from the schema output into the provisioned
replication group using redis-py with TLS.
"""
import time

import structlog

from src.agents.load_test.base import BaseSeeder
from src.agents.load_test.models import SeedManifest

logger = structlog.get_logger()

DEFAULT_ITEMS_PER_KEY_DESIGN = 1000


class ElastiCacheSeeder(BaseSeeder):
    """Seeds a Valkey/Redis cluster with synthetic data based on key designs."""

    def __init__(self, region: str = "us-east-1"):
        self.region = region

    def seed(self, schema_output: dict, max_items_per_table: int = 10_000) -> SeedManifest:
        """Seed all key designs that have in-scope access patterns.

        Connects to the cluster endpoint from the provisioned infrastructure
        (passed via schema_output enrichment or environment variable).
        """
        import os

        import redis  # type: ignore[import-untyped]

        endpoint = os.environ.get("ELASTICACHE_ENDPOINT", "")
        port = int(os.environ.get("ELASTICACHE_PORT", "6379"))

        if not endpoint:
            # Try to get from schema_output (enriched by handler)
            endpoint = schema_output.get("_cluster_endpoint", "")
            port = schema_output.get("_cluster_port", 6379)

        if not endpoint:
            raise ValueError(
                "ELASTICACHE_ENDPOINT not set and no _cluster_endpoint in schema_output"
            )

        logger.info("connecting_to_cluster", endpoint=endpoint, port=port)
        client = redis.Redis(
            host=endpoint,
            port=port,
            ssl=True,
            decode_responses=True,
        )

        start = time.time()
        key_designs = schema_output.get("key_designs", [])
        resources: dict = {}
        total_items = 0

        for kd in key_designs:
            key_pattern = kd.get("key_pattern", "")
            data_type = kd.get("data_type", "string")
            est_keys = min(
                kd.get("estimated_key_count") or DEFAULT_ITEMS_PER_KEY_DESIGN, max_items_per_table
            )
            items_to_seed = min(est_keys, max_items_per_table)

            seeded = self._seed_key_design(client, kd, items_to_seed)
            resources[key_pattern] = {
                "key_pattern": key_pattern,
                "data_type": data_type,
                "items_seeded": seeded,
                "ttl_seconds": kd.get("ttl_seconds"),
            }
            total_items += seeded
            logger.info("seeded_key_design", key_pattern=key_pattern, items=seeded)

        client.close()
        duration = time.time() - start

        return SeedManifest(
            resources=resources,
            total_items=total_items,
            duration_seconds=round(duration, 2),
        )

    def _seed_key_design(self, client, kd: dict, count: int) -> int:
        """Seed a single key design with synthetic data."""
        data_type = kd.get("data_type", "string")
        key_pattern = kd.get("key_pattern", "key:{id}")
        ttl = kd.get("ttl_seconds")

        pipe = client.pipeline(transaction=False)
        seeded = 0

        for i in range(1, count + 1):
            key = self._interpolate_key(key_pattern, i)

            match data_type:
                case "string":
                    pipe.set(key, f"value_{i}")
                case "hash":
                    fields = kd.get("fields_mapped", ["field1", "field2", "field3"])
                    mapping = {f: f"val_{i}_{j}" for j, f in enumerate(fields[:10])}
                    pipe.hset(key, mapping=mapping)
                case "list":
                    pipe.rpush(key, *[f"item_{j}" for j in range(min(5, count))])
                case "set":
                    pipe.sadd(key, *[f"member_{j}" for j in range(min(10, count))])
                case "sorted_set":
                    members = {f"member_{j}": float(j) for j in range(min(10, count))}
                    pipe.zadd(key, members)
                case "stream":
                    pipe.xadd(key, {"data": f"event_{i}", "ts": str(i)}, maxlen=1000)
                case "json":
                    import json as json_mod

                    fields = kd.get("fields_mapped", ["field1", "field2"])
                    doc = {f: f"val_{i}_{j}" for j, f in enumerate(fields[:10])}
                    pipe.set(key, json_mod.dumps(doc))
                case "hyperloglog":
                    pipe.pfadd(key, *[f"element_{j}" for j in range(min(100, count))])
                case "geo":
                    # Seed with random coordinates around a central point
                    import random

                    for j in range(min(10, count)):
                        lat = 40.0 + random.uniform(-1, 1)  # nosec B311
                        lon = -74.0 + random.uniform(-1, 1)  # nosec B311
                        pipe.geoadd(key, (lon, lat, f"location_{j}"))
                case _:
                    pipe.set(key, f"value_{i}")

            if ttl:
                pipe.expire(key, ttl)

            seeded += 1

            # Execute in batches of 500
            if seeded % 500 == 0:
                pipe.execute()
                pipe = client.pipeline(transaction=False)

        # Execute remaining
        pipe.execute()
        return seeded

    def _interpolate_key(self, pattern: str, index: int) -> str:
        """Replace placeholders in key pattern with index values.

        Handles patterns like 'users:{user_id}', 'posts:{post_id}:comments'
        by replacing any {placeholder} with the index.
        """
        import re

        return re.sub(r"\{[^}]+\}", str(index), pattern)
