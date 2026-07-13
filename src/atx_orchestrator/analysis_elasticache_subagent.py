"""ElastiCache Analysis subagent for AWS Transform (one agent per image).

Maps to the existing AGENT_TYPE='analysis-elasticache'. Reads the collector
output from the artifact store, runs the deterministic ElastiCache/Redis
analysis pipeline (caching, session store, leaderboards, time-series pattern
matching + cost estimation), and writes 2-3 artifacts to S3:

  - <db>/<job>/analysis-elasticache/analysis.json         (recommendations)
  - <db>/<job>/analysis-elasticache/decision-trace.json   (per-query trace)
  - <db>/<job>/analysis-elasticache/er-diagram.mmd        (optional)

Deterministic subagent — NO LLM invocation. Runs at LLM_MODE=none regardless
of container config (analyze_for_elasticache doesn't accept an llm_mode
parameter).
"""

from __future__ import annotations

from src.atx_orchestrator.subagent_base import make_subagent_factory

SYSTEM_PROMPT = """\
You are the ElastiCache/Redis Analysis subagent for database modernization assessments.

Your single task: given a job_id and database_name, read the collector output
already written to the artifact store and produce an ElastiCache-specific analysis.
Score every table against Redis access patterns (caching, session store,
leaderboards, time-series aggregation, geospatial), estimate monthly cost, and
produce migration recommendations.

Return a summary including tables_analyzed, confidence-band counts, and
estimated_monthly_cost_usd. The full recommendations and per-query decision
trace are written to S3 as separate artifacts.
"""


def _work(params: dict) -> dict:
    from src.atx_orchestrator.core import run_analysis_elasticache_core

    return run_analysis_elasticache_core(
        job_id=params["job_id"],
        database_name=params["database_name"],
    )


agent_factory = make_subagent_factory(SYSTEM_PROMPT, _work)
