"""
DynamoDB Schema Design Agent — uses SchemaDesignRunner for PE review loop.

Reads CollectorOutputContract and AnalysisOutputContract from paths set by
the handler, projects them through the schema_design_input contract models
(validation gate), and produces a DynamoDBModelOutputContract.

Architecture:
  1. load_agent_input tool: reads collector + analysis, projects via
     project_schema_design_input(), fails hard if validation fails
  2. compute_performances_and_costs tool: validates hot partition entries
  3. SchemaDesignRunner handles: designer invocation with retries,
     PE review loop, duplicate feedback detection, graceful fallback
  4. Skill prompt loaded from src/skills/dynamodb-data-modeling.md

Invoked by src/agents/schema_design/handler.py when target_type="dynamodb".
"""

import json
import logging
import os
import time
from pathlib import Path

from strands import Agent, tool
from strands.models.bedrock import BedrockModel

from src.contracts.analysis_output import AnalysisOutputContract
from src.contracts.collector_output import CollectorOutputContract
from src.contracts.dynamodb_model_output import DynamoDBModelOutputContract, HotPartitionEntry
from src.contracts.dynamodb_pe_review import PEReviewResult, ReviewVerdict  # noqa: F401
from src.contracts.schema_design_input import project_schema_design_input
from src.tools.schema.base_schema_agent import SchemaDesignRunner

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level path variables (set by run_dynamodb_schema_agent, read by tools)
# ---------------------------------------------------------------------------
_collector_path: str | None = None
_analysis_path: str | None = None
_revision_context_path: str | None = None

# ---------------------------------------------------------------------------
# Skill loader
# ---------------------------------------------------------------------------

DEFAULT_SKILL_PATH = "src/skills/dynamodb-data-modeling.md"
DEFAULT_PE_SKILL_PATH = "src/skills/dynamodb-pe-review.md"

FALLBACK_SYSTEM_PROMPT = (
    "You are a DynamoDB data modeling expert. Convert a relational database "
    "to DynamoDB using production telemetry. Follow the DynamoDB Model "
    "Output Contract."
)


def load_skill(skill_path: str | None = None) -> str:
    """Load the DynamoDB data modeling skill as a system prompt."""
    path = skill_path or DEFAULT_SKILL_PATH
    resolved = Path(path)

    if not resolved.is_absolute():
        project_root = Path(__file__).parent.parent.parent.parent
        resolved = project_root / path

    if resolved.exists():
        content = resolved.read_text(encoding="utf-8")
        logger.info("Loaded skill from %s (%d chars)", resolved, len(content))
        return content

    logger.warning("Skill not found at %s — using fallback", resolved)
    return FALLBACK_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Strands tools
# ---------------------------------------------------------------------------


@tool
def load_agent_input() -> dict:
    """Load collector + analysis outputs and project into validated input.

    Reads from paths set by run_dynamodb_schema_agent (preferred) or
    env vars COLLECTOR_OUTPUT_PATH/ANALYSIS_OUTPUT_PATH (fallback).

    Returns a dict with 'collector', 'analysis', and 'context' keys.
    """
    collector_path = _collector_path or os.environ.get("COLLECTOR_OUTPUT_PATH")
    analysis_path = _analysis_path or os.environ.get("ANALYSIS_OUTPUT_PATH")

    if not collector_path or not analysis_path:
        raise ValueError(
            "Collector/analysis paths must be set via run_dynamodb_schema_agent() "
            "or COLLECTOR_OUTPUT_PATH/ANALYSIS_OUTPUT_PATH env vars"
        )

    logger.info("Loading collector from %s", collector_path)
    with open(collector_path, encoding="utf-8") as f:
        collector = CollectorOutputContract.model_validate(json.load(f))

    logger.info("Loading analysis from %s", analysis_path)
    with open(analysis_path, encoding="utf-8") as f:
        analysis = AnalysisOutputContract.model_validate(json.load(f))

    agent_collector, agent_analysis, agent_context = project_schema_design_input(
        collector, analysis
    )

    print(
        f"[load_agent_input] Projected: {len(agent_collector.tables)} tables, "
        f"{len(agent_collector.queries.query_patterns)} patterns, "
        f"{len(agent_analysis.aggregate_recommendations or [])} aggregates"
    )

    return {
        "collector": agent_collector.model_dump(mode="json"),
        "analysis": agent_analysis.model_dump(mode="json"),
        "context": agent_context.model_dump(mode="json"),
    }


@tool
def compute_performances_and_costs(
    hot_partition_entries_json: str,
) -> list[dict]:
    """Validate hot partition entries against HotPartitionEntry contract.

    Args:
        hot_partition_entries_json: JSON string of list[HotPartitionEntry]

    Returns:
        list of validated HotPartitionEntry dicts
    """
    raw_entries = json.loads(hot_partition_entries_json)
    validated = [HotPartitionEntry.model_validate(e) for e in raw_entries]
    logger.info("Validated %d hot partition entries", len(validated))
    return [e.model_dump(mode="json") for e in validated]


# ---------------------------------------------------------------------------
# Model builder
# ---------------------------------------------------------------------------


