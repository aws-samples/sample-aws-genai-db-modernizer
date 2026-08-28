"""Collector subagent for AWS Transform (one agent per image).

Maps one-to-one to the existing AGENT_TYPE='collector'. Ingests the pre-collected
offline database collection from the artifact store and writes the collector
output contract. Does NOT run triage — that's a separate subagent.
"""

from __future__ import annotations

from src.atx_orchestrator.subagents.base import make_subagent_factory

SYSTEM_PROMPT = """\
You are the Collector subagent for database modernization assessments.

Your single task: given a job_id and database_name, read the pre-collected
offline database collection from the artifact store and build the collector
output contract. You do not analyze or triage — you only ingest and normalize
the collected schema and query data.
"""


def _work(params: dict) -> dict:
    from src.atx_orchestrator.core import run_collect_core

    return run_collect_core(
        job_id=params["job_id"],
        database_name=params["database_name"],
        input_key=params.get("input_key", ""),
    )


agent_factory = make_subagent_factory(SYSTEM_PROMPT, _work)
