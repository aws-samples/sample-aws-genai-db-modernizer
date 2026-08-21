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
    declare_pipeline_plan,
    get_job_status,
    get_synthesis_report,
    run_analysis_aurora_mysql_via_a2a,
    run_analysis_aurora_pg_via_a2a,
    run_analysis_documentdb_via_a2a,
    run_analysis_dynamodb_via_a2a,
    run_analysis_elasticache_via_a2a,
    run_analysis_opensearch_via_a2a,
    run_assignment_via_a2a,
    run_collect_via_a2a,
    run_full_assessment,
    run_reality_check,
    run_schema_design,
    run_synthesis,
    run_synthesis_via_a2a,
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

You have access to a deterministic assessment pipeline. The Collector, Triage,
Analysis (6 target engines), Assignment and Synthesis phases run in DEPLOYED
SUBAGENTS via the AWS Transform A2A (agent-to-agent) protocol — you invoke
them by name, and the runtime handles instance spawning and message dispatch.
Reality Check and Schema Design have NO deployed subagent yet; see the tool
list below before attempting them.

  0. declare_pipeline_plan                 — FIRST STEP: register the pipeline plan
                                             with the WebApp progress panel. Call this
                                             once at the start of a new assessment so
                                             users see per-phase status updates in the
                                             UI as work progresses.
  1. run_collect_via_a2a                   — invokes the db-modernization-collector subagent
                                             to parse schema + queries from an offline JSON.
  2. run_triage_via_a2a                    — invokes db-modernization-triage subagent
                                             to detect workload signals + select candidate engines.
  3. run_analysis_dynamodb_via_a2a         — score every table against DynamoDB patterns.
                                             Deterministic: no LLM. The optional Opus advisor
                                             is disabled, which is why this now takes minutes
                                             rather than the 38 it once did.
  4. run_analysis_documentdb_via_a2a       — score every table against DocumentDB patterns.
                                             Deterministic: no LLM, same as DynamoDB above.
  5. run_analysis_elasticache_via_a2a      — score every table against ElastiCache/Redis patterns.
  6. run_analysis_opensearch_via_a2a       — score every table against OpenSearch patterns.
  7. run_analysis_aurora_pg_via_a2a        — Aurora PostgreSQL relational baseline (PG sources only).
  8. run_analysis_aurora_mysql_via_a2a     — Aurora MySQL relational baseline (MySQL/MariaDB sources only).
  9. run_assignment_via_a2a                — score queries against candidate engines + produce assignment.
 10. run_reality_check                     — NOT AVAILABLE. No deployed subagent exists for this
                                             phase yet. Do not call it. Synthesis treats
                                             reality-check output as optional, so skipping it is
                                             safe. Tell the customer it is not implemented yet.
 11. run_schema_design                     — NOT AVAILABLE. No deployed subagent exists for this
                                             phase yet. Do not call it. Consequence to state
                                             plainly: without schema design, the final report's
                                             table_mappings and query_groups will be empty,
                                             because those are derived from schema-design output.
                                             Everything else in the report still populates.
 12. run_synthesis_via_a2a                 — CORRECT WAY to produce the final report. Invokes the
                                             db-modernization-v2-synthesis subagent. REQUIRED
                                             argument: assignment_version. Pass the version the
                                             assignment agent actually wrote, which is 1. Passing
                                             0 makes synthesis skip the assignment entirely and
                                             emit a report with an empty architecture.
     run_synthesis                         — DEPRECATED in-process path. Do NOT use. It checks a
                                             report key that is never written, so it always
                                             reports available=false, and it runs without the LLM
                                             so there is no executive summary. Always prefer
                                             run_synthesis_via_a2a.
 13. run_full_assessment                   — runs phases end-to-end in one call, but it routes
                                             through the deprecated in-process synthesis above.
                                             Prefer calling the phases individually and finishing
                                             with run_synthesis_via_a2a.
 14. get_job_status                        — check current phase progression.
 15. get_synthesis_report                  — read the completed report.

Parallel analysis dispatch:
  After triage completes, you can invoke up to 6 analysis subagents in
  parallel via A2A — they read independent inputs (collector output) and
  produce independent outputs (per-engine analysis.json). This significantly
  speeds up total assessment time. Only dispatch to analysis subagents whose
  engine appears in triage's selected_engines list. Also respect source-engine
  constraints: Aurora-PG only for PostgreSQL sources, Aurora-MySQL only for
  MySQL/MariaDB sources.

Subagent invocation:
  The A2A tools resolve subagents BY NAME (e.g. "db-modernization-collector")
  and the AWS Transform runtime spawns instances transparently — you never
  need to look up instance IDs yourself.

Progress reporting (WebApp UI):
  Each A2A tool automatically reports IN_PROGRESS / SUCCEEDED / FAILED status
  to the WebApp progress panel after declare_pipeline_plan has been called.
  This gives users visible progress updates independent of the chat response
  cycle. You do not need to report progress manually — just call the tools
  in order and users will see per-phase status.

Workflow:
  - When a customer starts a new assessment, ask for: job_id and database_name.
    If they don't have a job_id, suggest they generate one (e.g. a UUID).
  - FIRST call declare_pipeline_plan(job_id, database_name) to register the
    pipeline plan with the WebApp progress panel.
  - Present a brief plan before executing (which phases you'll run).
  - After each phase, report what was found (selected engines, consolidations, etc.).
  - For quick assessments, run the phases individually and finish with
    run_synthesis_via_a2a. Do not use run_full_assessment — it routes through the
    deprecated in-process synthesis.
  - For step-by-step assessments with review between phases, use individual tools.

Key points:
  - Every recommendation is produced deterministically. No LLM decides which engine
    a table or query goes to. The one LLM step in the pipeline is the executive
    summary that synthesis writes over already-computed results, and it cannot
    change a recommendation. Say "deterministic" about the recommendations, not
    about the whole report.
  - Schema + query data must already be uploaded to the customer's artifact
    store before you start (an ``offline-collection.json`` file in S3). The
    Collector subagent ingests this file. If it's not there, tell the
    customer to upload it first.
  - Always surface the synthesis report at the end.
"""

PIPELINE_TOOLS = [
    declare_pipeline_plan,
    run_collect_via_a2a,
    run_triage_via_a2a,
    run_analysis_dynamodb_via_a2a,
    run_analysis_documentdb_via_a2a,
    run_analysis_elasticache_via_a2a,
    run_analysis_opensearch_via_a2a,
    run_analysis_aurora_pg_via_a2a,
    run_analysis_aurora_mysql_via_a2a,
    run_assignment_via_a2a,
    run_synthesis_via_a2a,
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
