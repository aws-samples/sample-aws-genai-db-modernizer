"""ElastiCache (Redis/Valkey) schema design agent — uses SchemaDesignRunner for PE review loop.

Flow:
  1. Load projected input (collector + analysis + decision trace)
  2. Compact input to fit within Bedrock token limits
  3. SchemaDesignRunner handles: designer invocation with retries,
     PE review loop, duplicate feedback detection, graceful fallback
  4. Returns final output + trace log
"""

from __future__ import annotations

import json
import logging
import os
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from strands import Agent

from src.agents.prompt_framing import (
    SYSTEM_PROMPT_DATA_DIRECTIVE,
    frame_customer_requests,
    frame_untrusted,
)
from src.contracts.analysis_output import AnalysisOutputContract
from src.contracts.collector_output import CollectorOutputContract
from src.contracts.elasticache_decision_trace import ElastiCacheDecisionTrace
from src.contracts.elasticache_model_output import ElastiCacheModelOutputContract
from src.contracts.schema_design_input import project_schema_design_input
from src.shared.contract_versioning import check_contract_version, load_contract
from src.shared.models import Role, build_bedrock_model
from src.shared.role_bedrock_model import RoleBedrockModel
from src.tools.schema.base_schema_agent import AgentInputPaths, SchemaDesignRunner
from src.tools.sql.query_pattern_groups import listed_query_patterns

logger = logging.getLogger(__name__)

DEFAULT_SKILL_PATH = str(
    Path(__file__).resolve().parent.parent.parent / "skills" / "elasticache-data-modeling.md"
)
DEFAULT_PE_SKILL_PATH = str(
    Path(__file__).resolve().parent.parent.parent / "skills" / "elasticache-pe-review.md"
)


# ---------------------------------------------------------------------------
# ElastiCache-specific PE Review models
# ---------------------------------------------------------------------------


class ReviewVerdict(str, Enum):
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"


class ChangeSeverity(str, Enum):
    BLOCKER = "blocker"
    WARNING = "warning"


class ChangeCategory(str, Enum):
    KEY_DESIGN = "key_design"
    MEMORY = "memory"
    ACCESS_PATTERN = "access_pattern"
    INVALIDATION = "invalidation"
    OPERATIONAL = "operational"
    MIGRATION = "migration"


class ChangeRequest(BaseModel):
    category: ChangeCategory
    severity: ChangeSeverity
    target: str
    requested_change: str
    rationale: str

    model_config = ConfigDict(extra="ignore")


