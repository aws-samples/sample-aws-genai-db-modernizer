"""Schema-design subagent for AWS Transform (modernizer-atx-v2).

One agent (AGENT_TYPE='schema') designs the target schema for whichever engine
the invocation asks for. It writes:

  - <db>/<job>/schema-<target_type>/v<N>/schema_output.json

That is the exact key ``synthesis_data.py`` reads at the same version, and it is
what populates ``table_mappings``, ``query_groups`` and
``recommended_architecture.databases`` in the synthesis report. Those three
fields are empty in every report produced so far, including core-modernizer's
own, because this phase had no ATX subagent and upstream's ``orchestration.yaml``
does not pass ``ASSIGNMENT_VERSION`` to synthesis.

Unlike the analysis phases, which have six distinct core functions, upstream
exposes a single ``run_schema_design`` parameterised by ``target_type``. This
module is that one parameterised agent (ADR-027): rather than six registry names
and six AgentCore runtimes that differ only in a build-time ``AGENT_TYPE``, one
``schema`` agent takes ``target_type`` from the A2A invocation payload. The
orchestrator still invokes it once per engine, concurrently — AgentCore spawns a
fresh stateless instance per invoke, so N engines still run as N parallel
instances, with one registry entry instead of six.

``target_type`` is validated against :data:`VALID_TARGET_TYPES`. A missing or
unknown value fails loudly rather than dispatching an unknown engine, because a
single runtime no longer encodes the target the way the six baked ``AGENT_TYPE``
values did.

This phase is LLM-driven with no deterministic alternative: upstream's
``llm_mode`` only branches on ``"external"``, which writes the prepared model
input and designs nothing. That is the reverse of the analysis phases, where
``llm_mode="none"`` is a complete deterministic path. Model choice is left to
``SCHEMA_AGENT_MODEL_ID`` in the runtime environment rather than taken from the
library default, so it is visible as a deployment decision.
"""

from __future__ import annotations

from src.atx_orchestrator.subagents.base import make_subagent_factory

# The target engines this one agent designs for. target_type arrives in the A2A
# invocation payload (ADR-027); a value outside this set is rejected before any
# work runs. These are the engine identifiers upstream's run_schema_design and
# the artifact keys use (underscores, e.g. aurora_postgresql) — not the hyphenated
# orchestrator tool suffixes (aurora-pg), which tools.py maps before dispatch.
VALID_TARGET_TYPES: frozenset[str] = frozenset(
    {
        "dynamodb",
        "documentdb",
        "elasticache",
        "opensearch",
        "aurora_postgresql",
        "aurora_mysql",
    }
)

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


def _work(params: dict) -> dict:
    from src.atx_orchestrator.core import run_schema_design_core

    # target_type is payload-carried now (one agent, many engines). Fail loudly
    # on a missing/unknown value: a single runtime no longer encodes the target,
    # so a bad payload must surface here rather than silently designing nothing.
    target_type = str(params.get("target_type", "")).strip()
    if target_type not in VALID_TARGET_TYPES:
        raise ValueError(
            "Invocation must include a known 'target_type'. "
            f"Got {target_type!r}; valid: {sorted(VALID_TARGET_TYPES)}"
        )

    # Default assignment_version to 1, matching the assignment agent's output
    # path and the version synthesis reads. Never default to 0: at 0 upstream
    # passes every query to every engine rather than the ones assigned to it, and
    # writes to a key synthesis does not read.
    return run_schema_design_core(
        job_id=params["job_id"],
        database_name=params["database_name"],
        target_type=target_type,
        assignment_version=int(params.get("assignment_version", 1)),
    )


agent_factory = make_subagent_factory(SYSTEM_PROMPT, _work)
