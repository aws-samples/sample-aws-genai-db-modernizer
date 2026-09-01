"""Deterministic-core subagent for AWS Transform (one agent per image).

Maps to AGENT_TYPE='deterministic-core'. Runs the entire deterministic
front-half of the pipeline in one process, in DAG order:

    Collect -> Triage -> Analyze (every triage-selected engine) -> Assign

via ``core.run_deterministic_core``, which composes the unchanged per-phase
cores. Each phase writes the same artifacts it always has, so Schema Design,
Synthesis, and every downstream reader are unaffected (see ADR-025). This single
agent replaced four separate runtimes (collector, referee-triage, analysis,
assignment-resolver); consolidation is safe because analysis runs in-process,
satisfying the analysis -> assignment dependency sequentially.

Progress: the orchestrator declares the plan (collector, triage, analysis with
nested per-engine sub-steps, assignment) in a different process, so this agent
resolves those step ids from the server (``register_steps_from_server``) and
ticks each one IN_PROGRESS -> SUCCEEDED/FAILED as it moves through the phases,
attaching a short description (e.g. triage's detected signals) so the WebApp
panel shows live per-phase detail. All progress calls are best-effort: outside
the ATX runtime they are no-ops and the pipeline is unaffected.
"""

from __future__ import annotations

from src.atx_orchestrator.subagents.base import make_subagent_factory

SYSTEM_PROMPT = """\
You are the Deterministic Core subagent for database modernization assessments.

Your single task: given a job_id and database_name (and an optional input_key
locating the uploaded offline collection), run the deterministic front-half of
the assessment end to end: ingest the collection, triage candidate engines,
analyze every selected engine, and assign each query to its best-fit engine.

This work is fully deterministic — no LLM inference is involved. Each phase
reads and writes the shared artifact store; downstream phases (schema design,
synthesis) consume those artifacts unchanged. Return a merged summary of all
phases.
"""


def _work(params: dict) -> dict:
    from src.atx_orchestrator.core import run_deterministic_core
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

    # Top-level phase steps (collector, triage, analysis, assignment).
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

    return run_deterministic_core(
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