class PEReviewResult(BaseModel):
    """ElastiCache PE review result — compatible with SchemaDesignRunner."""

    verdict: ReviewVerdict
    change_requests: list[ChangeRequest] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    pe_notes: list[str] = Field(default_factory=list)
    summary: str = ""

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_skill(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _build_model() -> RoleBedrockModel:
    """Build the shared RoleBedrockModel for both designer and PE agents (role SCHEMA_DESIGN)."""
    from botocore.config import Config

    return build_bedrock_model(
        Role.SCHEMA_DESIGN,
        max_tokens=32768,
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        boto_client_config=Config(read_timeout=300, connect_timeout=10),
    )


def load_agent_input(paths: AgentInputPaths | None = None) -> dict:
    """Load collector + analysis + decision trace for the schema designer."""
    paths = paths or AgentInputPaths.resolve()
    collector_path, analysis_path = paths.collector, paths.analysis
    trace_path = paths.decision_trace

    if not collector_path or not analysis_path:
        raise ValueError(
            "Collector/analysis paths must be set via run_elasticache_schema_agent() "
            "or COLLECTOR_OUTPUT_PATH/ANALYSIS_OUTPUT_PATH env vars"
        )

    with open(collector_path, encoding="utf-8") as f:
        raw_collector = json.load(f)
    collector = load_contract(CollectorOutputContract, raw_collector, artifact=collector_path)
    # Owner writes on the cached tables, read-only context for invalidation (#296);
    # the contract projection below would drop it.
    invalidation_context = raw_collector.get("cache_invalidation_context")

    with open(analysis_path, encoding="utf-8") as f:
        analysis = load_contract(AnalysisOutputContract, json.load(f), artifact=analysis_path)

    agent_collector, agent_analysis, agent_context = project_schema_design_input(
        collector, analysis, query_pattern_groups=listed_query_patterns(raw_collector)
    )

    decision_trace: dict = {}
    if trace_path and os.path.exists(trace_path):
        with open(trace_path, encoding="utf-8") as f:
            raw_trace = json.load(f)
        # A versioned contract since #1191: refuse another MAJOR; a legacy trace loads.
        check_contract_version(raw_trace, ElastiCacheDecisionTrace, artifact=trace_path)
        decision_trace = raw_trace

    print(
        f"[load_agent_input] Projected: {len(agent_collector.tables)} tables, "
        f"{len(agent_collector.queries.query_patterns)} patterns"
    )

    return {
        "collector": agent_collector.model_dump(mode="json"),
        "analysis": agent_analysis.model_dump(mode="json"),
        "context": agent_context.model_dump(mode="json"),
        "decision_trace": decision_trace,
        **({"cache_invalidation_context": invalidation_context} if invalidation_context else {}),
    }


def _compact_agent_input(agent_input: dict) -> None:
    """Compact agent input to reduce token count for inline prompt injection."""
    keep_fields = {
        "query_id",
        "query_text",
        "query_type",
        "tables_accessed",
        "calls_per_second",
        "frequency_per_hour",
        "rows_returned_avg",
        "has_joins",
        "join_count",
        "has_aggregations",
        "has_subqueries",
        "has_text_search",
        "has_time_range_filter",
        "filter_columns",
        "sort_columns",
    }
    for qp in agent_input.get("collector", {}).get("queries", {}).get("query_patterns") or []:
        text = qp.get("query_text") or ""
        if len(text) > 200:
            qp["query_text"] = text[:200] + "..."
        for key in list(qp.keys()):
            if key not in keep_fields:
                del qp[key]

    dt = agent_input.get("decision_trace") or {}
    dt.pop("query_matches", None)


# ---------------------------------------------------------------------------
# PE reviewer
# ---------------------------------------------------------------------------


def _invoke_pe_reviewer(
    model: RoleBedrockModel,
    design_output: ElastiCacheModelOutputContract,
    agent_input_summary: dict,
    pe_skill_path: str | None = None,
) -> PEReviewResult:
    """Invoke the PE reviewer agent on a design output."""
    print("[schema-design/elasticache][pe-review] Invoking PE reviewer...")
    pe_prompt_text = _load_skill(pe_skill_path or DEFAULT_PE_SKILL_PATH)

    pe_agent = Agent(
        model=model,
        system_prompt=pe_prompt_text,
        tools=[],
        structured_output_model=PEReviewResult,
        callback_handler=None,
    )

    design_json = design_output.model_dump(mode="json")

    prompt = (
        "Review the following ElastiCache/Redis schema design.\n\n"
        + "## Source Database Summary\n"
        + f"Key designs: {len(design_output.key_designs)}, "
        + f"Access patterns: {len(design_output.access_patterns)}, "
        + f"Tables: {agent_input_summary.get('table_count', 0)}\n\n"
        + "## Design Output\n"
        + frame_untrusted(
            json.dumps(design_json, indent=2, default=str),
            label="schema design output to review (JSON; echoes source names)",
        )
        + "\n\nEvaluate this design following your review process. "
        + "Return a PEReviewResult with your verdict and any change requests."
    )

    result = pe_agent(prompt)
    output = getattr(result, "structured_output", None)
    if isinstance(output, PEReviewResult):
        print(
            f"[schema-design/elasticache][pe-review] Verdict: {output.verdict.value} | "
            f"Changes: {len(output.change_requests)} | "
            f"Strengths: {len(output.strengths)}"
        )
        return output

    parsed = json.loads(str(result))
    return PEReviewResult.model_validate(parsed)


def _format_pe_feedback(review: PEReviewResult) -> str:
    lines = [f"## PE Review Summary\n{review.summary}\n"]
    if review.change_requests:
        lines.append("## Change Requests")
        for i, cr in enumerate(review.change_requests, 1):
            lines.append(
                f"{i}. [{cr.severity.value}] {cr.category.value} — {cr.target}\n"
                f"   Change: {cr.requested_change}\n"
                f"   Rationale: {cr.rationale}"
            )
    if review.strengths:
        lines.append("\n## Strengths\n" + "\n".join(f"- {s}" for s in review.strengths))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def run_elasticache_schema_agent(
    skill_path: str | None = None,
    pe_skill_path: str | None = None,
    collector_path: str | None = None,
    analysis_path: str | None = None,
    revision_context_path: str | None = None,
    decision_trace_path: str | None = None,
) -> tuple[ElastiCacheModelOutputContract, dict]:
    """Run the ElastiCache/Redis schema design agent with PE review loop.

    Uses SchemaDesignRunner for retry logic, graceful fallback on max_tokens,
    duplicate PE feedback detection, and consistent logging.

    Args:
        collector_path: Path to collector JSON (preferred over env var).
        analysis_path: Path to analysis JSON (preferred over env var).
        revision_context_path: Path to revision context JSON (optional).

    Returns:
        Tuple of (validated output, trace dict for S3 artifact).
    """
    # This call's own files (#1054): never module state, so concurrent group
    # designs cannot read each other's input.
    paths = AgentInputPaths.resolve(
        collector_path, analysis_path, decision_trace_path, revision_context_path
    )

    model = _build_model()
    system_prompt = (
        _load_skill(skill_path or DEFAULT_SKILL_PATH) + "\n\n" + SYSTEM_PROMPT_DATA_DIRECTIVE
    )

    print("[schema-design/elasticache] Loading agent input...")
    agent_input = load_agent_input(paths)

    _compact_agent_input(agent_input)
    input_json = json.dumps(agent_input, indent=2, default=str)
    print(f"[schema-design/elasticache] Compacted input: {len(input_json):,} chars")

    designer = Agent(
        model=model,
        system_prompt=system_prompt,
        tools=[],
        structured_output_model=ElastiCacheModelOutputContract,
        callback_handler=None,
    )

    designer_prompt = (
        "Here is the projected input for your ElastiCache/Redis schema design:\n\n"
        + frame_untrusted(input_json, label="projected schema-design input (JSON)")
        + "\n\n"
        + "The input includes collector output with source tables and query patterns, "
        "plus analysis output with detected Redis-suitable patterns (caching, session store, "
        "leaderboard, time-series, geospatial). "
        "Follow all phases in the skill prompt to design the Redis key schema. "
        "Map each use case to the optimal Redis data type. "
        "Return the complete ElastiCacheModelOutputContract."
    )

    if paths.revision_context:
        revision_ctx = json.loads(Path(paths.revision_context).read_text(encoding="utf-8"))
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
            # R1: customer revision free-text framed as requested design changes
            # (data), not as instructions to execute verbatim.
            designer_prompt += "\n\n---\n# REVISION CONTEXT\n" + frame_customer_requests(
                "\n\n".join(revision_sections)
            )

    input_summary = {
        "table_count": len(agent_input.get("collector", {}).get("tables", [])),
        "pattern_count": len(
            agent_input.get("collector", {}).get("queries", {}).get("query_patterns", [])
        ),
    }

    runner = SchemaDesignRunner(
        target_type="elasticache",
        output_model=ElastiCacheModelOutputContract,
        model=model,
        designer_agent=designer,
        pe_skill_path=pe_skill_path or DEFAULT_PE_SKILL_PATH,
        pe_reviewer_fn=_invoke_pe_reviewer,
        format_pe_feedback_fn=_format_pe_feedback,
    )

    return runner.run(designer_prompt, input_summary)
