"""OpenSearch Analysis subagent for AWS Transform (one agent per image).

Maps to the existing AGENT_TYPE='analysis-opensearch'. Reads the collector
output from the artifact store, runs the deterministic OpenSearch analysis
pipeline (full-text, wildcard, time-series, log analytics pattern matching
+ index sizing + cost estimation), and writes 2 artifacts to S3:

  - <db>/<job>/analysis-opensearch/analysis.json         (recommendations)
  - <db>/<job>/analysis-opensearch/decision-trace.json   (per-query trace)

Note: no ER diagram artifact — OpenSearch indexes are independent, no
relational structure to visualize.

Deterministic subagent — NO LLM invocation.
"""

from __future__ import annotations

from src.atx_orchestrator.subagents.base import make_subagent_factory

SYSTEM_PROMPT = """\
You are the OpenSearch Analysis subagent for database modernization assessments.

Your single task: given a job_id and database_name, read the collector output
already written to the artifact store and produce an OpenSearch-specific analysis.
Score every table against OpenSearch access patterns (full-text search, wildcard,
regex, fuzzy, time-series, log analytics), estimate index sizing and monthly cost,
and produce migration recommendations.

Return a summary including tables_analyzed, confidence-band counts, and
estimated_monthly_cost_usd. The full recommendations and per-query decision
trace are written to S3 as separate artifacts.
"""


def _work(params: dict) -> dict:
    from src.atx_orchestrator.core import run_analysis_core

    return run_analysis_core(
        "opensearch",
        job_id=params["job_id"],
        database_name=params["database_name"],
    )


agent_factory = make_subagent_factory(SYSTEM_PROMPT, _work)