def _build_model() -> BedrockModel:
    """Build the shared BedrockModel for both designer and PE agents."""
    from botocore.config import Config

    model_id = os.environ.get(
        "SCHEMA_AGENT_MODEL_ID",
        "us.anthropic.claude-opus-4-6-v1",
    )
    max_tokens = int(os.environ.get("SCHEMA_AGENT_MAX_TOKENS", "65536"))

    additional_fields = {}
    if "opus" in model_id:
        additional_fields["thinking"] = {
            "type": "enabled",
            "budget_tokens": 6000,
        }

    return BedrockModel(
        model_id=model_id,
        max_tokens=max_tokens,
        temperature=1.0,
        additional_request_fields=additional_fields,
        boto_client_config=Config(read_timeout=300, connect_timeout=10),
    )


# ---------------------------------------------------------------------------
# PE reviewer
# ---------------------------------------------------------------------------

MAX_ATTRS_PER_ENTITY = 10


def _invoke_pe_reviewer(
    model: BedrockModel,
    design_output: DynamoDBModelOutputContract,
    agent_input_summary: dict,
    pe_skill_path: str | None = None,
) -> PEReviewResult:
    """Invoke the PE reviewer agent on a design output."""
    print("[schema-design/dynamodb][pe-review] Invoking PE reviewer...")
    pe_prompt_text = load_skill(pe_skill_path or DEFAULT_PE_SKILL_PATH)

    pe_agent = Agent(
        model=model,
        system_prompt=pe_prompt_text,
        tools=[],
        structured_output_model=PEReviewResult,
        callback_handler=None,
    )

    design_json = design_output.model_dump(mode="json")
    design_summary = _build_pe_input(design_json)

    prompt = (
        "Review the following DynamoDB schema design.\n\n"
        f"## Source Database Summary\n"
        f"Tables: {agent_input_summary.get('table_count', 0)}, "
        f"Query patterns: {agent_input_summary.get('pattern_count', 0)}, "
        f"Aggregates: {agent_input_summary.get('aggregate_count', 0)}\n\n"
        f"## Design Output\n```json\n{json.dumps(design_summary, indent=2)}\n```\n\n"
        "Evaluate this design following your review process. "
        "Return a PEReviewResult with your verdict and any change requests."
    )

    result = pe_agent(prompt)
    output = getattr(result, "structured_output", None)
    if isinstance(output, PEReviewResult):
        print(
            f"[schema-design/dynamodb][pe-review] Verdict: {output.verdict.value} | "
            f"Changes: {len(output.change_requests)} | "
            f"Strengths: {len(output.strengths)}"
        )
        return output

    parsed = json.loads(str(result))
    return PEReviewResult.model_validate(parsed)


def _build_pe_input(design_json: dict) -> dict:
    """Build PE review input — full structure with attribute sampling for large models."""
    tables = []
    for t in design_json.get("table_definitions", []):
        table_copy = {**t}
        if table_copy.get("attributes") and len(table_copy["attributes"]) > MAX_ATTRS_PER_ENTITY:
            sampled = table_copy["attributes"][:5]
            denorm = [a for a in table_copy["attributes"][5:] if a.get("denormalized")]
            sampled.extend(denorm[:5])
            table_copy["attributes"] = sampled
            table_copy["_attrs_sampled"] = True
            table_copy["_total_attrs"] = len(t["attributes"])
        if table_copy.get("entities"):
            for ent in table_copy["entities"]:
                if len(ent.get("attributes", [])) > MAX_ATTRS_PER_ENTITY:
                    sampled = ent["attributes"][:5]
                    denorm = [a for a in ent["attributes"][5:] if a.get("denormalized")]
                    sampled.extend(denorm[:5])
                    ent["attributes"] = sampled
                    ent["_attrs_sampled"] = True
                    ent["_total_attrs"] = len(t["entities"][0]["attributes"])
        tables.append(table_copy)

    return {
        "contract_version": design_json.get("contract_version"),
        "job_id": design_json.get("job_id"),
        "source_database": design_json.get("source_database"),
        "table_definitions": tables,
        "access_patterns": design_json.get("access_patterns", []),
        "unsupported_patterns": design_json.get("unsupported_patterns", []),
        "hot_partition_analysis": design_json.get("hot_partition_analysis", []),
        "trade_offs": design_json.get("trade_offs", []),
        "validation_passed": design_json.get("validation_passed"),
        "validation_failures": design_json.get("validation_failures", []),
    }


