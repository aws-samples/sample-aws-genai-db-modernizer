"""Triage subagent for AWS Transform (one agent per image).

Maps one-to-one to the existing AGENT_TYPE='referee-triage'. Reads the collector
output artifact from the store, runs deterministic signal detection + candidate
engine selection, and writes the triage.json output. Does NOT invoke any LLM.

Before Phase A, triage ran in-process inside the orchestrator container (see
claude.md §91). This subagent moves it into its own container reachable via A2A,
matching the pattern established by the collector subagent.
"""

from __future__ import annotations

from src.atx_orchestrator.subagents.base import make_subagent_factory

SYSTEM_PROMPT = """\
You are the Triage subagent for database modernization assessments.

Your single task: given a job_id and database_name, read the collector output
already written to the artifact store and produce the triage decision. Detect
workload signals across the collected queries and schema, then select the
candidate AWS database engines that should each analyze the workload in more
depth.

This work is deterministic — no LLM inference is required. Return a summary
including selected_engines and signal_count. Downstream agents (analysis
subagents per engine) will use the full triage.json to drive their scoring.
"""


def _work(params: dict) -> dict:
    from src.atx_orchestrator.core import run_triage_core

    return run_triage_core(
        job_id=params["job_id"],
        database_name=params["database_name"],
    )


agent_factory = make_subagent_factory(SYSTEM_PROMPT, _work)
