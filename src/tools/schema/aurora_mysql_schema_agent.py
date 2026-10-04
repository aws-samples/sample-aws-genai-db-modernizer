"""Aurora MySQL schema design agent — uses SchemaDesignRunner for PE review loop.

Flow:
  1. Load projected input (collector + analysis + decision trace)
  2. Run the deterministic core (source-family classification + DDL generation)
     and build a compact design view of the draft (issue #273)
  3. SchemaDesignRunner handles: designer invocation with retries,
     PE review loop, duplicate feedback detection, graceful fallback; the
     designer returns only an AuroraDesignDeltaContract
  4. Merge the delta into the draft deterministically (one correction round
     on merge errors) and return the full output contract + trace log
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

from src.agents.prompt_framing import (
    SYSTEM_PROMPT_DATA_DIRECTIVE,
    frame_customer_requests,
)
from src.contracts.analysis_output import AnalysisOutputContract
from src.contracts.aurora_design_delta import AuroraDesignDeltaContract
from src.contracts.aurora_mysql_model_output import AuroraMySQLModelOutputContract
from src.contracts.collector_output import CollectorOutputContract
from src.contracts.schema_design_input import project_schema_design_input
from src.tools.schema.aurora_common.bedrock_delta import designer_prompt as build_designer_prompt
from src.tools.schema.aurora_common.bedrock_delta import (
    merge_or_correct,
    pe_review_prompt,
    prepare_delta_design,
)
from src.tools.schema.base_schema_agent import SchemaDesignRunner

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level path variables (set by run_aurora_mysql_schema_agent, read by tools)
# ---------------------------------------------------------------------------
_collector_path: str | None = None
_analysis_path: str | None = None
_revision_context_path: str | None = None

DEFAULT_SKILL_PATH = str(
    Path(__file__).resolve().parent.parent.parent / "skills" / "aurora_mysql-data-modeling.md"
)
DEFAULT_PE_SKILL_PATH = str(
    Path(__file__).resolve().parent.parent.parent / "skills" / "aurora_mysql-pe-review.md"
)


# ---------------------------------------------------------------------------
# Aurora MySQL-specific PE Review models
# ---------------------------------------------------------------------------


class ReviewVerdict(str, Enum):
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"


class ChangeSeverity(str, Enum):
    BLOCKER = "blocker"
    WARNING = "warning"


class ChangeCategory(str, Enum):
    TYPE_MAPPING = "type_mapping"
    INTEGRITY = "integrity"
    INDEXING = "indexing"
    OPTIMIZATION = "optimization"
    FABRICATION = "fabrication"
    MIGRATION = "migration"


class ChangeRequest(BaseModel):
    category: ChangeCategory
    severity: ChangeSeverity
    target: str
    requested_change: str
    rationale: str

    model_config = ConfigDict(extra="ignore")


class PEReviewResult(BaseModel):
    """Aurora MySQL PE review result — compatible with SchemaDesignRunner."""

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

    Reads from paths set by run_aurora_mysql_schema_agent (preferred) or
    env vars (fallback).
    """
    collector_path = _collector_path or os.environ.get("COLLECTOR_OUTPUT_PATH")
    analysis_path = _analysis_path or os.environ.get("ANALYSIS_OUTPUT_PATH")
    trace_path = os.environ.get("DECISION_TRACE_PATH")

    if not collector_path or not analysis_path:
        raise ValueError(
            "Collector/analysis paths must be set via run_aurora_mysql_schema_agent() "
            "or COLLECTOR_OUTPUT_PATH/ANALYSIS_OUTPUT_PATH env vars"
        )

    with open(collector_path, encoding="utf-8") as f:
        raw_collector = json.load(f)
    collector = CollectorOutputContract.model_validate(raw_collector)

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
        f"{len(agent_collector.queries.query_patterns)} patterns"
    )

    return {
        "collector": agent_collector.model_dump(mode="json"),
        "analysis": agent_analysis.model_dump(mode="json"),
        "context": agent_context.model_dump(mode="json"),
        "decision_trace": decision_trace,
        # Raw source data_type per column: what residual type_rules match (#273).
        "raw_collector": raw_collector,
    }


