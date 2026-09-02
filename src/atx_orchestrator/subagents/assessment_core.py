"""Assessment-core subagent for AWS Transform (one agent per image).

Maps to AGENT_TYPE='assessment-core'. Runs the whole assessment front-half of
the pipeline in one process, in DAG order:

    Collect -> Triage -> Analyze (every triage-selected engine) -> Assign
    -> Reality Check (CTO-level engine consolidation)

via ``core.run_assessment_core``, which composes the unchanged per-phase cores.
Each phase writes the same artifacts it always has, so Schema Design, Synthesis,
and every downstream reader are unaffected (see ADR-025, ADR-026). This single
agent replaced four separate runtimes (collector, referee-triage, analysis,
assignment-resolver) and now also runs Reality Check; consolidation is safe
because every phase runs in-process, satisfying the ordering dependencies
sequentially.

Collect, Triage, Analyze, and Assign are deterministic. Reality Check runs a
Bedrock LLM pass (validates the consolidations and writes the CTO executive
summary), which is why the agent is named for what it produces rather than for
being purely deterministic. When Reality Check consolidates, it writes an
assignment v2 that downstream phases resolve automatically.

Progress: the orchestrator declares the plan (collector, triage, analysis with
nested per-engine sub-steps, assignment, reality_check) in a different process,
so this agent resolves those step ids from the server
(``register_steps_from_server``) and ticks each one IN_PROGRESS ->
SUCCEEDED/FAILED as it moves through the phases, attaching a short description
(e.g. triage's detected signals) so the WebApp panel shows live per-phase detail.
All progress calls are best-effort: outside the ATX runtime they are no-ops and
the pipeline is unaffected.
"""

from __future__ import annotations

from src.atx_orchestrator.subagents.base import make_subagent_factory

SYSTEM_PROMPT = """\
You are the Assessment Core subagent for database modernization assessments.

Your single task: given a job_id and database_name (and an optional input_key
locating the uploaded offline collection), run the assessment front-half end to
end: ingest the collection, triage candidate engines, analyze every selected
engine, assign each query to its best-fit engine, and run the CTO-level reality
check that consolidates redundant engines.

Collect, triage, analysis, and assignment are deterministic; the reality check
adds one LLM pass to validate consolidations and write an executive summary.
Each phase reads and writes the shared artifact store; downstream phases (schema
design, synthesis) consume those artifacts unchanged. Return a merged summary of
all phases.
"""


def _work(params: dict) -> dict:
    from src.atx_orchestrator.core import run_assessment_core
    from src.atx_orchestrator.runtime.job_plan import (
        mark_step_failed,
        mark_step_running,
        mark_step_skipped,
        mark_step_succeeded,
        register_steps_from_server,
    )

    # Resolve the plan step ids the orchestrator declared in another process, so
    # the mark_step_* calls below land on the right steps and sub-steps.
    # Best-effort: outside the ATX runtime this is a no-op and progress is skipped.
    register_steps_from_server()

    # Top-level phase steps (collector, triage, analysis, assignment, reality_check).
    def _phase_start(phase: str) -> None:
        mark_step_running(phase)

    def _phase_done(phase: str, _summary: dict, detail: str) -> None:
        mark_step_succeeded(phase, detail)

    def _phase_error(phase: str, reason: str) -> None:
        mark_step_failed(phase, reason)

    # Nested per-engine analysis sub-steps (analysis_<target_database>).
    def _engine_start(_engine: str, phase: str) -> None:
        mark_step_running(phase)

    def _engine_done(_engine: str, phase: str, _summary: dict) -> None:
        mark_step_succeeded(phase)

    def _engine_error(_engine: str, phase: str, reason: str) -> None:
        mark_step_failed(phase, reason)

    def _engine_skipped(_engine: str, phase: str, reason: str) -> None:
        # Engine triage chose not to analyze (e.g. non-matching Aurora variant).
        mark_step_skipped(phase, reason)

    return run_assessment_core(
        job_id=params["job_id"],
        database_name=params["database_name"],
        input_key=params.get("input_key", ""),
        on_phase_start=_phase_start,
        on_phase_done=_phase_done,
        on_phase_error=_phase_error,
        on_engine_start=_engine_start,
        on_engine_done=_engine_done,
        on_engine_error=_engine_error,
        on_engine_skipped=_engine_skipped,
    )


agent_factory = make_subagent_factory(SYSTEM_PROMPT, _work)
