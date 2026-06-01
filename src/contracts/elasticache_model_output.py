"""ElastiCache (Redis/Valkey) schema design output contract.

Defines the structured output that the ElastiCache schema design agent
produces — key designs, data structure selections, access patterns
translated to Redis/Valkey commands, TTL policies, and eviction strategies.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .schema_design_output import TradeOff

# ---------------------------------------------------------------------------
# Building blocks
# ---------------------------------------------------------------------------

REDIS_DATA_TYPE = Literal[
    "string",
    "hash",
    "list",
    "set",
    "sorted_set",
    "stream",
    "json",
    "hyperloglog",
    "bloom_filter",
    "geo",
    "bitmap",
]

REDIS_OPERATION = Literal[
    # String
    "GET",
    "SET",
    "MGET",
    "MSET",
    "INCR",
    "INCRBY",
    "DECR",
    # Hash
    "HGET",
    "HSET",
    "HMGET",
    "HMSET",
    "HGETALL",
    "HDEL",
    "HINCRBY",
    # List
    "LPUSH",
    "RPUSH",
    "LPOP",
    "RPOP",
    "LRANGE",
    "LLEN",
    # Set
    "SADD",
    "SREM",
    "SMEMBERS",
    "SISMEMBER",
    "SINTER",
    "SUNION",
    "SCARD",
    # Sorted Set
    "ZADD",
    "ZREM",
    "ZRANGE",
    "ZREVRANGE",
    "ZRANGEBYSCORE",
    "ZRANK",
    "ZREVRANK",
    "ZSCORE",
    "ZCARD",
    # Stream
    "XADD",
    "XREAD",
    "XRANGE",
    "XREVRANGE",
    "XLEN",
    "XGROUP",
    "XREADGROUP",
    "XACK",
    # JSON (RedisJSON / Valkey JSON)
    "JSON.SET",
    "JSON.GET",
    "JSON.MGET",
    "JSON.DEL",
    "JSON.ARRAPPEND",
    # HyperLogLog
    "PFADD",
    "PFCOUNT",
    "PFMERGE",
    # Bloom filter (RedisBloom / Valkey Bloom)
    "BF.ADD",
    "BF.EXISTS",
    "BF.MADD",
    "BF.MEXISTS",
    # Geo
    "GEOADD",
    "GEODIST",
    "GEOSEARCH",
    "GEOSEARCHSTORE",
    # Bitmap
    "SETBIT",
    "GETBIT",
    "BITCOUNT",
    "BITOP",
    # Key management
    "DEL",
    "EXPIRE",
    "TTL",
    "EXISTS",
    "SCAN",
]

USE_CASE_TYPE = Literal[
    "caching",
    "session_store",
    "leaderboard",
    "geospatial",
    "time_series",
    "json_document",
    "real_time_analytics",
    "event_sourcing",
    "recommendation",
    "reference_data",
    "rate_limiting",
    "pub_sub",
]


class KeyDesign(BaseModel):
    """A Redis/Valkey key design mapping source data to a Redis data structure."""

    key_pattern: str = Field(
        ...,
        description="Key naming pattern with placeholders, e.g. 'user:{user_id}:session'",
    )
    data_type: REDIS_DATA_TYPE = Field(..., description="Redis data type for this key")
    use_case: USE_CASE_TYPE = Field(..., description="Primary use case this key serves")
    source_tables: list[str] = Field(
        ..., min_length=1, description="Source table_ids that map to this key design"
    )
    fields_mapped: list[str] = Field(
        default_factory=list,
        description="Source columns/fields stored in this key (for hash/json types)",
    )
    ttl_seconds: int | None = Field(None, ge=0, description="TTL in seconds (None = no expiry)")
    eviction_notes: str | None = Field(
        None, description="Notes on eviction behavior or memory management"
    )
    estimated_key_count: int | None = Field(
        None, ge=0, description="Estimated number of keys matching this pattern"
    )
    estimated_avg_value_bytes: int | None = Field(
        None, ge=0, description="Estimated average value size in bytes"
    )
    rationale: str = Field(..., description="Why this data type and key design were chosen")
    example_key: str = Field(..., description="Concrete example key, e.g. 'user:42:session'")
    example_value: str = Field(..., description="Example value (JSON string for complex types)")

    model_config = ConfigDict(extra="ignore")


class AccessPattern(BaseModel):
    """An access pattern translated to Redis/Valkey commands."""

    pattern_id: str = Field(
        ...,
        description="Stable identifier prefixed with engine shorthand, e.g. 'EC-AP-1', 'EC-AP-2'",
    )
    description: str = Field(..., description="Plain-English description of this pattern")
    operation: REDIS_OPERATION = Field(..., description="Primary Redis command")
    key_pattern: str = Field(..., description="Key pattern this access pattern targets")
    command_example: str = Field(
        ..., description="Full Redis command example, e.g. 'HGET user:42:profile name'"
    )
    source_tables: list[str] = Field(
        default_factory=list,
        description="Source table_ids that this access pattern reads from/writes to",
    )
    source_query_ids: list[str] = Field(
        ..., min_length=1, description="Source query IDs from collector"
    )
    pipeline_commands: list[str] | None = Field(
        None,
        description="Additional commands if this pattern requires a pipeline/transaction",
    )
    lua_script: str | None = Field(
        None, description="Lua script if atomic multi-step logic is needed"
    )

    model_config = ConfigDict(extra="ignore")


class UnsupportedPattern(BaseModel):
    """A source query pattern that cannot be served by Redis/Valkey."""

    source_query_ids: list[str] = Field(
        ..., min_length=1, description="Source query IDs that are unsupported"
    )
    reason: str = Field(..., description="Why this pattern is unsupported in Redis")
    workaround: str | None = Field(None, description="Suggested alternative approach")

    model_config = ConfigDict(extra="ignore")


class MigrationNote(BaseModel):
    """Migration consideration for moving data to Redis/Valkey."""

    object_name: str = Field(..., description="Name of the source object or pattern")
    object_type: Literal[
        "view", "procedure", "function", "trigger", "constraint", "transaction"
    ] = Field(..., description="Type of source object")
    source_table: str | None = Field(None, description="Related source table if applicable")
    application_logic_required: str = Field(
        ..., description="Description of application logic needed"
    )

    model_config = ConfigDict(extra="ignore")


class CacheInvalidationStrategy(BaseModel):
    """Cache invalidation strategy for a key design."""

    key_pattern: str = Field(..., description="Key pattern this strategy applies to")
    strategy: Literal["ttl", "write_through", "write_behind", "event_driven", "manual"] = Field(
        ..., description="Invalidation strategy type"
    )
    description: str = Field(..., description="How invalidation works for this key")
    source_write_query_ids: list[str] = Field(
        default_factory=list,
        description="Write query IDs that should trigger invalidation",
    )

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Top-level contract
# ---------------------------------------------------------------------------


class ElastiCacheModelOutputContract(BaseModel):
    """Output contract for the ElastiCache/Redis schema design agent."""

    contract_version: str = Field(
        default="1.0",
        pattern=r"^\d+\.\d+$",
        description="Contract version (MAJOR.MINOR format)",
    )
    job_id: str = Field(..., description="job_id from collector output")
    source_database: str = Field(..., description="database_name from collector metadata")
    target_engine: str = Field(default="elasticache", description="Target engine identifier")

    key_designs: list[KeyDesign] = Field(..., min_length=1, description="Redis/Valkey key designs")
    access_patterns: list[AccessPattern] = Field(
        ..., min_length=1, description="Access patterns translated to Redis commands"
    )
    cache_invalidation: list[CacheInvalidationStrategy] = Field(
        default_factory=list, description="Cache invalidation strategies per key design"
    )
    unsupported_patterns: list[UnsupportedPattern] = Field(
        default_factory=list, description="Patterns that cannot be served by Redis"
    )
    migration_notes: list[MigrationNote] = Field(
        default_factory=list, description="Migration considerations"
    )

    trade_offs: list[TradeOff] = Field(..., min_length=1, description="Design trade-off decisions")
    validation_passed: bool = Field(..., description="Whether all validation checks passed")
    validation_failures: list[str] = Field(
        default_factory=list, description="Validation failures when validation_passed=false"
    )

    model_config = ConfigDict(extra="ignore")
