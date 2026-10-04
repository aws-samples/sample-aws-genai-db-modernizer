"""Bedrock-side delta design for both Aurora agents (issue #273).

Same contract as the external path: the designer gets the compact design view
and returns an ``AuroraDesignDeltaContract``; the PE reviewer reviews that
delta; the merged full contract is built deterministically at the end. Neither
the designer's prompt nor its structured output grows with the column count
beyond one short line per column in the view.
"""

from __future__ import annotations

import json
import logging

from pydantic import BaseModel

from src.agents.prompt_framing import frame_untrusted
from src.contracts.aurora_design_delta import AuroraDesignDeltaContract
from src.contracts.schema_design_input import AgentAnalysisInput, AgentCollectorInput
from src.tools.schema.aurora_common.delta_merge import AuroraDesignBase, merge_design_delta
from src.tools.schema.aurora_common.design_view import build_design_view

logger = logging.getLogger(__name__)

_ENGINE_LABELS = {"aurora_postgresql": "Aurora PostgreSQL", "aurora_mysql": "Aurora MySQL"}


def prepare_delta_design(engine: str, agent_input: dict) -> tuple[AuroraDesignBase, dict]:
    """``(base, design_view)`` from a loaded agent input (``collector`` is the projection)."""
    collector = AgentCollectorInput.model_validate(agent_input.get("collector", {}))
    analysis_raw = agent_input.get("analysis") or None
    analysis = AgentAnalysisInput.model_validate(analysis_raw) if analysis_raw else None
    raw_collector = agent_input.get("raw_collector") or {}
    base = AuroraDesignBase.from_inputs(engine, collector, raw_collector)
    view = build_design_view(base, collector, analysis, raw_collector=raw_collector)
    return base, view


def designer_prompt(engine: str, view: dict) -> str:
    label = _ENGINE_LABELS[engine]
    return (
        f"Here is the compact design view of the deterministic {label} draft:\n\n"
        + frame_untrusted(json.dumps(view, indent=2, default=str), label="design view (JSON)")
        + "\n\n"
        + f"The migration_strategy is '{view['migration_strategy']}'. The draft is "
        "authoritative and is merged with your answer deterministically. Return ONLY an "
        'AuroraDesignDeltaContract (delta_version "1.0"): type_rules for the residual '
        "source types listed in residual_types, per-table column_types only where one "
        "column needs a different type, index add/modify/remove only where a hot query "
        "needs it, plus optimizations, app_layer_notes and at least one trade-off. Never "
        "repeat unchanged tables, columns or DDL."
    )


def pe_review_prompt(engine: str, delta: BaseModel, input_summary: dict) -> str:
    label = _ENGINE_LABELS[engine]
    view = input_summary.get("design_view") or {}
    return (
        f"Review the following {label} schema design. It is a delta against the "
        "deterministic draft: tables, columns and DDL not listed carry over unchanged.\n\n"
        + "## Source Database Summary\n"
        + f"Tables: {input_summary.get('table_count', 0)}, "
        + f"Migration strategy: {view.get('migration_strategy', '?')}, "
        + f"Residual types: {json.dumps(view.get('residual_types', []), default=str)}\n\n"
        + "## Design Delta\n"
        + frame_untrusted(
            json.dumps(delta.model_dump(mode="json"), indent=2, default=str),
            label="schema design delta to review (JSON; echoes source names)",
        )
        + "\n\nEvaluate this design following your review process. "
        + "Return a PEReviewResult with your verdict and any change requests."
    )


def merge_or_correct[T: BaseModel](
    engine: str,
    base: AuroraDesignBase,
    delta: AuroraDesignDeltaContract,
    runner,
    output_model: type[T],
    trace: dict,
) -> T:
    """Merge the delta; on merge errors ask the designer once to fix them, then merge leniently.

    A lenient merge skips the invalid entries and records them in
    ``validation_failures`` with ``validation_passed=false``.
    """
    merged = merge_design_delta(base, delta)
    if merged.errors:
        logger.warning("[schema-design/%s] delta merge errors: %s", engine, merged.errors)
        prompt = (
            "Your design delta references things the draft does not have:\n- "
            + "\n- ".join(merged.errors)
            + "\n\nReturn the corrected complete AuroraDesignDeltaContract."
        )
        try:
            delta = runner._invoke_designer(prompt, previous_output=delta)
        except Exception as exc:  # keep the first delta; lenient merge records the errors
            logger.warning("[schema-design/%s] delta correction failed: %s", engine, exc)
        merged = merge_design_delta(base, delta, strict=False)
    trace["delta_merge"] = {"summary": merged.summary, "errors": merged.errors}
    return output_model.model_validate(merged.output)
