"""Assignment-stage adapter: stage a case into a store, run the agent, extract result.

The assignment agent is rule-based (no Bedrock), so this is deterministic.
"""

from __future__ import annotations

from pathlib import Path

from benchmarks.runner.loader import Case
from src.agents.referee.assignment_handler import run_assignment_resolver
from src.storage.local_store import LocalArtifactStore

_DB = "benchmark_db"
_JOB = "case"


def run_assignment_case(case: Case, work_dir: Path) -> dict[str, str]:
    """Stage the case's gold inputs, run assignment, return {query_id: assigned_engine}."""
    store = LocalArtifactStore(str(work_dir))

    store.write_json(f"{_DB}/{_JOB}/referee-triage/triage.json", case.triage)
    store.write_json(f"{_DB}/{_JOB}/collector/output.json", case.collection)
    for engine, analysis in case.analysis.items():
        store.write_json(f"{_DB}/{_JOB}/analysis-{engine}/analysis.json", analysis)

    run_assignment_resolver(_JOB, _DB, store)

    assignment = store.read_json(f"{_DB}/{_JOB}/assignment/v1/assignment.json")
    return {qa["query_id"]: qa["assigned_engine"] for qa in assignment.get("query_assignments", [])}
