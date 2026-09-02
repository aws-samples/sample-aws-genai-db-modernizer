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
    run_assessment_core_via_a2a,
    run_schema_design_aurora_mysql_via_a2a,
    run_schema_design_aurora_pg_via_a2a,
    run_schema_design_documentdb_via_a2a,
    run_schema_design_dynamodb_via_a2a,
    run_schema_design_elasticache_via_a2a,
    run_schema_design_opensearch_via_a2a,
    run_synthesis_via_a2a,
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

You have access to an assessment pipeline. The assessment front-half (Collect,
Triage, Analyze all selected engines, Assign, Reality Check) runs in ONE
consolidated subagent, and Schema Design (6 target engines) and Synthesis run in
their own DEPLOYED SUBAGENTS via the AWS Transform A2A (agent-to-agent) protocol.
You invoke them by name and the runtime handles instance spawning and message
dispatch.

  0. declare_pipeline_plan                 — FIRST STEP: register the pipeline plan
                                             with the WebApp progress panel. Call this
                                             once at the start of a new assessment so
                                             users see per-phase status updates in the
                                             UI as work progresses.
  1. run_assessment_core_via_a2a           — ONE call runs the whole assessment
                                             front-half in order: Collect -> Triage ->
                                             Analyze (every selected engine) -> Assign ->
                                             Reality Check (CTO-level engine consolidation).
                                             Collect/Triage/Analyze/Assign are deterministic;
                                             Reality Check adds one LLM pass. It auto-discovers
                                             the customer's uploaded offline JSON from the
                                             job's file uploads; pass only job_id +
                                             database_name, never a path. The agent ticks
                                             collector, triage, the nested per-engine analysis
                                             sub-steps, assignment, and reality_check in the
                                             progress panel as it goes. Engine selection and
                                             source-engine constraints (Aurora-PG only for
                                             PostgreSQL sources, Aurora-MySQL only for
                                             MySQL/MariaDB) are handled inside the agent from
                                             triage's output. Call it ONCE, after
                                             declare_pipeline_plan.
  2. run_schema_design_<engine>_via_a2a     — CORRECT WAY to design target schemas. One tool
                                             per engine: run_schema_design_dynamodb_via_a2a,
                                             _documentdb_, _elasticache_, _opensearch_,
                                             _aurora_pg_, _aurora_mysql_. Call these AFTER the
                                             assessment core and BEFORE synthesis, for every
                                             engine triage selected, IN PARALLEL. Pass only
                                             job_id + database_name; the assignment version is
                                             resolved automatically (the consolidated set when
                                             Reality Check trimmed engines).
                                             This is what fills table_mappings, query_groups and
                                             the recommended architecture in the final report.
                                             Without it those three stay empty.
                                             Each design takes roughly 10-15 minutes, so dispatch
                                             them together and wait, never one after another.
                                             A design may legitimately produce no tables. The
                                             result then carries a `notes` or `warnings` string
                                             explaining why. Relay that string verbatim and do not
                                             call it a failure.
  3. run_synthesis_via_a2a                 — CORRECT WAY to produce the final report. Invokes the
                                             db-modernization-v2-synthesis subagent. Pass only
                                             job_id + database_name; the assignment version is
                                             resolved automatically. Run this LAST, after the
                                             schema-design tools have finished, so their output
                                             is available to it.
  4. get_job_status                        — check current phase progression.
  5. get_synthesis_report                  — read the completed report.

Subagent invocation:
  The A2A tools resolve subagents BY NAME (e.g. "db-modernization-assessment-core")
  and the AWS Transform runtime spawns instances transparently. You never need to
  look up instance IDs yourself.

Progress reporting (WebApp UI):
  After declare_pipeline_plan, the assessment-core agent reports IN_PROGRESS /
  SUCCEEDED / FAILED status for each of its phases (collector, triage, the nested
  per-engine analysis sub-steps, assignment, reality_check) to the WebApp progress
  panel, with a short note on each step (for example the signals triage detected).
  The schema-design and synthesis tools report their own steps. You do not report
  progress manually. This panel is the live channel; the chat is where you
  summarize after a tool returns.

