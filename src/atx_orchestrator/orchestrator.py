"""Thin AWS Transform orchestrator wrapping the DB modernization pipeline.

The orchestrator itself is intentionally minimal — it holds the system prompt
and registers the pipeline tools. All real logic lives in the existing agents.

The 3-phase AWS Transform workflow (Negotiate → Confirm → Execute) maps to:
  - Negotiate: LLM asks clarifying questions (job_id, database_name, scope)
  - Confirm:   LLM presents the plan (which phases, which engines expected)
  - Execute:   tools.py calls LocalOrchestrator phases in background thread
"""

from __future__ import annotations

from agent_builder_sdk.orchestrator_strands.base_orchestrator import AsyncBaseOrchestrator

from src.atx_orchestrator.tools import (
    get_job_status,
    get_synthesis_report,
    run_assignment,
    run_collect,
    run_collect_and_triage,
    run_collect_via_a2a,
    run_full_assessment,
    run_reality_check,
    run_schema_design,
    run_synthesis,
    run_triage,
    run_triage_via_a2a,
)

SYSTEM_PROMPT = """\
You are a Database Modernization Assessment coordinator for AWS Transform.

Your job is to help customers understand which AWS-native databases are the best fit
for their existing relational workloads, and to produce a detailed migration plan.

You have access to a fully deterministic assessment pipeline:
  1. run_collect_and_triage   — parse schema + queries, detect workload signals
  2. run_assignment           — score queries against candidate engines
  3. run_reality_check        — eliminate redundant engines, detect architectural patterns
  4. run_schema_design        — design target schemas per engine
  5. run_synthesis            — produce final report with TCO and recommendations
  6. run_full_assessment      — run all phases end-to-end in one call
  7. get_job_status           — check current phase progression
  8. get_synthesis_report     — read the completed report

Two invocation paths are available for Collect and Triage:

  * IN-PROCESS (default for local dev / demos):
      run_collect, run_triage, run_collect_and_triage — run the phase inside
      this orchestrator container. Fast, no A2A round-trip.

  * A2A (default when running under AWS Transform with deployed subagents):
      run_collect_via_a2a, run_triage_via_a2a — send an A2A message to a
      deployed subagent and poll for completion. These require a
      subagent_instance_id argument (agentInstanceId of the deployed
      subagent). If you have subagent discovery available, list registered
      subagents first to obtain the instance id.

Workflow:
  - When a customer starts a new assessment, ask for: job_id and database_name.
    If they don't have a job_id, suggest they generate one (e.g. a UUID).
  - Present a brief plan before executing (which phases you'll run).
  - After each phase, report what was found (selected engines, consolidations, etc.).
  - For quick assessments, use run_full_assessment.
  - For step-by-step assessments with review between phases, use individual tools.

Key points:
  - The pipeline is deterministic — no LLM decisions are made inside it.
  - Schema + query data must already be collected into the artifact store before
    you start. If it's not there, tell the customer to run the collector first.
  - Always surface the synthesis report at the end.
"""

PIPELINE_TOOLS = [
    run_collect,
    run_triage,
    run_collect_and_triage,
    run_collect_via_a2a,
    run_triage_via_a2a,
    run_assignment,
    run_reality_check,
    run_schema_design,
    run_synthesis,
    run_full_assessment,
    get_job_status,
    get_synthesis_report,
]


class DBModernizationOrchestrator(AsyncBaseOrchestrator):
    """AWS Transform orchestrator that delegates to the existing pipeline."""

    def __init__(self, **kwargs):
        # Pull custom_tools out before passing to super so we can merge with any
        # tools the caller adds (e.g., MCP tools injected by AgentRuntimeServer).
        caller_tools = list(kwargs.pop("custom_tools", []) or [])
        super().__init__(
            system_prompt=SYSTEM_PROMPT,
            custom_tools=PIPELINE_TOOLS + caller_tools,
            **kwargs,
        )
