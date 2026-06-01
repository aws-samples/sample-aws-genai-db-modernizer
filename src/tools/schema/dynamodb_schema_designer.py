"""
DynamoDB Schema Designer — LLM-powered data model generation.

Takes the analysis output (table recommendations, patterns, aggregates,
relationships) and generates concrete DynamoDB table designs per aggregate.

Architecture:
  1. Load skill prompt from skills/ directory (DynamoDB data modeling expertise)
  2. Per aggregate: send schema + patterns + relationships to LLM
  3. Get back structured table design (keys, GSIs, access patterns)
  4. Combine all aggregate designs into a single schema-design.json artifact

The skill prompt is a markdown file that acts as the system prompt — it contains
all the DynamoDB data modeling knowledge (single-table design, GSI strategies,
partition key patterns, etc.). This mirrors the "Claude Skills" approach where
expertise lives in a document, not in code.
"""

import json
import logging
import os
import time
from pathlib import Path

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Output models
# ---------------------------------------------------------------------------


class AttributeDefinition(BaseModel):
    """A DynamoDB attribute definition."""

    name: str = Field(description="Attribute name")
    type: str = Field(description="DynamoDB type: S (string), N (number), B (binary)")
    source_column: str | None = Field(None, description="Original source column this maps from")


class KeySchema(BaseModel):
    """Partition key(s) and optional sort key(s). DynamoDB GSIs support up to 4 partition key and 4 sort key attributes."""

    partition_key: str | list[str] = Field(
        description="Partition key attribute name(s) — single string or list of up to 4 for composite"
    )
    sort_key: str | list[str] | None = Field(
        None,
        description="Sort key attribute name(s) — single string or list of up to 4 for composite",
    )


class GSIDefinition(BaseModel):
    """A Global Secondary Index definition."""

    index_name: str = Field(description="GSI name")
    key_schema: KeySchema = Field(description="GSI key schema")
    projection_type: str = Field(
        default="ALL", description="Projection type: ALL, KEYS_ONLY, or INCLUDE"
    )
    projected_attributes: list[str] | None = Field(
        None, description="Attributes to project (when projection_type=INCLUDE)"
    )
    purpose: str = Field(description="What access pattern this GSI supports")


class AccessPattern(BaseModel):
    """An access pattern supported by this table design."""

    name: str = Field(description="Human-readable name (e.g., 'Get discussion by ID')")
    operation: str = Field(description="DynamoDB operation: GetItem, Query, Scan, PutItem, etc.")
    key_condition: str = Field(description="Key condition expression (e.g., 'PK = :discussionId')")
    index_used: str | None = Field(None, description="GSI name if not using base table")
    source_query: str | None = Field(None, description="Original SQL query this replaces")


class ItemExample(BaseModel):
    """An example item showing the data shape."""

    entity_type: str = Field(description="Entity type (e.g., 'DISCUSSION', 'POST', 'USER')")
    pk_value: str = Field(description="Example partition key value")
    sk_value: str | None = Field(None, description="Example sort key value")
    attributes: dict = Field(description="Example attribute values")


class TableDesign(BaseModel):
    """A single DynamoDB table design."""

    table_name: str = Field(description="Proposed DynamoDB table name")
    description: str = Field(description="What this table stores and why")
    design_approach: str = Field(description="single-table, table-per-entity, or hybrid")
    key_schema: KeySchema = Field(description="Base table key schema")
    attribute_definitions: list[AttributeDefinition] = Field(
        description="All key attributes (PK, SK, GSI keys)"
    )
    global_secondary_indexes: list[GSIDefinition] | None = Field(
        None, description="GSI definitions"
    )
    access_patterns: list[AccessPattern] = Field(description="Access patterns this design supports")
    item_examples: list[ItemExample] | None = Field(
        None, description="Example items showing data shape"
    )
    source_tables: list[str] = Field(
        description="Source tables from the relational database that map to this table"
    )
    ttl_attribute: str | None = Field(
        None, description="TTL attribute name if time-based expiry is recommended"
    )
    capacity_mode: str = Field(default="ON_DEMAND", description="ON_DEMAND or PROVISIONED")
    estimated_item_size_bytes: int | None = Field(
        None, description="Estimated average item size in bytes"
    )


class SchemaDesignOutput(BaseModel):
    """Complete schema design output for one target database."""

    target_database: str = Field(default="dynamodb")
    design_summary: str = Field(description="High-level summary of the data model design decisions")
    tables: list[TableDesign] = Field(description="All DynamoDB table designs")
    migration_notes: list[str] | None = Field(
        None, description="Important notes for the migration team"
    )
    total_gsi_count: int | None = Field(
        None, description="Total GSIs across all tables (DynamoDB limit: 20 per table)"
    )


