"""DocumentDB schema designer — LLM-powered collection design.

Mirrors the DynamoDB schema designer pattern: Strands Agent with structured
output, retry with exponential backoff, graceful fallback.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

from pydantic import BaseModel, Field

from src.contracts.documentdb_model_output import (
    AccessPattern,
    CollectionDefinition,
    DocumentDBModelOutputContract,
    MigrationNote,
    UnsupportedPattern,
)
from src.contracts.schema_design_output import TradeOff

logger = logging.getLogger(__name__)

DEFAULT_SKILL_PATH = str(
    Path(__file__).resolve().parent.parent.parent / "skills" / "documentdb-data-modeling.md"
)


def load_skill(skill_path: str | None = None) -> str:
    """Load the DocumentDB data modeling skill prompt."""
    path = skill_path or DEFAULT_SKILL_PATH
    return Path(path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Pydantic models for per-aggregate LLM output
# ---------------------------------------------------------------------------


class AggregateCollectionDesign(BaseModel):
    """LLM output for a single aggregate's collection design."""

    aggregate_id: str = Field(..., description="Aggregate ID from analysis")
    rationale: str = Field(..., description="Design rationale summary")
    collections: list[CollectionDefinition]
    access_patterns: list[AccessPattern] = Field(default_factory=list)
    unsupported_patterns: list[UnsupportedPattern] = Field(default_factory=list)
    migration_notes: list[MigrationNote] = Field(default_factory=list)
    trade_offs: list[TradeOff] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# LLM Designer
# ---------------------------------------------------------------------------


class LlmDocumentDBSchemaDesigner:
    """LLM-powered DocumentDB schema designer."""

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
                structured_output_model=AggregateCollectionDesign,
                callback_handler=None,
            )
        return self._agent

    def design_aggregate(
        self,
        aggregate: dict,
        collector_output: dict,
        analysis_output: dict,
        decision_trace: dict,
    ) -> AggregateCollectionDesign | None:
        """Design collections for a single aggregate. Returns None on failure."""
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
                    "DocumentDB schema designer attempt %d/%d failed for %s: %s",
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
    ) -> DocumentDBModelOutputContract | None:
        """Design schema for all aggregates and standalone tables."""
        if not self.enabled:
            return None

        all_collections: list[CollectionDefinition] = []
        all_patterns: list[AccessPattern] = []
        all_unsupported: list[UnsupportedPattern] = []
        all_migration: list[MigrationNote] = []
        all_trade_offs: list = []

        for agg in aggregates:
            result = self.design_aggregate(agg, collector_output, analysis_output, decision_trace)
            if result:
                all_collections.extend(result.collections)
                all_patterns.extend(result.access_patterns)
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
                all_collections.extend(result.collections)
                all_patterns.extend(result.access_patterns)
                all_unsupported.extend(result.unsupported_patterns)
                all_migration.extend(result.migration_notes)
                all_trade_offs.extend(result.trade_offs)

        if not all_collections:
            return None

        job_id = collector_output.get("job_id", "")
        source_db = (
            collector_output.get("metadata", {}).get("source_database", {}).get("database_name", "")
        )

        return DocumentDBModelOutputContract(
            job_id=job_id,
            source_database=source_db,
            collections=all_collections,
            access_patterns=all_patterns,
            unsupported_patterns=all_unsupported,
            migration_notes=all_migration,
            trade_offs=all_trade_offs
            or [
                TradeOff(
                    description="No significant trade-offs identified",
                    impact="The migration is straightforward with no major behavioral changes.",
                    engine="documentdb",
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
    ) -> AggregateCollectionDesign:
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

        # Extract relevant embedding candidates and denorm strategies
        relevant_embeddings = [
            e
            for e in (decision_trace.get("embedding_candidates") or [])
            if e.get("parent_table") in member_tables or e.get("child_table") in member_tables
        ]
        relevant_denorm = [
            s
            for s in (decision_trace.get("denormalization_strategies") or [])
            if s.get("parent_table") in member_tables or s.get("child_table") in member_tables
        ]

        prompt = self._build_prompt(
            aggregate,
            relevant_tables,
            relevant_recs,
            relevant_queries,
            relevant_embeddings,
            relevant_denorm,
            decision_trace.get("documentdb_compatibility", {}),
        )

        agent = self._get_agent()
        result = agent(prompt)

        output = getattr(result, "structured_output", None)
        if isinstance(output, AggregateCollectionDesign):
            return output

        try:
            parsed = json.loads(str(result))
            return AggregateCollectionDesign(**parsed)  # type: ignore[arg-type]
        except Exception as exc:
            raise ValueError(
                f"LLM did not return valid collection design: {str(result)[:200]}"
            ) from exc

    def _build_prompt(
        self,
        aggregate: dict,
        tables: list[dict],
        recommendations: list[dict],
        queries: list[dict],
        embedding_candidates: list[dict],
        denorm_strategies: list[dict],
        compatibility: dict,
    ) -> str:
        compact_queries = [
            {
                "query_text": q.get("query_text", "")[:200],
                "query_type": q.get("query_type"),
                "tables_accessed": q.get("tables_accessed"),
                "frequency_per_hour": q.get("frequency_per_hour"),
                "calls_per_second": q.get("calls_per_second"),
                "rows_returned_avg": q.get("rows_returned_avg"),
                "filter_columns": q.get("filter_columns"),
                "sort_columns": q.get("sort_columns"),
                "has_joins": q.get("has_joins"),
                "has_aggregations": q.get("has_aggregations"),
                "has_text_search": q.get("has_text_search"),
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
            f"## Query Patterns\n\n{json.dumps(compact_queries, indent=2, default=str)}\n\n"
            f"## Pre-Computed Embedding Decisions\n\n{json.dumps(embedding_candidates, indent=2, default=str)}\n\n"
            f"## Denormalization Strategies\n\n{json.dumps(denorm_strategies, indent=2, default=str)}\n\n"
            f"## DocumentDB Compatibility\n\n{json.dumps(compatibility, indent=2, default=str)}\n\n"
            "Design the DocumentDB collections for this aggregate. Use the pre-computed\n"
            "embedding decisions as your starting point — validate and refine them.\n"
            "For each collection, provide:\n"
            "- Document shape with embedded entities and reference fields\n"
            "- Indexes for all access patterns (compound index field order: equality → sort → range)\n"
            "- 2-3 realistic document examples\n"
            "- Estimated document sizes\n\n"
            "Return the design as structured output matching the AggregateCollectionDesign schema."
        )
