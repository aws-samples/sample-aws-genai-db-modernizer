"""Schema-design subagents for AWS Transform (modernizer-atx-v2).

Maps to AGENT_TYPE='schema-<engine>', one per target engine. Each writes:

  - <db>/<job>/schema-<target_type>/v<N>/schema_output.json

That is the exact key ``synthesis_data.py`` reads at the same version, and it is
what populates ``table_mappings``, ``query_groups`` and
``recommended_architecture.databases`` in the synthesis report. Those three
fields are empty in every report produced so far, including core-modernizer's
own, because this phase had no ATX subagent and upstream's ``orchestration.yaml``
does not pass ``ASSIGNMENT_VERSION`` to synthesis.

Unlike the analysis phases, which have six distinct core functions and therefore
six modules, upstream exposes a single ``run_schema_design`` parameterised by
``target_type``. So this module provides one factory builder rather than six
near-identical files, and the entrypoint shim resolves the target from
AGENT_TYPE.

Six agents are registered even though triage typically selects fewer, so the
fleet is uniform and a workload with a different engine mix needs no new
deployment.

This phase is LLM-driven with no deterministic alternative: upstream's
``llm_mode`` only branches on ``"external"``, which writes the prepared model
input and designs nothing. That is the reverse of the analysis phases, where
``llm_mode="none"`` is a complete deterministic path. Model choice is left to
``SCHEMA_AGENT_MODEL_ID`` in the runtime environment rather than taken from the
library default, so it is visible as a deployment decision. Measured against the
reference workload, all five selected engines completed in roughly fifteen
minutes sequentially on Opus; registering per-engine lets them run in parallel
instead.
"""

from __future__ import annotations

from src.atx_orchestrator.subagents.base import make_subagent_factory

# AGENT_TYPE suffix -> the target_type upstream expects. The two differ: agent
# types use hyphens to match the rest of the fleet's naming, while artifact keys
# and upstream's dispatch use the engine's own identifier.
SCHEMA_TARGETS: dict[str, str] = {
    "schema-dynamodb": "dynamodb",
    "schema-documentdb": "documentdb",
    "schema-elasticache": "elasticache",
    "schema-opensearch": "opensearch",
    "schema-aurora-pg": "aurora_postgresql",
    "schema-aurora-mysql": "aurora_mysql",
}

SYSTEM_PROMPT = """\
You are a schema-design subagent for database modernization assessments.

Your single task: given a job_id, database_name and assignment_version, design
the target schema for one engine. Read the collector output and the assignment,
then produce table definitions, access patterns, and any patterns the target
engine does not support.

Return the summary the tool gives you: status, the counts of table_definitions,
access_patterns and unsupported_patterns, and the artifact key. The full design
is written to S3 separately.

Report the numbers the tool returns verbatim. Do not estimate or infer counts
that are not in the tool output, and do not describe design decisions the output
does not contain. If the tool reports that no design was produced, relay its
reason as given rather than characterising it.
"""


def make_schema_agent_factory(target_type: str):
    """Build the agent_factory for one target engine."""

    def _work(params: dict) -> dict:
        from src.atx_orchestrator.core import run_schema_design_core

        # Default to 1, matching the assignment agent's output path and the
        # version synthesis reads. Never default to 0: at 0 upstream passes every
        # query to every engine rather than the ones assigned to it, and writes to
        # a key synthesis does not read.
        return run_schema_design_core(
            job_id=params["job_id"],
            database_name=params["database_name"],
            target_type=target_type,
            assignment_version=int(params.get("assignment_version", 1)),
        )

    return make_subagent_factory(SYSTEM_PROMPT, _work)
