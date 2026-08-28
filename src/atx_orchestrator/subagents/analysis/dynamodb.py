"""DynamoDB Analysis subagent for AWS Transform (one agent per image).

Maps to the existing AGENT_TYPE='analysis-dynamodb'. Reads the collector
output from the artifact store, runs the full DynamoDB analysis pipeline
(deterministic pattern matching + scoring + cost estimation + optional
LLM advisor via Strands SDK), and writes three artifacts to S3:

  - <db>/<job>/analysis-dynamodb/analysis.json         (recommendations)
  - <db>/<job>/analysis-dynamodb/decision-trace.json   (per-query trace)
  - <db>/<job>/analysis-dynamodb/er-diagram.mmd        (Mermaid ER diagram)

This is the FIRST LLM-invoking subagent in the PoC. The container's
LLM_MODE env var controls Bedrock behavior:
  - "bedrock" (default): calls LlmAdvisor via Strands SDK when
    ENABLE_LLM_ADVISOR=true
  - "none":              deterministic only, useful for CI/testing
  - "external":          deterministic only, output marked awaiting

Model selection is via MODEL_ID env var — set at deploy time (A10) to the
intended production model (Opus 4.8 per the model matrix in
ATX_POC_STATE.md §10).
"""

from __future__ import annotations

from src.atx_orchestrator.subagents.base import make_subagent_factory

SYSTEM_PROMPT = """\
You are the DynamoDB Analysis subagent for database modernization assessments.

Your single task: given a job_id and database_name, read the collector output
already written to the artifact store and produce a DynamoDB-specific analysis.
Score every table against DynamoDB access patterns (key-value lookups, range
queries, high-frequency writes, etc.), detect anti-patterns (joins, complex
aggregations), estimate monthly cost, and produce migration recommendations.

Return a summary including tables_analyzed, confidence-band counts
(highly_suitable/suitable/marginal/not_suitable), and estimated_monthly_cost_usd.
The full recommendations, per-query decision trace, and Mermaid ER diagram are
written to S3 as separate artifacts.
"""


def _work(params: dict) -> dict:
    from src.atx_orchestrator.core import run_analysis_core

    return run_analysis_core(
        "dynamodb",
        job_id=params["job_id"],
        database_name=params["database_name"],
    )


agent_factory = make_subagent_factory(SYSTEM_PROMPT, _work)
