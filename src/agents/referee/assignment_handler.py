"""
Assignment Resolver agent handler — reads pipeline artifacts, produces assignment.

Reads triage, analysis outputs, and collector output via ArtifactStore.
Calls AssignmentResolver.resolve() to produce an Assignment, then
AssignmentValidator.validate() on the result. Writes versioned artifacts:
  - assignment/v{N}/assignment.json
  - assignment/v{N}/validation.json

Requirements: 2.1, 2.5, 4.1
"""

from __future__ import annotations

import time

from src.agents.referee.assignment_resolver import AssignmentResolver
from src.agents.referee.assignment_validator import AssignmentValidator
from src.storage.artifact_store import ArtifactStore


def run_assignment_resolver(job_id: str, database_name: str, store: ArtifactStore) -> None:
    """Run the assignment resolver agent.

    1. Read triage output to discover selected engines
    2. Read each engine's analysis output
    3. Read collector output
    4. Determine the next version number
    5. Call AssignmentResolver.resolve()
    6. Call AssignmentValidator.validate()
    7. Write assignment and validation artifacts
    """
    start_time = time.time()
    print(f"[assignment] Starting assignment resolution for {database_name}")

    # --- Read triage output ---
    triage_key = f"{database_name}/{job_id}/referee-triage/triage.json"
    triage = store.read_json(triage_key)
    selected_agents = [a["agent_type"] for a in triage.get("selected_agents", [])]
    print(f"[assignment] Selected engines from triage: {selected_agents}")

    # --- Read collector output ---
    collector_key = f"{database_name}/{job_id}/collector/output.json"
    collector_output = store.read_json(collector_key)
    queries = collector_output.get("queries", {}).get("query_patterns", [])
    print(f"[assignment] Loaded collector: {len(queries)} queries")

    # --- Read analysis outputs for each selected engine ---
    analysis_outputs: dict[str, dict] = {}
    for engine in selected_agents:
        analysis_key = f"{database_name}/{job_id}/analysis-{engine}/analysis.json"
        if store.exists(analysis_key):
            analysis_outputs[engine] = store.read_json(analysis_key)
            print(f"[assignment] Loaded analysis for {engine}")
        else:
            print(f"[assignment] WARNING: No analysis found for {engine}")

    # --- Determine version number ---
    version = _next_version(store, database_name, job_id)
    print(f"[assignment] Assignment version: {version}")

    # --- Resolve assignment ---
    resolver = AssignmentResolver()
    assignment = resolver.resolve(triage, analysis_outputs, collector_output)
    # Override version from resolver (which defaults to 1)
    assignment = assignment.model_copy(update={"version": version})
    print(
        f"[assignment] Resolved: {len(assignment.query_assignments)} queries, "
        f"{len(assignment.table_assignments)} tables, "
        f"{len(assignment.co_dependency_groups)} co-dep groups"
    )

    # --- Validate assignment ---
    validator = AssignmentValidator()
    validation = validator.validate(assignment, collector_output, analysis_outputs)
    print(
        f"[assignment] Validation: valid={validation.valid}, "
        f"{len(validation.warnings)} warnings, {len(validation.errors)} errors"
    )

    # Store validation warnings on the assignment artifact
    assignment = assignment.model_copy(update={"validation_warnings": validation.warnings})

    # --- Write artifacts ---
    assignment_key = f"{database_name}/{job_id}/assignment/v{version}/assignment.json"
    store.write_json(assignment_key, assignment.model_dump(mode="json"))

    # Materialize query journey files (assignment section) — ADR-019
    from src.agents.query_journey_materializer import materialize_assignment

    materialize_assignment(assignment.model_dump(mode="json"), database_name, job_id, store)
    print(f"[assignment] Assignment written to {assignment_key}")

    validation_key = f"{database_name}/{job_id}/assignment/v{version}/validation.json"
    store.write_json(validation_key, validation.model_dump(mode="json"))
    print(f"[assignment] Validation written to {validation_key}")

    elapsed = time.time() - start_time
    print(f"[assignment] ✅ Complete in {elapsed:.1f}s — version {version}")


def _next_version(store: ArtifactStore, database_name: str, job_id: str) -> int:
    """Determine the next assignment version by scanning existing versions."""
    prefix = f"{database_name}/{job_id}/assignment/"
    existing = store.list_prefix(prefix)
    max_version = 0
    for path in existing:
        # Paths look like: db/job/assignment/v3/assignment.json
        parts = path.split("/")
        for part in parts:
            if part.startswith("v") and part[1:].isdigit():
                max_version = max(max_version, int(part[1:]))
    return max_version + 1
