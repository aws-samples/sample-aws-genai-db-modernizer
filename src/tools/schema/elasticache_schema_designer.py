"""ElastiCache (Redis/Valkey) schema designer — LLM-powered key design.

Strands Agent with structured output, retry with exponential backoff, graceful fallback.

Designs Redis key schemas per aggregate (group of related tables), then
the schema agent merges them into the final ElastiCacheModelOutputContract.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from pydantic import BaseModel, Field

from src.contracts.elasticache_model_output import (
    AccessPattern,
    CacheInvalidationStrategy,
    ElastiCacheModelOutputContract,
    KeyDesign,
    MigrationNote,
    UnsupportedPattern,
)
from src.contracts.schema_design_output import TradeOff

logger = logging.getLogger(__name__)

DEFAULT_SKILL_PATH = str(
    Path(__file__).resolve().parent.parent.parent / "skills" / "elasticache-data-modeling.md"
)


def load_skill(skill_path: str | None = None) -> str:
    """Load the ElastiCache data modeling skill prompt."""
    path = skill_path or DEFAULT_SKILL_PATH
    return Path(path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Pydantic models for per-aggregate LLM output
# ---------------------------------------------------------------------------


class AggregateKeyDesign(BaseModel):
    """LLM output for a single aggregate's Redis key design."""

    aggregate_id: str = Field(..., description="Aggregate ID from analysis")
    rationale: str = Field(..., description="Design rationale summary")
    key_designs: list[KeyDesign]
    access_patterns: list[AccessPattern] = Field(default_factory=list)
    cache_invalidation: list[CacheInvalidationStrategy] = Field(default_factory=list)
    unsupported_patterns: list[UnsupportedPattern] = Field(default_factory=list)
    migration_notes: list[MigrationNote] = Field(default_factory=list)
    trade_offs: list[TradeOff] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# LLM Designer
# ---------------------------------------------------------------------------


