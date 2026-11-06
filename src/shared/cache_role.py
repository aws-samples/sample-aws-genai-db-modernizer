# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Why the plan puts a cache-aside layer in front of a durable owner (#399).

The cache layer is this assessment's recommendation, not a limit of the service: the
reads it caches are hot, small and can be rebuilt from their owner engine, so the owner
stays the system of record and the cache is sized for the hot reads alone. ElastiCache
for Valkey 9.0 and later offers durability on Multi-AZ clusters (AWS ElastiCache
documentation, "Durability in ElastiCache"); the tool does not use it as a system of
record today (#401).

The sentence is ``facts.cache_layer.role_sentence`` (ADR-032): synthesis writes it once
and every deliverable prints it from there.
"""

from __future__ import annotations

from src.shared.engine_names import display_engine


def cache_role_sentence(engine: str | None) -> str:
    """The cache layer's role, as every deliverable states it."""
    name = display_engine(engine) if engine else "The cache layer"
    return (
        f"{name} is recommended as a cache-aside layer in front of the engine that owns "
        "each cached read: the cached reads are hot, small and can be rebuilt from that "
        "engine, so it stays the system of record and the cache is sized for the hot reads, "
        "not the whole dataset. ElastiCache for Valkey 9.0 and later offers durability on "
        "Multi-AZ clusters; this assessment does not use it as a system of record today."
    )