class AggregateSchemaDesign(BaseModel):
    """Schema design for a single aggregate — the unit of LLM invocation."""

    aggregate_id: str = Field(description="Aggregate ID from analysis output")
    tables: list[TableDesign] = Field(description="Table designs for this aggregate")
    rationale: str = Field(description="Why this design was chosen for this aggregate")


# ---------------------------------------------------------------------------
# Skill loader
# ---------------------------------------------------------------------------

DEFAULT_SKILL_PATH = "src/skills/dynamodb-data-modeling.md"

# Fallback system prompt if skill file is not found
FALLBACK_SYSTEM_PROMPT = """You are a DynamoDB data modeling expert. Given a set of relational
database tables, their access patterns, and analysis results, design an optimal DynamoDB
data model. Consider single-table design where appropriate, use GSIs for secondary access
patterns, and follow DynamoDB best practices for partition key design."""


def load_skill(skill_path: str | None = None) -> str:
    """Load a skill markdown file as a system prompt.

    Falls back to a minimal built-in prompt if the file doesn't exist.
    """
    path = skill_path or DEFAULT_SKILL_PATH
    resolved = Path(path)

    # Try relative to project root
    if not resolved.is_absolute():
        project_root = Path(__file__).parent.parent.parent.parent
        resolved = project_root / path

    if resolved.exists():
        content = resolved.read_text(encoding="utf-8")
        logger.info("Loaded skill from %s (%d chars)", resolved, len(content))
        return content

    logger.warning("Skill file not found at %s — using fallback prompt", resolved)
    return FALLBACK_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# LLM Schema Designer
# ---------------------------------------------------------------------------


