"""Aurora PostgreSQL Analysis subagent for AWS Transform (one agent per image).

Maps to the existing AGENT_TYPE='analysis-aurora-pg'. Deterministic — no LLM.
Reads the collector output, runs analyze_for_aurora_pg with llm_mode="none",
and writes 2 artifacts to S3:

  - <db>/<job>/analysis-aurora_postgresql/analysis.json         (recommendations)
  - <db>/<job>/analysis-aurora_postgresql/decision-trace.json   (per-query trace)

Aurora agent's "bedrock" LLM mode is documented as "not yet implemented",
so this subagent always runs with LLM_MODE=none regardless of container config.
"""

from __future__ import annotations

from src.atx_orchestrator.subagent_base import make_subagent_factory

SYSTEM_PROMPT = """\
You are the Aurora PostgreSQL Analysis subagent for database modernization assessments.

Your single task: given a job_id and database_name, read the collector output
already written to the artifact store and produce an Aurora PostgreSQL-specific
analysis. Aurora PG is the relational baseline — score every table's suitability
for staying on relational infrastructure, estimate cost, and produce
recommendations.

Return a summary including tables_analyzed, confidence-band counts, and
estimated_monthly_cost_usd. The full recommendations and per-query decision
trace are written to S3 as separate artifacts.
"""


def _work(params: dict) -> dict:
    from src.atx_orchestrator.core import run_analysis_core

    return run_analysis_core(
        "aurora_pg",
        job_id=params["job_id"],
        database_name=params["database_name"],
    )


agent_factory = make_subagent_factory(SYSTEM_PROMPT, _work)