def _format_pe_feedback(review: PEReviewResult) -> str:
    """Format PE review into a prompt-friendly string."""
    lines = [f"## PE Summary\n{review.summary}\n"]
    if review.strengths:
        lines.append("## Strengths (keep these)")
        for s in review.strengths:
            lines.append(f"- {s}")
        lines.append("")
    if review.change_requests:
        lines.append("## Required Changes")
        for cr in review.change_requests:
            lines.append(f"### [{cr.severity.value.upper()}] {cr.category.value}: {cr.target}")
            lines.append(f"Current: {cr.current_state}")
            lines.append(f"Requested: {cr.requested_change}")
            lines.append(f"Rationale: {cr.rationale}")
            lines.append("")
    if review.pe_notes:
        lines.append("## Advisory Notes")
        for note in review.pe_notes:
            lines.append(f"- {note}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Trace (kept for backward compat with existing tests)
# ---------------------------------------------------------------------------


class SchemaDesignTrace:
    """Accumulates structured trace entries for the design loop."""

    def __init__(self) -> None:
        self.iterations: list[dict] = []
        self.start_time = time.time()

    def log_designer(
        self,
        iteration: int,
        duration_s: float,
        output: DynamoDBModelOutputContract,
    ) -> None:
        entry = self._get_or_create(iteration)
        entry["designer"] = {
            "duration_seconds": round(duration_s, 2),
            "tables": len(output.table_definitions),
            "access_patterns": len(output.access_patterns),
            "unsupported_patterns": len(output.unsupported_patterns),
            "hot_partition_entries": len(output.hot_partition_analysis),
            "validation_passed": output.validation_passed,
        }

    def log_pe_review(
        self,
        iteration: int,
        duration_s: float,
        review: PEReviewResult,
    ) -> None:
        entry = self._get_or_create(iteration)
        entry["pe_review"] = {
            "duration_seconds": round(duration_s, 2),
            "verdict": review.verdict.value,
            "change_requests": [
                {
                    "category": cr.category.value,
                    "severity": cr.severity.value,
                    "target": cr.target,
                    "requested_change": cr.requested_change,
                    "rationale": cr.rationale,
                }
                for cr in review.change_requests
            ],
            "strengths": review.strengths,
            "pe_notes": review.pe_notes,
            "summary": review.summary,
        }

    def log_pe_error(self, iteration: int, error: str) -> None:
        entry = self._get_or_create(iteration)
        entry["pe_review"] = {"error": error}

    def to_dict(self) -> dict:
        return {
            "total_duration_seconds": round(time.time() - self.start_time, 2),
            "total_iterations": len(self.iterations),
            "iterations": self.iterations,
        }

    def _get_or_create(self, iteration: int) -> dict:
        while len(self.iterations) <= iteration:
            self.iterations.append({"iteration": len(self.iterations) + 1})
        return self.iterations[iteration]


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_dynamodb_schema_agent(
    skill_path: str | None = None,
    pe_skill_path: str | None = None,
    collector_path: str | None = None,
    analysis_path: str | None = None,
    revision_context_path: str | None = None,
) -> tuple[DynamoDBModelOutputContract, dict]:
    """Run the DynamoDB schema design agent with PE review loop.

    Uses SchemaDesignRunner for retry logic, graceful fallback on max_tokens,
    duplicate PE feedback detection, and consistent logging.

    Args:
        collector_path: Path to collector JSON (preferred over env var).
        analysis_path: Path to analysis JSON (preferred over env var).
        revision_context_path: Path to revision context JSON (optional).

    Returns:
        Tuple of (validated output, trace dict for S3 artifact)
    """
    global _collector_path, _analysis_path, _revision_context_path
    _collector_path = collector_path
    _analysis_path = analysis_path
    _revision_context_path = revision_context_path

    model = _build_model()
    system_prompt = load_skill(skill_path)

    designer = Agent(
        model=model,
        system_prompt=system_prompt,
        tools=[load_agent_input, compute_performances_and_costs],
        structured_output_model=DynamoDBModelOutputContract,
        callback_handler=None,
    )

    designer_prompt = (
        "Use the load_agent_input tool to read the projected input. "
        "Then follow all phases in the skill prompt to design the "
        "DynamoDB data model. Before finalizing, call "
        "compute_performances_and_costs with your hot partition "
        "analysis entries. Return the complete "
        "DynamoDBModelOutputContract."
    )

    # Inject revision context if this is a revision-triggered redesign
    if _revision_context_path:
        import json as _json
        from pathlib import Path as _Path

        revision_ctx = _json.loads(_Path(_revision_context_path).read_text(encoding="utf-8"))
        revision_sections = []
        if revision_ctx.get("exclusion_instructions"):
            revision_sections.append(
                f"## Excluded Patterns\n{revision_ctx['exclusion_instructions']}"
            )
        if revision_ctx.get("customer_instructions"):
            revision_sections.append(f"## Customer Notes\n{revision_ctx['customer_instructions']}")
        if revision_ctx.get("new_patterns_instructions"):
            revision_sections.append(
                f"## New Patterns to Design\n{revision_ctx['new_patterns_instructions']}"
            )
        if revision_sections:
            designer_prompt += (
                "\n\n---\n# REVISION CONTEXT\n"
                "This is a revision pass. Apply the following customer instructions:\n\n"
                + "\n\n".join(revision_sections)
            )

    runner = SchemaDesignRunner(
        target_type="dynamodb",
        output_model=DynamoDBModelOutputContract,
        model=model,
        designer_agent=designer,
        pe_skill_path=pe_skill_path or DEFAULT_PE_SKILL_PATH,
        pe_reviewer_fn=_invoke_pe_reviewer,
        format_pe_feedback_fn=_format_pe_feedback,
    )

    return runner.run(designer_prompt)