class LlmSchemaDesigner:
    """LLM-powered DynamoDB schema designer.

    Follows the same pattern as LlmAdvisor: Strands Agent with structured
    output, retry with exponential backoff, graceful fallback.

    Usage:
        designer = LlmSchemaDesigner()
        result = designer.design_aggregate(
            aggregate=aggregate_data,
            collector_output=collector_output,
            analysis_output=analysis_output,
        )
    """

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
        self._agent = None
        self.attempts_made = 0

    def _get_agent(self):
        """Lazily create the Strands Agent on first use."""
        if self._agent is None:
            from strands import Agent

            self._agent = Agent(
                system_prompt=self.system_prompt,
                tools=[],
                structured_output_model=AggregateSchemaDesign,
                callback_handler=None,
            )
        return self._agent

    def design_aggregate(
        self,
        aggregate: dict,
        collector_output: dict,
        analysis_output: dict,
        timeout_seconds: int = 120,
    ) -> AggregateSchemaDesign | None:
        """Design schema for a single aggregate. Returns None after all retries exhausted."""
        if not self.enabled:
            logger.info(
                "Schema designer disabled — skipping aggregate %s", aggregate.get("aggregate_id")
            )
            return None

        self.attempts_made = 0

        for attempt in range(self.MAX_RETRIES):
            self.attempts_made = attempt + 1
            if attempt > 0:
                backoff = self.BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
                time.sleep(backoff)  # nosemgrep: arbitrary-sleep  # retry backoff
            try:
                return self._call_llm(aggregate, collector_output, analysis_output)
            except Exception as exc:
                logger.warning(
                    "Schema designer attempt %d/%d failed for %s: %s",
                    attempt + 1,
                    self.MAX_RETRIES,
                    aggregate.get("aggregate_id"),
                    exc,
                )
                continue

        return None

    def design_all(
        self,
        aggregates: list[dict],
        standalone_tables: list[str],
        collector_output: dict,
        analysis_output: dict,
    ) -> SchemaDesignOutput | None:
        """Design schema for all aggregates and standalone tables.

        Args:
            aggregates: List of aggregate dicts from analysis output.
            standalone_tables: Table IDs not in any aggregate.
            collector_output: Full collector output.
            analysis_output: Full analysis output (dict from model_dump).

        Returns:
            Complete SchemaDesignOutput or None if all calls fail.
        """
        if not self.enabled:
            return None

        all_tables: list[TableDesign] = []
        rationales: list[str] = []

        # Design per aggregate
        for agg in aggregates:
            result = self.design_aggregate(agg, collector_output, analysis_output)
            if result:
                all_tables.extend(result.tables)
                rationales.append(f"[{result.aggregate_id}] {result.rationale}")
            else:
                logger.warning("Failed to design aggregate %s", agg.get("aggregate_id"))

        # Design standalone tables (wrap each as a single-table aggregate)
        for table_id in standalone_tables:
            standalone_agg = {
                "aggregate_id": f"standalone-{table_id.split('.')[-1]}",
                "root_table": table_id,
                "member_tables": [table_id],
                "co_access_confidence": 0,
                "combined_migration_complexity": "LOW",
            }
            result = self.design_aggregate(standalone_agg, collector_output, analysis_output)
            if result:
                all_tables.extend(result.tables)
                rationales.append(f"[{result.aggregate_id}] {result.rationale}")

        if not all_tables:
            return None

        total_gsis = sum(len(t.global_secondary_indexes or []) for t in all_tables)

        return SchemaDesignOutput(
            target_database="dynamodb",
            design_summary="\n".join(rationales),
            tables=all_tables,
            migration_notes=[
                "Review GSI projections — ALL projection maximizes flexibility but increases cost",
                "Consider DynamoDB Streams for change data capture during migration",
                "Test with production-scale data before finalizing capacity mode",
            ],
            total_gsi_count=total_gsis,
        )

    def _call_llm(
        self,
        aggregate: dict,
        collector_output: dict,
        analysis_output: dict,
    ) -> AggregateSchemaDesign:
        """Build prompt and call the Strands Agent."""
        # Extract relevant tables from collector output
        member_tables = aggregate.get("member_tables", [])
        all_tables = collector_output.get("database_schema", {}).get("tables", [])
        relevant_tables = [
            t
            for t in all_tables
            if t.get("table_id") in member_tables or t.get("table_name") in member_tables
        ]

        # Extract relevant recommendations from analysis output
        all_recs = analysis_output.get("table_recommendations", [])
        relevant_recs = [r for r in all_recs if r.get("table_id") in member_tables]

        # Extract relevant query patterns
        all_queries = collector_output.get("queries", {}).get("query_patterns", [])
        relevant_queries = [
            q
            for q in all_queries
            if any(t in member_tables for t in (q.get("tables_accessed") or []))
        ]

        # Build the prompt
        prompt = self._build_prompt(aggregate, relevant_tables, relevant_recs, relevant_queries)

        agent = self._get_agent()
        result = agent(prompt)

        # Extract structured output
        output: AggregateSchemaDesign | None = getattr(result, "structured_output", None)
        if output is not None and isinstance(output, AggregateSchemaDesign):
            return output

        # Fallback: parse from text
        try:
            text = str(result)
            parsed = json.loads(text)
            return AggregateSchemaDesign(**parsed)  # type: ignore[arg-type]
        except Exception as exc:
            raise ValueError(
                f"LLM did not return valid schema design: {str(result)[:200]}"
            ) from exc

    def _build_prompt(
        self,
        aggregate: dict,
        tables: list[dict],
        recommendations: list[dict],
        queries: list[dict],
    ) -> str:
        """Build a structured prompt for the LLM."""
        # Compact the query data to avoid huge prompts
        compact_queries = []
        for q in queries[:50]:  # Cap at 50 queries
            compact_queries.append(
                {
                    "query_text": q.get("query_text", "")[:200],
                    "query_type": q.get("query_type"),
                    "tables_accessed": q.get("tables_accessed"),
                    "frequency_per_hour": q.get("frequency_per_hour"),
                    "calls_per_second": q.get("calls_per_second"),
                    "avg_latency_ms": q.get("avg_latency_ms"),
                    "rows_returned_avg": q.get("rows_returned_avg"),
                    "filter_columns": q.get("filter_columns"),
                    "sort_columns": q.get("sort_columns"),
                }
            )

        return (
            f"## Aggregate: {aggregate.get('aggregate_id')}\n\n"
            f"Root table: {aggregate.get('root_table')}\n"
            f"Member tables: {aggregate.get('member_tables')}\n"
            f"Co-access confidence: {aggregate.get('co_access_confidence')}%\n"
            f"Migration complexity: {aggregate.get('combined_migration_complexity')}\n\n"
            f"## Source Table Schemas\n\n{json.dumps(tables, indent=2, default=str)}\n\n"
            f"## Analysis Recommendations\n\n{json.dumps(recommendations, indent=2, default=str)}\n\n"
            f"## Query Patterns\n\n{json.dumps(compact_queries, indent=2, default=str)}\n\n"
            "Design the DynamoDB data model for this aggregate. Consider:\n"
            "- Whether single-table design is appropriate for these entities\n"
            "- Partition key and sort key design for each entity type\n"
            "- GSIs needed for the access patterns shown in the queries\n"
            "- How to handle the relationships between tables\n"
            "- Item examples showing the data shape\n\n"
            "Return the design as structured output matching the AggregateSchemaDesign schema."
        )
