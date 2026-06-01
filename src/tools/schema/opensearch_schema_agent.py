"""OpenSearch schema design agent — orchestrator with PE review loop.

Mirrors the DocumentDB schema agent pattern:
  1. Load projected input (collector + analysis + decision trace)
  2. LLM Designer produces OpenSearchModelOutputContract
  3. PE Reviewer evaluates the design
  4. If PE requests changes, Designer revises (max 3 iterations)
  5. Returns final output + trace log
"""

from __future__ import annotations

import json
import logging
import os
import time
from enum import Enum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from strands import Agent, tool
from strands.models.bedrock import BedrockModel

from src.contracts.analysis_output import AnalysisOutputContract
from src.contracts.collector_output import CollectorOutputContract
from src.contracts.opensearch_model_output import OpenSearchModelOutputContract
from src.contracts.schema_design_input import project_schema_design_input
from src.contracts.schema_design_output import TradeOff

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level path variables (set by run_opensearch_schema_agent, read by tools)
# ---------------------------------------------------------------------------
_collector_path: str | None = None
_analysis_path: str | None = None
_revision_context_path: str | None = None

MAX_PE_ITERATIONS = 2
DEFAULT_SKILL_PATH = str(
    Path(__file__).resolve().parent.parent.parent / "skills" / "opensearch-index-modeling.md"
)
DEFAULT_PE_SKILL_PATH = str(
    Path(__file__).resolve().parent.parent.parent / "skills" / "opensearch-pe-review.md"
)


# ---------------------------------------------------------------------------
# PE Review models
# ---------------------------------------------------------------------------


class ReviewVerdict(str, Enum):
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"


class ChangeSeverity(str, Enum):
    BLOCKER = "blocker"
    WARNING = "warning"


class ChangeCategory(str, Enum):
    FIELD_TYPE = "field_type"
    SHARD_SIZING = "shard_sizing"
    ISM_POLICY = "ism_policy"
    ACCESS_PATTERN = "access_pattern"
    ANALYZER = "analyzer"
    DATA_STREAM = "data_stream"


class ChangeRequest(BaseModel):
    category: ChangeCategory
    severity: ChangeSeverity
    target: str
    requested_change: str
    rationale: str

    model_config = ConfigDict(extra="ignore")


class PEReviewResult(BaseModel):
    verdict: ReviewVerdict
    change_requests: list[ChangeRequest] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    pe_notes: list[str] = Field(default_factory=list)
    summary: str = ""

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------