# ---------------------------------------------------------------------------
# PE reviewer
# ---------------------------------------------------------------------------


def _invoke_pe_reviewer(
    model: BedrockModel,
    design_output: AuroraDesignDeltaContract,
    agent_input_summary: dict,
    pe_skill_path: str | None = None,
) -> PEReviewResult:
    """Invoke the PE reviewer agent on a design output."""
    print("[schema-design/aurora_mysql][pe-review] Invoking PE reviewer...")
    pe_prompt_text = _load_skill(pe_skill_path or DEFAULT_PE_SKILL_PATH)

    pe_agent = Agent(
        model=model,
        system_prompt=pe_prompt_text,
        tools=[],
        structured_output_model=PEReviewResult,
        callback_handler=None,
    )

    prompt = pe_review_prompt("aurora_mysql", design_output, agent_input_summary)

    result = pe_agent(prompt)
    output = getattr(result, "structured_output", None)
    if isinstance(output, PEReviewResult):
        print(
            f"[schema-design/aurora_mysql][pe-review] Verdict: {output.verdict.value} | "
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


def run_aurora_mysql_schema_agent(
    skill_path: str | None = None,
    pe_skill_path: str | None = None,
    collector_path: str | None = None,
    analysis_path: str | None = None,
    revision_context_path: str | None = None,
) -> tuple[AuroraMySQLModelOutputContract, dict]:
    """Run the Aurora MySQL schema design agent with PE review loop.

    Uses SchemaDesignRunner for retry logic, graceful fallback on max_tokens,
    duplicate PE feedback detection, and consistent logging.

    Before invoking the designer, runs the deterministic core (source-family
    classification + DDL generation). The designer sees a compact view of
    that draft and returns only a delta, which is merged into the draft
    deterministically (issue #273), so neither its prompt nor its output
    grows with the schema beyond one short line per column.

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
    system_prompt = (
        _load_skill(skill_path or DEFAULT_SKILL_PATH) + "\n\n" + SYSTEM_PROMPT_DATA_DIRECTIVE
    )

    # Load input eagerly — don't rely on LLM calling the tool.
    print("[schema-design/aurora_mysql] Loading agent input...")
    agent_input = load_agent_input()

    # Issue #273: the designer sees a compact view of the deterministic draft
    # and returns only a delta; the full contract is merged deterministically.
    base, view = prepare_delta_design("aurora_mysql", agent_input)
    if not base.tables:
        logger.warning(
            "[schema-design/aurora_mysql] Collector has no tables; "
            "there is no draft to design against."
        )
        raise ValueError("Aurora MySQL schema design has no tables to design")
    print(
        f"[schema-design/aurora_mysql] Design view: "
        f"{len(json.dumps(view, default=str)):,} chars for {len(base.tables)} tables"
    )

    designer = Agent(
        model=model,
        system_prompt=system_prompt,
        tools=[],
        structured_output_model=AuroraDesignDeltaContract,
        callback_handler=None,
    )

    designer_prompt = build_designer_prompt("aurora_mysql", view)

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
            # R1: customer revision free-text framed as requested design changes
            # (data), not as instructions to execute verbatim.
            designer_prompt += "\n\n---\n# REVISION CONTEXT\n" + frame_customer_requests(
                "\n\n".join(revision_sections)
            )

    input_summary = {"table_count": len(base.tables), "design_view": view}

    runner = SchemaDesignRunner(
        target_type="aurora_mysql",
        output_model=AuroraDesignDeltaContract,
        model=model,
        designer_agent=designer,
        pe_skill_path=pe_skill_path or DEFAULT_PE_SKILL_PATH,
        pe_reviewer_fn=_invoke_pe_reviewer,
        format_pe_feedback_fn=_format_pe_feedback,
    )

    delta: AuroraDesignDeltaContract
    delta, trace = runner.run(designer_prompt, input_summary)
    output = merge_or_correct(
        "aurora_mysql", base, delta, runner, AuroraMySQLModelOutputContract, trace
    )
    return output, trace
