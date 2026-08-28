"""Assignment subagent for AWS Transform (one agent per image).

Maps to the existing AGENT_TYPE='assignment'. Reads the collector, triage,
and all analysis outputs from the artifact store, invokes the assignment
resolver, and writes 1 artifact to S3:

  - <db>/<job>/assignment/v1/assignment.json (query -> engine mapping)

Deterministic subagent — NO LLM invocation. The assignment is rule-based:
scores every query against each candidate engine's analysis and picks
the highest-confidence match per query.
"""

from __future__ import annotations

from src.atx_orchestrator.subagent_base import make_subagent_factory

SYSTEM_PROMPT = """\
You are the Assignment subagent for database modernization assessments.

Your single task: given a job_id and database_name, read the collector, triage,
and all analysis outputs already in the artifact store and produce a per-query
assignment mapping — which target engine each query should go to.

The assignment is deterministic and rule-based. It uses each engine's analysis
confidence scores + workload signals from triage to score every query, then
picks the highest-scoring engine per query with tie-breaking rules.

Return a summary including total_queries and queries_per_engine breakdown.
The full assignment.json artifact is written to S3.
"""


def _work(params: dict) -> dict:
    from src.atx_orchestrator.core import run_assignment_core

    return run_assignment_core(
        job_id=params["job_id"],
        database_name=params["database_name"],
    )


agent_factory = make_subagent_factory(SYSTEM_PROMPT, _work)
