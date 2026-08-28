"""Referee-Synthesis subagent for AWS Transform (modernizer-atx-v2).

Maps to AGENT_TYPE='referee-synthesis'. Reads every artifact the pipeline has
produced — collector output, triage, per-engine analysis, the assignment, and
per-engine schema designs — and writes the consolidated report:

  - <db>/<job>/synthesis/v<N>/report.json      (when assignment_version > 0)
  - <db>/<job>/referee-synthesis/report.json   (when assignment_version == 0)

This phase does not exist on the deployed v1 path, which stops at assignment.
That gap is why a demo-quality report had to be produced from a core-modernizer
run rather than from AWS Transform.

Deterministic-first, like the analysis phases: every builder (ranking, table
mappings, query groups, TCO, risk assessment, architecture recommendation) runs
with no LLM, then a single Bedrock call writes the executive summary. Parity with
core-modernizer means keeping that call — its ``entrypoint.py`` invokes
``run_synthesis(...)`` without an ``llm_mode`` argument and so takes the
``"bedrock"`` default. This is the opposite of the analysis phases, where
core-modernizer passes ``llm_mode="none"``. Matching upstream means respecting
the difference rather than applying one setting everywhere.

``assignment_version`` is required in the incoming params and must be the
version the assignment agent actually produced. It is not optional and it should
not be guessed: the handler defaults across phases disagree with one another
(synthesis defaults to 0, reality-check to 1), and at version 0 synthesis never
reads the assignment at all. ``run_synthesis_core`` raises if the resulting
report ranks engines but recommends no architecture, which is the signature of a
wrong version.
"""

from __future__ import annotations

from src.atx_orchestrator.subagent_base import make_subagent_factory

SYSTEM_PROMPT = """\
You are the Referee-Synthesis subagent for database modernization assessments.

Your single task: given a job_id, database_name and assignment_version, read
every artifact the pipeline has already written and produce the consolidated
synthesis report. Rank the candidate engines by confidence, map source tables to
target engines, group queries by access pattern, build the TCO comparison and
risk assessment, and recommend a target architecture.

Return a summary including engines_ranked, top_engine, architecture_type, the
recommended databases, table_mappings and query_groups counts, and
overall_risk_level. The full report is written to S3 as a separate artifact.

Report the numbers the tool returns verbatim. Do not estimate, round, or infer
values that are not in the tool output, and do not describe a recommendation the
report does not contain.
"""


def _work(params: dict) -> dict:
    from src.atx_orchestrator.core import run_synthesis_core

    # Default to 1, matching run_synthesis_via_a2a, because run_assignment_core
    # writes assignment/v1/. Never default to 0: at version 0 synthesis skips the
    # assignment entirely and emits a report with an empty architecture.
    # Defaulting rather than requiring the key means a caller that omits it gets
    # the correct behaviour instead of a KeyError mid-pipeline.
    return run_synthesis_core(
        job_id=params["job_id"],
        database_name=params["database_name"],
        assignment_version=int(params.get("assignment_version", 1)),
    )


agent_factory = make_subagent_factory(SYSTEM_PROMPT, _work)
