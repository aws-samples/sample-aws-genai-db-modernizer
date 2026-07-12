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
from agent_builder_sdk.orchestrator_strands.tools.subagent_registry_tools import (
    SubagentRegistryTools,
)

from src.atx_orchestrator.tools import (
    get_job_status,
    get_synthesis_report,
    run_analysis_dynamodb_via_a2a,
    run_assignment,
    run_collect_via_a2a,
    run_full_assessment,
    run_reality_check,
    run_schema_design,
    run_synthesis,
    run_triage_via_a2a,
)

# Instantiate the SDK-provided subagent registry tools once at module load.
# The init is trivial (only logs); the actual API call happens when
# discover_subagents is invoked.
_subagent_registry = SubagentRegistryTools()
discover_subagents = _subagent_registry.discover_subagents

SYSTEM_PROMPT = """\
You are a Database Modernization Assessment coordinator for AWS Transform.

Your job is to help customers understand which AWS-native databases are the best fit
for their existing relational workloads, and to produce a detailed migration plan.

You have access to a fully deterministic assessment pipeline. The Collector,
Triage, and DynamoDB Analysis phases run in DEPLOYED SUBAGENTS via the AWS
Transform A2A (agent-to-agent) protocol — you invoke them by name, and the
runtime handles instance spawning and message dispatch. Later phases run
in-process for now.

  1. run_collect_via_a2a          — invokes the db-modernization-collector subagent
                                    to parse schema + queries from an offline JSON.
  2. run_triage_via_a2a           — invokes the db-modernization-triage subagent
                                    to detect workload signals + select candidate engines.
  3. run_analysis_dynamodb_via_a2a — invokes the db-modernization-analysis-dynamodb
                                    subagent to score every table against DynamoDB
                                    patterns and produce cost + design recommendations.
  4. run_assignment               — score queries against candidate engines.
  5. run_reality_check            — eliminate redundant engines, detect architectural
                                    patterns.
  6. run_schema_design            — design target schemas per engine.
  7. run_synthesis                — produce final report with TCO and recommendations.
  8. run_full_assessment          — run all phases end-to-end in one call.
  9. get_job_status               — check current phase progression.
 10. get_synthesis_report         — read the completed report.

Subagent invocation:
  The A2A tools resolve subagents BY NAME (e.g. "db-modernization-collector")
  and the AWS Transform runtime spawns instances transparently — you never
  need to look up instance IDs yourself.

Workflow:
  - When a customer starts a new assessment, ask for: job_id and database_name.
    If they don't have a job_id, suggest they generate one (e.g. a UUID).
  - Present a brief plan before executing (which phases you'll run).
  - After each phase, report what was found (selected engines, consolidations, etc.).
  - For quick assessments, use run_full_assessment.
  - For step-by-step assessments with review between phases, use individual tools.

Key points:
  - The pipeline is deterministic — no LLM decisions are made inside it.
  - Schema + query data must already be uploaded to the customer's artifact
    store before you start (an ``offline-collection.json`` file in S3). The
    Collector subagent ingests this file. If it's not there, tell the
    customer to upload it first.
  - Always surface the synthesis report at the end.
"""

PIPELINE_TOOLS = [
    run_collect_via_a2a,
    run_triage_via_a2a,
    run_analysis_dynamodb_via_a2a,
    run_assignment,
    run_reality_check,
    run_schema_design,
    run_synthesis,
    run_full_assessment,
    get_job_status,
    get_synthesis_report,
    # NOTE: discover_subagents omitted intentionally. As of SDK v1.0.2 it
    # returns a hardcoded MOCK ("dynamic-showcase-subagent" weather agent),
    # which caused the LLM to mis-conclude that our real subagents weren't
    # deployed when a2a discovery failed for other reasons.
    # Re-add when the SDK ships the real registry-backed implementation.
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
