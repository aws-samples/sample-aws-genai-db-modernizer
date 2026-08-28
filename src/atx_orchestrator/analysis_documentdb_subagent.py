"""DocumentDB Analysis subagent for AWS Transform (one agent per image).

Maps to the existing AGENT_TYPE='analysis-documentdb'. Reads the collector
output from the artifact store, runs the full DocumentDB analysis pipeline
(deterministic pattern matching + scoring + cost estimation + optional
LLM advisor via Strands SDK for embedding-vs-reference trade-offs),
and writes three artifacts to S3:

  - <db>/<job>/analysis-documentdb/analysis.json         (recommendations)
  - <db>/<job>/analysis-documentdb/decision-trace.json   (per-query trace)
  - <db>/<job>/analysis-documentdb/er-diagram.mmd        (Mermaid ER diagram)

Second LLM-invoking subagent in the PoC (after Analysis-DynamoDB). The
container's LLM_MODE env var controls Bedrock behavior:
  - "bedrock" (default): calls LlmDocumentDBAdvisor via Strands SDK when
    ENABLE_LLM_ADVISOR=true and embedding candidates exist
  - "none":              deterministic only, useful for CI/testing
  - "external":          deterministic only, output marked awaiting

Model selection is via MODEL_ID env var — set at deploy time to the intended
production model (Opus 4.8 per the model matrix in ATX_POC_STATE.md §10).
"""

from __future__ import annotations

from src.atx_orchestrator.subagent_base import make_subagent_factory

SYSTEM_PROMPT = """\
You are the DocumentDB Analysis subagent for database modernization assessments.

Your single task: given a job_id and database_name, read the collector output
already written to the artifact store and produce a DocumentDB-specific analysis.
Score every table against DocumentDB access patterns (nested-document candidates,
embedding vs. reference relationships, aggregation pipelines), detect
anti-patterns (heavy cross-collection joins, unbounded array growth), estimate
monthly cost, and produce migration recommendations.

Return a summary including tables_analyzed, confidence-band counts
(highly_suitable/suitable/marginal/not_suitable), and estimated_monthly_cost_usd.
The full recommendations, per-query decision trace, and Mermaid ER diagram are
written to S3 as separate artifacts.
"""


def _work(params: dict) -> dict:
    from src.atx_orchestrator.core import run_analysis_documentdb_core

    return run_analysis_documentdb_core(
        job_id=params["job_id"],
        database_name=params["database_name"],
    )


agent_factory = make_subagent_factory(SYSTEM_PROMPT, _work)
