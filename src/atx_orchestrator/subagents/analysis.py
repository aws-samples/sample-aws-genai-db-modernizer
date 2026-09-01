"""Consolidated Analysis subagent for AWS Transform (one agent, all engines).

Maps to AGENT_TYPE='analysis'. Replaces the six per-engine analysis subagents
(see ADR-024): analysis is deterministic and millisecond-scale, so running all
triage-selected engines in this one process is cheaper than six AgentCore
runtimes and, end to end, faster (one A2A round-trip instead of six).

It reads the collector + triage output already in the artifact store and, for
each engine triage selected, runs the unchanged per-engine analysis via
``core.run_all_analyses`` -> ``core.run_analysis_core``. Each engine still writes
its own artifacts, so Assign / Reality Check / Schema Design are unaffected:

  - <db>/<job>/analysis-<engine>/analysis.json         (recommendations)
  - <db>/<job>/analysis-<engine>/decision-trace.json   (per-query trace)
  - <db>/<job>/analysis-<engine>/er-diagram.mmd        (DynamoDB/DocumentDB)

Progress: the orchestrator declares the plan with an "analysis" parent step whose
sub-steps are the per-engine phases (analysis_<target_database>). This agent runs
in a different process from the orchestrator, so it resolves those sub-step ids
from the server (``register_steps_from_server``) and ticks each one
IN_PROGRESS -> SUCCEEDED/FAILED as it loops — giving the nested per-engine
checklist inside the Analysis box in the WebApp. All progress calls are
best-effort no-ops outside the ATX runtime.
"""

from __future__ import annotations

from src.atx_orchestrator.subagents.base import make_subagent_factory

SYSTEM_PROMPT = """\
You are the Analysis subagent for database modernization assessments.

Your single task: given a job_id and database_name, read the collector and triage
output already written to the artifact store and run the analysis for every
target engine triage selected. For each engine, score every table against that
engine's access patterns, detect anti-patterns, estimate monthly cost, and
produce migration recommendations.

The analysis is deterministic (no LLM). The full per-engine recommendations,
decision traces, and ER diagrams are written to the artifact store; return a
summary of which engines were analyzed.
"""


def _work_all(params: dict) -> dict:
    from src.atx_orchestrator.core import run_all_analyses
    from src.atx_orchestrator.runtime.job_plan import (
        mark_step_failed,
        mark_step_running,
        mark_step_skipped,
        mark_step_succeeded,
        register_steps_from_server,
    )

    # Resolve the per-engine sub-step ids the orchestrator declared in another
    # process, so the mark_step_* calls below land on the right sub-steps.
    # Best-effort: outside the ATX runtime this is a no-op and progress is skipped.
    register_steps_from_server()

    def _start(_engine: str, phase: str) -> None:
        mark_step_running(phase)

    def _done(_engine: str, phase: str, _summary: dict) -> None:
        mark_step_succeeded(phase)

    def _error(_engine: str, phase: str, reason: str) -> None:
        mark_step_failed(phase, reason)

    def _skipped(_engine: str, phase: str, reason: str) -> None:
        # Engine triage chose not to analyze (e.g. non-matching Aurora variant).
        # Mark the sub-step STOPPED with the reason so the WebApp can explain why.
        mark_step_skipped(phase, reason)

    return run_all_analyses(
        job_id=params["job_id"],
        database_name=params["database_name"],
        on_engine_start=_start,
        on_engine_done=_done,
        on_engine_error=_error,
        on_engine_skipped=_skipped,
    )


agent_factory = make_subagent_factory(SYSTEM_PROMPT, _work_all)
