"""DocumentDB schema design agent — uses SchemaDesignRunner for PE review loop.

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
from strands.models.bedrock import BedrockModel

from src.contracts.analysis_output import AnalysisOutputContract
from src.contracts.collector_output import CollectorOutputContract
from src.contracts.documentdb_model_output import DocumentDBModelOutputContract
from src.contracts.schema_design_input import project_schema_design_input
from src.tools.schema.base_schema_agent import SchemaDesignRunner

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level path variables (set by run_documentdb_schema_agent, read by tools)
# ---------------------------------------------------------------------------
_collector_path: str | None = None
_analysis_path: str | None = None
_revision_context_path: str | None = None

DEFAULT_SKILL_PATH = str(
    Path(__file__).resolve().parent.parent.parent / "skills" / "documentdb-data-modeling.md"
)
DEFAULT_PE_SKILL_PATH = str(
    Path(__file__).resolve().parent.parent.parent / "skills" / "documentdb-pe-review.md"
)


# ---------------------------------------------------------------------------
# DocumentDB-specific PE Review models
# ---------------------------------------------------------------------------


class ReviewVerdict(str, Enum):
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"


class ChangeSeverity(str, Enum):
    BLOCKER = "blocker"
    WARNING = "warning"


class ChangeCategory(str, Enum):
    DOCUMENT_SIZE = "document_size"
    EMBEDDING = "embedding"
    INDEXING = "indexing"
    ACCESS_PATTERN = "access_pattern"
    COMPATIBILITY = "compatibility"
    MIGRATION = "migration"


class ChangeRequest(BaseModel):
    category: ChangeCategory
    severity: ChangeSeverity
    target: str
    requested_change: str
    rationale: str

    model_config = ConfigDict(extra="ignore")


class PEReviewResult(BaseModel):
    """DocumentDB PE review result — compatible with SchemaDesignRunner."""

    verdict: ReviewVerdict
    change_requests: list[ChangeRequest] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    pe_notes: list[str] = Field(default_factory=list)
    summary: str = ""

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Trace (kept for backward compat with existing tests)
# ---------------------------------------------------------------------------


class SchemaDesignTrace:
    """Accumulates structured trace entries for the design loop."""

    def __init__(self) -> None:
        self.iterations: list[dict] = []
        self.start_time: float = 0.0

    def log_designer(
        self, iteration: int, duration_s: float, output: DocumentDBModelOutputContract
    ) -> None:
        entry = self._get_or_create(iteration)
        entry["designer"] = {
            "duration_seconds": round(duration_s, 2),
            "collections": len(output.collections),
            "access_patterns": len(output.access_patterns),
            "unsupported_patterns": len(output.unsupported_patterns),
            "validation_passed": output.validation_passed,
        }

    def log_pe_review(self, iteration: int, duration_s: float, review: PEReviewResult) -> None:
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
        import time

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
# Helpers
# ---------------------------------------------------------------------------


def _load_skill(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _build_model() -> BedrockModel:
    """Build the shared BedrockModel for both designer and PE agents."""
    from botocore.config import Config

    model_id = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-opus-4-6-v1")
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
        region_name=os.environ.get("AWS_REGION", "us-east-1"),
        additional_request_fields=additional_fields,
        boto_client_config=Config(read_timeout=300, connect_timeout=10),
    )


def load_agent_input() -> dict:
    """Load collector + analysis + decision trace for the schema designer.

    Reads from paths set by run_documentdb_schema_agent (preferred) or
    env vars (fallback).
    """
    collector_path = _collector_path or os.environ.get("COLLECTOR_OUTPUT_PATH")
    analysis_path = _analysis_path or os.environ.get("ANALYSIS_OUTPUT_PATH")
    trace_path = os.environ.get("DECISION_TRACE_PATH")

    if not collector_path or not analysis_path:
        raise ValueError(
            "Collector/analysis paths must be set via run_documentdb_schema_agent() "
            "or COLLECTOR_OUTPUT_PATH/ANALYSIS_OUTPUT_PATH env vars"
        )

    with open(collector_path, encoding="utf-8") as f:
        collector = CollectorOutputContract.model_validate(json.load(f))

    with open(analysis_path, encoding="utf-8") as f:
        analysis = AnalysisOutputContract.model_validate(json.load(f))

    agent_collector, agent_analysis, agent_context = project_schema_design_input(
        collector, analysis
    )

    decision_trace: dict = {}
    if trace_path and os.path.exists(trace_path):
        with open(trace_path, encoding="utf-8") as f:
            decision_trace = json.load(f)

    print(
        f"[load_agent_input] Projected: {len(agent_collector.tables)} tables, "
        f"{len(agent_collector.queries.query_patterns)} patterns, "
        f"{len(agent_analysis.aggregate_recommendations or [])} aggregates, "
        f"{len(decision_trace.get('embedding_candidates', []))} embedding candidates"
    )

    return {
        "collector": agent_collector.model_dump(mode="json"),
        "analysis": agent_analysis.model_dump(mode="json"),
        "context": agent_context.model_dump(mode="json"),
        "decision_trace": decision_trace,
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
        "text_search_type",
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
    model: BedrockModel,
    design_output: DocumentDBModelOutputContract,
    agent_input_summary: dict,
    pe_skill_path: str | None = None,
) -> PEReviewResult:
    """Invoke the PE reviewer agent on a design output."""
    print("[schema-design/documentdb][pe-review] Invoking PE reviewer...")
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
        "Review the following DocumentDB schema design.\n\n"
        f"## Source Database Summary\n"
        f"Collections: {len(design_output.collections)}, "
        f"Access patterns: {len(design_output.access_patterns)}, "
        f"Tables: {agent_input_summary.get('table_count', 0)}\n\n"
        f"## Design Output\n```json\n{json.dumps(design_json, indent=2, default=str)}\n```\n\n"
        "Evaluate this design following your review process. "
        "Return a PEReviewResult with your verdict and any change requests."
    )

    result = pe_agent(prompt)
    output = getattr(result, "structured_output", None)
    if isinstance(output, PEReviewResult):
        print(
            f"[schema-design/documentdb][pe-review] Verdict: {output.verdict.value} | "
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


def run_documentdb_schema_agent(
    skill_path: str | None = None,
    pe_skill_path: str | None = None,
    collector_path: str | None = None,
    analysis_path: str | None = None,
    revision_context_path: str | None = None,
) -> tuple[DocumentDBModelOutputContract, dict]:
    """Run the DocumentDB schema design agent with PE review loop.

    Uses SchemaDesignRunner for retry logic, graceful fallback on max_tokens,
    duplicate PE feedback detection, and consistent logging.

    Args:
        collector_path: Path to collector JSON (preferred over env var).
        analysis_path: Path to analysis JSON (preferred over env var).
        revision_context_path: Path to revision context JSON (optional).

    Returns:
        Tuple of (validated output, trace dict for S3 artifact).
    """
    global _collector_path, _analysis_path, _revision_context_path
    _collector_path = collector_path
    _analysis_path = analysis_path
    _revision_context_path = revision_context_path

    model = _build_model()
    system_prompt = _load_skill(skill_path or DEFAULT_SKILL_PATH)

    # Load input eagerly — don't rely on LLM calling the tool.
    print("[schema-design/documentdb] Loading agent input...")
    agent_input = load_agent_input()

    # Compact input to fit within Bedrock token limits.
    _compact_agent_input(agent_input)
    input_json = json.dumps(agent_input, indent=2, default=str)
    print(f"[schema-design/documentdb] Compacted input: {len(input_json):,} chars")

    designer = Agent(
        model=model,
        system_prompt=system_prompt,
        tools=[],
        structured_output_model=DocumentDBModelOutputContract,
        callback_handler=None,
    )

    designer_prompt = (
        "Here is the projected input for your DocumentDB schema design:\n\n"
        f"```json\n{input_json}\n```\n\n"
        "The input includes a decision_trace with pre-computed embedding candidates "
        "and denormalization strategies from the DocumentDB analysis agent. "
        "Use these as your starting point for collection design. "
        "Follow all phases in the skill prompt to design the DocumentDB data model. "
        "Return the complete DocumentDBModelOutputContract."
    )

    # Inject revision context if this is a revision-triggered redesign
    if _revision_context_path:
        revision_ctx = json.loads(Path(_revision_context_path).read_text(encoding="utf-8"))
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

    input_summary = {
        "table_count": len(agent_input.get("collector", {}).get("tables", [])),
        "pattern_count": len(
            agent_input.get("collector", {}).get("queries", {}).get("query_patterns", [])
        ),
        "aggregate_count": len(
            agent_input.get("analysis", {}).get("aggregate_recommendations") or []
        ),
    }

    runner = SchemaDesignRunner(
        target_type="documentdb",
        output_model=DocumentDBModelOutputContract,
        model=model,
        designer_agent=designer,
        pe_skill_path=pe_skill_path or DEFAULT_PE_SKILL_PATH,
        pe_reviewer_fn=_invoke_pe_reviewer,
        format_pe_feedback_fn=_format_pe_feedback,
    )

    return runner.run(designer_prompt, input_summary)