class LlmElastiCacheSchemaDesigner:
    """LLM-powered ElastiCache/Redis schema designer."""

    MAX_RETRIES = 3
    BASE_BACKOFF_SECONDS = 1.0

    def __init__(
        self,
        skill_path: str | None = None,
        enabled: bool | None = None,
    ):
        self.system_prompt = load_skill(skill_path)
        self.enabled = (
            enabled
            if enabled is not None
            else os.environ.get("ENABLE_SCHEMA_DESIGNER", "true").lower() == "true"
        )
        self._agent = None  # type: ignore[assignment]
        self.attempts_made = 0

    def _get_agent(self):  # type: ignore[return]
        """Lazily create the Strands Agent on first use."""
        if self._agent is None:
            from strands import Agent

            self._agent = Agent(
                system_prompt=self.system_prompt,
                tools=[],
                structured_output_model=AggregateKeyDesign,
                callback_handler=None,
            )
        return self._agent

    def design_aggregate(
        self,
        aggregate: dict,
        collector_output: dict,
        analysis_output: dict,
        decision_trace: dict,
    ) -> AggregateKeyDesign | None:
        """Design Redis keys for a single aggregate. Returns None on failure."""
        if not self.enabled:
            return None

        self.attempts_made = 0
        for attempt in range(self.MAX_RETRIES):
            self.attempts_made = attempt + 1
            if attempt > 0:
                backoff = self.BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                time.sleep(backoff)  # nosemgrep: arbitrary-sleep
            try:
                return self._call_llm(aggregate, collector_output, analysis_output, decision_trace)
            except Exception as exc:
                logger.warning(
                    "ElastiCache schema designer attempt %d/%d failed for %s: %s",
                    attempt + 1,
                    self.MAX_RETRIES,
                    aggregate.get("aggregate_id"),
                    exc,
                )
        return None

    def design_all(
        self,
        aggregates: list[dict],
        standalone_tables: list[str],
        collector_output: dict,
        analysis_output: dict,
        decision_trace: dict,
    ) -> ElastiCacheModelOutputContract | None:
        """Design schema for all aggregates and standalone tables."""
        if not self.enabled:
            return None

        all_key_designs: list[KeyDesign] = []
        all_patterns: list[AccessPattern] = []
        all_invalidation: list[CacheInvalidationStrategy] = []
        all_unsupported: list[UnsupportedPattern] = []
        all_migration: list[MigrationNote] = []
        all_trade_offs: list[TradeOff] = []

        for agg in aggregates:
            result = self.design_aggregate(agg, collector_output, analysis_output, decision_trace)
            if result:
                all_key_designs.extend(result.key_designs)
                all_patterns.extend(result.access_patterns)
                all_invalidation.extend(result.cache_invalidation)
                all_unsupported.extend(result.unsupported_patterns)
                all_migration.extend(result.migration_notes)
                all_trade_offs.extend(result.trade_offs)

        for table_id in standalone_tables:
            standalone_agg = {
                "aggregate_id": f"standalone-{table_id.split('.')[-1]}",
                "root_table": table_id,
                "member_tables": [table_id],
                "co_access_confidence": 0,
                "combined_migration_complexity": "LOW",
            }
            result = self.design_aggregate(
                standalone_agg, collector_output, analysis_output, decision_trace
            )
            if result:
                all_key_designs.extend(result.key_designs)
                all_patterns.extend(result.access_patterns)
                all_invalidation.extend(result.cache_invalidation)
                all_unsupported.extend(result.unsupported_patterns)
                all_migration.extend(result.migration_notes)
                all_trade_offs.extend(result.trade_offs)

        if not all_key_designs:
            return None

        job_id = collector_output.get("job_id", "")
        source_db = (
            collector_output.get("metadata", {}).get("source_database", {}).get("database_name", "")
        )

        return ElastiCacheModelOutputContract(
            job_id=job_id,
            source_database=source_db,
            key_designs=all_key_designs,
            access_patterns=all_patterns,
            cache_invalidation=all_invalidation,
            unsupported_patterns=all_unsupported,
            migration_notes=all_migration,
            trade_offs=all_trade_offs
            or [
                TradeOff(
                    description="No significant trade-offs identified",
                    impact="Minimal impact expected",
                    source_tables=[],
                    target_tables=[],
                    query_ids=[],
                    engine="elasticache",
                )
            ],
            validation_passed=True,
            validation_failures=[],
        )

    def _call_llm(
        self,
        aggregate: dict,
        collector_output: dict,
        analysis_output: dict,
        decision_trace: dict,
    ) -> AggregateKeyDesign:
        """Build prompt and call the Strands Agent."""
        member_tables = aggregate.get("member_tables", [])
        all_tables = collector_output.get("database_schema", {}).get("tables", [])
        relevant_tables = [
            t
            for t in all_tables
            if t.get("table_id") in member_tables or t.get("table_name") in member_tables
        ]

        all_recs = analysis_output.get("table_recommendations", [])
        relevant_recs = [r for r in all_recs if r.get("table_id") in member_tables]

        all_queries = collector_output.get("queries", {}).get("query_patterns", [])
        relevant_queries = [
            q
            for q in all_queries
            if any(t in member_tables for t in (q.get("tables_accessed") or []))
        ][:50]

        # Extract detected Redis patterns from analysis
        patterns_detected = analysis_output.get("workload_analysis", {}).get(
            "patterns_detected", []
        )
        relevant_patterns = [
            p
            for p in patterns_detected
            if any(t in member_tables for t in (p.get("table_ids") or []))
        ]

        prompt = self._build_prompt(
            aggregate,
            relevant_tables,
            relevant_recs,
            relevant_queries,
            relevant_patterns,
        )

        agent = self._get_agent()
        result = agent(prompt)

        output = getattr(result, "structured_output", None)
        if isinstance(output, AggregateKeyDesign):
            return output

        try:
            parsed = json.loads(str(result))
            return AggregateKeyDesign(**parsed)  # type: ignore[arg-type]
        except Exception as exc:
            raise ValueError(f"LLM did not return valid key design: {str(result)[:200]}") from exc

    def _build_prompt(
        self,
        aggregate: dict,
        tables: list[dict],
        recommendations: list[dict],
        queries: list[dict],
        redis_patterns: list[dict],
    ) -> str:
        compact_queries = [
            {
                "query_id": q.get("query_id"),
                "query_text": (q.get("query_text") or "")[:200],
                "query_type": q.get("query_type"),
                "tables_accessed": q.get("tables_accessed"),
                "frequency_per_hour": q.get("frequency_per_hour"),
                "calls_per_second": q.get("calls_per_second"),
                "rows_returned_avg": q.get("rows_returned_avg"),
                "filter_columns": q.get("filter_columns"),
                "sort_columns": q.get("sort_columns"),
                "has_joins": q.get("has_joins"),
                "has_aggregations": q.get("has_aggregations"),
                "has_time_range_filter": q.get("has_time_range_filter"),
            }
            for q in queries
        ]

        return (
            f"## Aggregate: {aggregate.get('aggregate_id')}\n\n"
            f"Root table: {aggregate.get('root_table')}\n"
            f"Member tables: {aggregate.get('member_tables')}\n"
            f"Co-access confidence: {aggregate.get('co_access_confidence')}%\n"
            f"Migration complexity: {aggregate.get('combined_migration_complexity')}\n\n"
            f"## Source Table Schemas\n\n{json.dumps(tables, indent=2, default=str)}\n\n"
            f"## Analysis Recommendations\n\n{json.dumps(recommendations, indent=2, default=str)}\n\n"
            f"## Detected Redis Patterns\n\n{json.dumps(redis_patterns, indent=2, default=str)}\n\n"
            f"## Query Patterns\n\n{json.dumps(compact_queries, indent=2, default=str)}\n\n"
            "Design the Redis/Valkey key schema for this aggregate. For each table or\n"
            "group of related data, choose the optimal Redis data type:\n\n"
            "- **Caching** (frequent SELECTs): `string` or `hash` with TTL\n"
            "- **Session store**: `hash` with TTL\n"
            "- **Leaderboard**: `sorted_set` with scores\n"
            "- **Geospatial**: `geo` for location-based queries\n"
            "- **Time series**: `sorted_set` (timestamp scores) or `stream`\n"
            "- **JSON documents**: `json` (RedisJSON) for nested structures\n"
            "- **Real-time analytics**: `hyperloglog` for cardinality, `bitmap` for flags\n"
            "- **Event sourcing**: `stream` with consumer groups\n"
            "- **Recommendations**: `set` for intersections/unions\n"
            "- **Reference data**: `hash` for field-level access\n\n"
            "For each key design provide:\n"
            "- Key naming pattern with placeholders\n"
            "- TTL policy (every cache key MUST have a TTL)\n"
            "- Concrete example key and value\n"
            "- Cache invalidation strategy\n"
            "- Estimated key count and value size\n\n"
            "Return the design as structured output matching the AggregateKeyDesign schema."
        )
