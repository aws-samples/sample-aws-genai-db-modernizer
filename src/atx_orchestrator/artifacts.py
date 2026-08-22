"""Publish phase outputs to the AWS Transform Artifacts panel.

The pipeline's system of record is S3, written through ``ArtifactStore``
(``S3ArtifactStore`` is a raw ``put_object``). That makes an object durable but
gives the platform nothing to show: no artifact record, no download link, no
entry in the Artifacts panel. Registering a platform artifact is a separate call,
and until 2026-08-21 nothing in ``src/atx_orchestrator/`` made it — which is why
a successful synthesis run produced a report the customer could not reach.

This is a port of the shape already working in
``docdb-mig-exp-atx/src/atx_orchestrator/sizing.py`` (lines 159-183). Two
properties of that implementation are deliberate and preserved here:

**The S3 copy is written first and independently.** Callers persist through the
store, then publish. If publishing fails the data is already safe.

**Publishing never raises.** A failure to register an artifact must not fail a
phase whose real work succeeded. That mistake was made once already, in the
synthesis guard, where an exception after the report was durable turned a
successful run into a reported failure and left the agent telling the customer no
report existed. Here a failure logs a warning and returns an empty mapping.

Not yet implemented: worklogs and ``plan_step_id``. An artifact carries a
download link in the Artifacts panel; the *worklog* is a second index that makes
it appear in the step narrative. The reference implementation does not do this
either, so it is unproven and left as follow-up.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Literal

logger = logging.getLogger(__name__)

# The SDK types these as Literals, so they are the authoritative enums — more
# reliable than copying whatever the reference implementation happened to pass.
# Surfaced by mypy on 2026-08-21; note TXT, not TEXT.
CategoryType = Literal[
    "AGENT_INPUT",
    "AGENT_OUTPUT",
    "CUSTOMER_INPUT",
    "CUSTOMER_OUTPUT",
    "HITL_FROM_AGENT",
    "HITL_FROM_USER",
    "INTERNAL",
    "PLAN_STEP_OUTPUT",
    "PLAN_STEP_SUMMARY",
    "STATE",
]
FileType = Literal["CSV", "HTML", "JSON", "MARKDOWN", "OTHER", "PDF", "PPTX", "TXT", "XLSX", "ZIP"]


def publish(items: list[tuple[bytes, FileType, str, CategoryType]]) -> dict[str, str]:
    """Register content with the platform so it appears in the Artifacts panel.

    Args:
        items: ``(content, file_type, label, category_type)`` tuples. ``label`` is
            what the customer sees in the panel, so write it for them.

    Returns:
        ``{label: artifact_id}`` for whatever uploaded. Empty when running outside
        the ATX runtime, or when publishing failed. **Never raises** — the caller's
        S3 copy is the system of record and its phase must not fail over this.

    Each item is uploaded independently so one rejection does not lose the rest.
    That matters because ``category_type`` is caller-role-scoped: a category valid
    from the agent side may be refused from the operator side, and the warning names
    the category so the cause is visible rather than silent.
    """
    published: dict[str, str] = {}
    try:
        from agent_builder_sdk.agentic_framework.artifact_store import ArtifactStore
        from agent_builder_sdk.agentic_framework.client_factory import get_agentic_api_client
        from agent_builder_sdk.agentic_framework.common import calculate_digest
        from agent_builder_sdk.env_var import get_agent_context_from_env

        ctx = get_agent_context_from_env()
        store = ArtifactStore(
            workspace_id=ctx.workspace_id,
            job_id=ctx.job_id,
            agent_instance_id=ctx.agent_instance_id,
            client=get_agentic_api_client(),
        )
        for content, file_type, label, category in items:
            try:
                artifact_id = store.upload_artifact(
                    content,
                    calculate_digest(content),
                    category_type=category,
                    file_type=file_type,
                    label=label,
                )
                published[label] = artifact_id
                logger.info(
                    "Published artifact: label=%r type=%s category=%s bytes=%d id=%s",
                    label,
                    file_type,
                    category,
                    len(content),
                    artifact_id,
                )
            except Exception as exc:  # noqa: BLE001
                # One artifact failing must not lose the others.
                logger.warning(
                    "Artifact upload failed for %r (type=%s category=%s): %s",
                    label,
                    file_type,
                    category,
                    exc,
                )
    except Exception as exc:  # noqa: BLE001
        # Expected outside the ATX runtime (no agent context env vars), e.g. local
        # runs and tests. Also catches SDK or client construction failure.
        logger.warning("Artifact publishing unavailable, S3 copies still written: %s", exc)
    return published


def render_synthesis_markdown(
    report: dict[str, Any],
    warnings: list[str],
    trust_generated_summary: bool = True,
) -> str:
    """Render a synthesis report as Markdown a customer can actually read.

    The JSON is the machine artifact. This is the human one. It deliberately
    reports only what the report contains and states the gaps outright rather than
    leaving a reader to wonder why a section is empty — the same discipline the
    synthesis system prompt applies to narration.

    Args:
        trust_generated_summary: When False, use ``summary_deterministic`` instead
            of the generated ``summary`` and say why.

            This exists because of a measured failure, not a hypothetical one. On
            job v2-e2e-01 the generated summary asserted *"The schema design I
            produced resolves every flagged capability gap"* and *"The schema work
            is done, the access patterns are mapped, and the team is ready to
            build"* — on a run where schema-design never executed. The risk
            entries say "0% of queries resolved by schema design", and the model
            narrated that as completed work.

            The exec-summary prompt lives upstream in core-modernizer's
            ``run_synthesis``, so this cannot be fixed from here. What can be
            fixed is refusing to put an unsupported claim about deliverables in
            front of a customer. The generated text is still preserved verbatim in
            the JSON artifact; only the human-readable deliverable substitutes it.
    """
    db = report.get("database_name", "?")
    job = report.get("job_id", "?")
    arch = report.get("recommended_architecture") or {}
    risk = report.get("risk_assessment") or {}
    ranking = report.get("ranking") or []
    assignment = report.get("assignment_summary") or {}

    out: list[str] = [
        f"# Database Modernization Assessment: {db}",
        "",
        f"Job `{job}`  |  architecture **{arch.get('architecture_type', 'not determined')}**"
        f"  |  overall risk **{risk.get('overall_risk_level', 'not assessed')}**",
        "",
    ]

    if trust_generated_summary:
        summary = report.get("summary") or report.get("summary_deterministic")
        if summary:
            out += ["## Executive summary", "", summary.strip(), ""]
    else:
        deterministic = report.get("summary_deterministic")
        if deterministic:
            out += ["## Summary", "", deterministic.strip(), ""]
        out += [
            "> The generated narrative summary was withheld from this document "
            "because it referenced schema design work that this run did not "
            "perform. The measured figures above and below are unaffected. The "
            "generated text is retained in the JSON artifact for reference.",
            "",
        ]

    if ranking:
        out += [
            "## Engine ranking",
            "",
            "| Engine | Confidence | Queries | Share | Tables analysed | Est. monthly |",
            "|---|---|---|---|---|---|",
        ]
        for e in ranking:
            cost = e.get("monthly_cost_usd")
            out.append(
                f"| {e.get('target', '?')} "
                f"| {e.get('confidence_score', '-')} "
                f"| {e.get('assigned_queries', '-')} "
                f"| {e.get('workload_percent', '-')}% "
                f"| {e.get('tables_analyzed', '-')} "
                f"| {'$' + format(cost, ',.2f') if isinstance(cost, (int, float)) else '-'} |"
            )
        out.append("")

    if assignment:
        out += [
            "## Query assignment",
            "",
            f"Assignment version {assignment.get('version', '?')}, "
            f"{assignment.get('query_count', '?')} queries "
            f"({assignment.get('in_scope_count', '?')} in scope), "
            f"{assignment.get('co_dependency_groups', '?')} co-dependency groups.",
            "",
        ]

    risks = risk.get("risks") or []
    if risks:
        out += [f"## Risks ({len(risks)})", ""]
        for r in risks:
            if isinstance(r, dict):
                sev = r.get("severity") or r.get("level") or ""
                desc = r.get("description") or r.get("risk") or json.dumps(r)[:200]
                out.append(f"- **{sev}** {desc}" if sev else f"- {desc}")
            else:
                out.append(f"- {r}")
        out.append("")

    if warnings:
        out += ["## Known gaps in this report", ""]
        out += [f"- {w}" for w in warnings]
        out.append("")

    out += [
        "---",
        "",
        "Engine and query assignments in this report are produced deterministically. "
        "No language model decides which engine a table or query goes to. The "
        "executive summary is written over already-computed results and cannot change "
        "a recommendation.",
        "",
    ]
    return "\n".join(out)