class SchemaDesignTrace:
    """Accumulates structured trace entries for the design loop."""

    def __init__(self) -> None:
        self.iterations: list[dict] = []
        self.start_time = time.time()

    def log_designer(
        self, iteration: int, duration_s: float, output: OpenSearchModelOutputContract
    ) -> None:
        entry = self._get_or_create(iteration)
        entry["designer"] = {
            "duration_seconds": round(duration_s, 2),
            "index_designs": len(output.index_designs),
            "data_stream_designs": len(output.data_stream_designs),
            "access_patterns": len(output.access_patterns),
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

    # Thinking config only supported on Opus models
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


@tool
def load_agent_input() -> dict:
    """Load collector + analysis + decision trace for the schema designer.

    Reads from paths set by run_opensearch_schema_agent (preferred) or
    env vars (fallback).
    """
    collector_path = _collector_path or os.environ.get("COLLECTOR_OUTPUT_PATH")
    analysis_path = _analysis_path or os.environ.get("ANALYSIS_OUTPUT_PATH")
    trace_path = os.environ.get("DECISION_TRACE_PATH")

    if not collector_path or not analysis_path:
        raise ValueError(
            "Collector/analysis paths must be set via run_opensearch_schema_agent() "
            "or COLLECTOR_OUTPUT_PATH/ANALYSIS_OUTPUT_PATH env vars"
        )

    with open(collector_path, encoding="utf-8") as f:
        collector = CollectorOutputContract.model_validate(json.load(f))

    with open(analysis_path, encoding="utf-8") as f:
        analysis = AnalysisOutputContract.model_validate(json.load(f))

    agent_collector, agent_analysis, agent_context = project_schema_design_input(
        collector, analysis
    )

    # Load decision trace (OpenSearch-specific enrichment with workload_classifications)
    decision_trace: dict = {}
    if trace_path and os.path.exists(trace_path):
        with open(trace_path, encoding="utf-8") as f:
            decision_trace = json.load(f)

    print(
        f"[load_agent_input] Projected: {len(agent_collector.tables)} tables, "
        f"{len(agent_collector.queries.query_patterns)} patterns, "
        f"{len(decision_trace.get('workload_classifications', []))} workload classifications"
    )

    return {
        "collector": agent_collector.model_dump(mode="json"),
        "analysis": agent_analysis.model_dump(mode="json"),
        "context": agent_context.model_dump(mode="json"),
        "decision_trace": decision_trace,
    }


def _invoke_designer(
    designer: Agent,  # type: ignore[type-arg]
    prompt: str,
) -> OpenSearchModelOutputContract:
    """Invoke the designer agent and extract structured output."""
    result = designer(prompt)
    output = getattr(result, "structured_output", None)
    if isinstance(output, OpenSearchModelOutputContract):
        return output
    parsed = json.loads(str(result))
    return OpenSearchModelOutputContract.model_validate(parsed)


def _invoke_pe_reviewer(
    model: BedrockModel,
    design_output: OpenSearchModelOutputContract,
    agent_input_summary: dict,
    pe_skill_path: str | None = None,
) -> PEReviewResult:
    """Invoke the PE reviewer agent on a design output."""
    print("[schema-design/opensearch][pe-review] Invoking PE reviewer...")
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
        "Review the following OpenSearch schema design.\n\n"
        f"## Source Database Summary\n"
        f"Index designs: {len(design_output.index_designs)}, "
        f"Data stream designs: {len(design_output.data_stream_designs)}, "
        f"Access patterns: {len(design_output.access_patterns)}, "
        f"Tables: {agent_input_summary['table_count']}\n\n"
        f"## Design Output\n```json\n{json.dumps(design_json, indent=2, default=str)}\n```\n\n"
        "Evaluate this design following your review process. "
        "Return a PEReviewResult with your verdict and any change requests."
    )

    result = pe_agent(prompt)
    output = getattr(result, "structured_output", None)
    if isinstance(output, PEReviewResult):
        print(
            f"[schema-design/opensearch][pe-review] Verdict: {output.verdict.value} | "
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


def run_opensearch_schema_agent(
    skill_path: str | None = None,
    pe_skill_path: str | None = None,
    collector_path: str | None = None,
    analysis_path: str | None = None,
    revision_context_path: str | None = None,
) -> tuple[OpenSearchModelOutputContract, dict]:
    """Run the OpenSearch schema design agent with PE review loop.

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

    trace = SchemaDesignTrace()
    model = _build_model()
    system_prompt = _load_skill(skill_path or DEFAULT_SKILL_PATH)

    designer = Agent(
        model=model,
        system_prompt=system_prompt,
        tools=[load_agent_input],
        structured_output_model=OpenSearchModelOutputContract,
        callback_handler=None,
    )

    designer_prompt = (
        "Use the load_agent_input tool to read the projected input. "
        "The input includes a decision_trace with workload_classifications that indicate "
        "whether each table is SEARCH, TIMESERIES, or NOT_SUITABLE. "
        "Design IndexMapping for SEARCH tables and DataStreamConfig for TIMESERIES tables. "
        "Skip NOT_SUITABLE tables entirely. "
        "Follow all rules in the skill prompt including shard sizing math, "
        "analyzer selection, and ISM policy design. "
        "Return the complete OpenSearchModelOutputContract."
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

    # --- Iteration 0: initial design ---
    print(f"[schema-design/opensearch] === Designer iteration 1/{MAX_PE_ITERATIONS} ===")
    t0 = time.time()
    design_output = _invoke_designer(designer, designer_prompt)
    elapsed = time.time() - t0
    trace.log_designer(0, elapsed, design_output)
    print(f"[schema-design/opensearch] Designer iteration 1 complete in {elapsed:.0f}s")

    input_summary = {
        "table_count": len(design_output.index_designs) + len(design_output.data_stream_designs)
    }

    # --- PE review loop ---
    for iteration in range(MAX_PE_ITERATIONS):
        print(f"[schema-design/opensearch] === PE review {iteration + 1}/{MAX_PE_ITERATIONS} ===")

        try:
            t0 = time.time()
            review = _invoke_pe_reviewer(model, design_output, input_summary, pe_skill_path)
            elapsed = time.time() - t0
            trace.log_pe_review(iteration, elapsed, review)
        except Exception as exc:
            print(f"[schema-design/opensearch] PE review failed: {exc} — accepting design")
            trace.log_pe_error(iteration, str(exc))
            break

        if review.verdict == ReviewVerdict.APPROVED:
            print(f"[schema-design/opensearch] PE approved on iteration {iteration + 1}")
            if review.pe_notes:
                design_output.trade_offs.extend(
                    TradeOff(
                        description=f"[PE note] {note}",
                        impact=note,
                        engine="opensearch",
                    )
                    for note in review.pe_notes
                )
            break

        if iteration + 1 >= MAX_PE_ITERATIONS:
            print("[schema-design/opensearch] Max PE iterations — accepting with notes")
            if review.pe_notes:
                design_output.trade_offs.extend(
                    TradeOff(
                        description=f"[PE note] {note}",
                        impact=note,
                        engine="opensearch",
                    )
                    for note in review.pe_notes
                )
            break

        # --- Designer revision ---
        feedback_text = _format_pe_feedback(review)
        prev_output_json = design_output.model_dump_json(indent=2)
        revision_prompt = (
            "The PE reviewer has requested changes to your OpenSearch design. "
            "Here is their feedback:\n\n"
            f"{feedback_text}\n\n"
            "Here is your previous output that needs revision:\n"
            f"```json\n{prev_output_json}\n```\n\n"
            "Apply ONLY the requested changes. Do not remove or restructure "
            "anything the PE approved. Return the complete revised "
            "OpenSearchModelOutputContract."
        )

        print(
            f"[schema-design/opensearch] === Designer revision {iteration + 2}/{MAX_PE_ITERATIONS}"
            f" ({len(review.change_requests)} changes requested) ==="
        )
        t0 = time.time()
        design_output = _invoke_designer(designer, revision_prompt)
        elapsed = time.time() - t0
        trace.log_designer(iteration + 1, elapsed, design_output)

    return design_output, trace.to_dict()