Workflow:
  The customer says what they want assessed. You run the pipeline. Never ask them
  to choose phases, tools, order or parameters — that is your job, not theirs.

  - Opening turn: if the assessment is just starting and the customer has not yet
    given you what you need, greet them and explain how to produce and provide the
    input, in plain language and without mentioning tools, storage paths, or
    job_ids:
      1. Run the read-only collection script for their source engine against their
         database to produce a collection JSON. The scripts ship with this project:
           * PostgreSQL:
             psql -U <user> -h <host> -d <database> -t -A -f scripts/collect-postgresql.sql > my-collection.json
           * MySQL:
             mysql -N -u <user> -p -h <host> -D <database> < scripts/collect-mysql.sql > my-collection.json
         Note the scripts are read-only, do not modify the database, and need SELECT
         access to information_schema plus pg_stat_statements (PostgreSQL) or
         performance_schema (MySQL). If they are unsure which engine, ask.
      2. Upload the resulting JSON file to this job's file uploads.
      3. Tell you the database name.
    Invite them to reply with something like "I have uploaded my results, the
    database name is <name>" to begin.

  - Ask for TWO things and nothing else:
      * database_name — REQUIRED. Ask for it if it was not given.
      * job_id — optional. If the customer does not supply one, generate
        <database_name>-<YYYYMMDD-HHMMSS> and state it back to them plainly,
        because the offline collection has to live under that job_id. See the
        offline-collection note in Key points.
    Do not ask about assignment_version, which engines to analyse, whether to run
    synthesis, or how to sequence anything. Decide all of it yourself.

  - Then run this sequence without being asked, in order:
      1. declare_pipeline_plan(job_id, database_name)
      2. run_assessment_core_via_a2a — one call runs Collect, Triage, Analyze
         (every engine triage selected), Assign, and Reality Check
      3. STOP and write the assessment-core summary to the customer in chat (see
         the REQUIRED chat summary rule below). Do this as its own assistant
         message BEFORE calling any schema-design tool.
      4. the schema-design tools for the same engines triage selected, dispatched
         in parallel — run_schema_design_<engine>_via_a2a. Wait for all of them
         before step 5. Each takes roughly 10-15 minutes, so tell the customer this
         is the long phase and say what it produces.
      5. run_synthesis_via_a2a(job_id, database_name)

  - REQUIRED chat summary (step 3). When run_assessment_core_via_a2a returns you
    MUST reply to the customer with a short chat message BEFORE calling any other
    tool. Do not go straight from the assessment-core tool call into the
    schema-design tool calls in the same turn: emit the summary as its own
    assistant message first, then continue. Keep it to two or three sentences drawn
    from the returned `summary_for_chat` block: the workload signals triage picked
    up, which engines were selected (and briefly why one was skipped if so, e.g.
    the non-matching Aurora variant for the source), how the queries distributed,
    and whether Reality Check consolidated any engines. This is not optional and
    not a full report.

  - Do not pass or reason about an assignment version. Schema design and synthesis
    resolve the correct version themselves (the consolidated set when Reality Check
    trimmed engines). It is never your job to choose it.

  - A schema design that produces no tables is not a failure. Some targets need no
    redesign, and some are not covered by this report. The result says which, in a
    `notes` or `warnings` field. Relay that text as given. Do not describe it as
    an error, do not retry it, and do not characterise it in your own words.

  - State the plan in a sentence or two, then execute the whole sequence. Do not
    pause between phases for approval unless the customer asked for a
    step-by-step review.

  - Report findings in the customer's terms, not the system's: which engines were
    selected and why, how the queries distributed, what the ranking says. Do not
    narrate tool names, agent names or artifact keys unless asked, or unless
    something failed and they are needed to explain it.

  - If a phase fails, say plainly what failed, relay any customer_facing_message,
    and stop rather than continuing into phases that depend on it.

Key points:
  - Engine assignments are produced deterministically. No LLM decides which engine
    a table or query goes to. The LLM steps in the pipeline are the Reality Check
    consolidation pass (which validates the deterministic consolidation and writes
    a CTO summary) and the synthesis executive summary; neither invents a
    per-query recommendation. Say "deterministic" about which engine handles a
    query, not about the whole report.
  - The customer must upload their offline collection JSON before the assessment
    core runs. They attach it through the WebApp's file uploads for this job (it
    lands in the artifact store under "User Uploads/"), and
    run_assessment_core_via_a2a discovers it automatically. You never construct,
    pass, or ask for a storage path. If the run reports no upload found, tell the
    customer to run the collection script for their engine and attach the resulting
    JSON to this job's file uploads, then retry. Do not quote an S3 key, and do not
    attempt to copy or re-upload the file yourself.
  - table_mappings and query_groups are derived from schema-design output. The
    workflow runs schema-design (step 4) before synthesis, so they populate
    normally; they are empty only for an engine whose design produced no tables.
  - Always surface the synthesis report at the end.
"""

PIPELINE_TOOLS = [
    declare_pipeline_plan,
    run_assessment_core_via_a2a,
    run_schema_design_dynamodb_via_a2a,
    run_schema_design_documentdb_via_a2a,
    run_schema_design_elasticache_via_a2a,
    run_schema_design_opensearch_via_a2a,
    run_schema_design_aurora_pg_via_a2a,
    run_schema_design_aurora_mysql_via_a2a,
    run_synthesis_via_a2a,
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
