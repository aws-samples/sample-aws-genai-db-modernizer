"""Assignment routes — read, override, and scope-narrow query assignments.

Requirements: 14.1, 14.2, 3.2
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from src.agents.referee.assignment_validator import AssignmentValidator
from src.contracts.assignment_models import (
    Assignment,
    AssignmentStatus,
    QueryAssignment,
    ValidationResult,
)
from src.storage.artifact_store import ArtifactStore

router = APIRouter(prefix="/api/v1/assessments", tags=["assignments"])

# Services injected by main.py at startup
artifact_store: ArtifactStore | None = None


def _require_store() -> ArtifactStore:
    if not artifact_store:
        raise HTTPException(status_code=503, detail="ArtifactStore not configured")
    return artifact_store


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class QueryOverride(BaseModel):
    """A single query-level override submitted by the customer."""

    query_id: str
    assigned_engine: str | None = None
    in_scope: bool | None = None


class ScopeNarrowing(BaseModel):
    """Table-level scope narrowing: exclude entire tables from the current iteration."""

    exclude_tables: list[str] = Field(default_factory=list)
    reason: str | None = None


class AssignmentOverrideRequest(BaseModel):
    """PUT body for assignment overrides / scope narrowing."""

    overrides: list[QueryOverride] = Field(
        default_factory=list,
        description="Per-query overrides (engine change or scope change)",
    )
    scope: ScopeNarrowing | None = Field(
        None,
        description="Table-level scope narrowing (exclude entire tables)",
    )


class AssignmentResponse(BaseModel):
    """Response wrapper for assignment data with validation info."""

    assignment: Assignment
    validation: ValidationResult | None = None
    skipped_engines: list[str] = Field(
        default_factory=list,
        description="Engines with zero in-scope queries (schema design will be SKIPPED)",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _latest_assignment_version(store: ArtifactStore, db: str, job_id: str) -> int:
    """Find the latest assignment version by listing versioned prefixes."""
    prefix = f"{db}/{job_id}/assignment/"
    keys = store.list_prefix(prefix)
    versions: list[int] = []
    for key in keys:
        # keys look like: db/job/assignment/v3/assignment.json
        parts = key.replace(prefix, "").split("/")
        if parts and parts[0].startswith("v"):
            try:
                versions.append(int(parts[0][1:]))
            except ValueError:
                continue
    return max(versions) if versions else 0


def _read_assignment(store: ArtifactStore, db: str, job_id: str, version: int) -> Assignment:
    """Read a specific assignment version."""
    path = f"{db}/{job_id}/assignment/v{version}/assignment.json"
    data = store.read_json(path)
    return Assignment.model_validate(data)


def _read_collector_output(store: ArtifactStore, db: str, job_id: str) -> dict:
    return store.read_json(f"{db}/{job_id}/collector/output.json")


def _read_analysis_outputs(store: ArtifactStore, db: str, job_id: str) -> dict[str, dict]:
    """Read all analysis outputs by listing the analysis-* prefixes."""
    prefix = f"{db}/{job_id}/"
    keys = store.list_prefix(prefix)
    engines: set[str] = set()
    for key in keys:
        relative = key.replace(prefix, "")
        if relative.startswith("analysis-"):
            engine = relative.split("/")[0].replace("analysis-", "")
            engines.add(engine)
    outputs: dict[str, dict] = {}
    for engine in engines:
        path = f"{db}/{job_id}/analysis-{engine}/analysis.json"
        if store.exists(path):
            outputs[engine] = store.read_json(path)
    return outputs


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/{job_id}/assignments", response_model=AssignmentResponse)
async def get_assignments(
    job_id: str,
    database_name: str = Query(..., description="Database name for artifact lookup"),
):
    """Read the current (latest version) assignment artifact."""
    store = _require_store()
    version = _latest_assignment_version(store, database_name, job_id)
    if version == 0:
        raise HTTPException(status_code=404, detail="No assignment artifact found")

    assignment = _read_assignment(store, database_name, job_id, version)
    return AssignmentResponse(assignment=assignment)


@router.put("/{job_id}/assignments", response_model=AssignmentResponse)
async def put_assignments(
    job_id: str,
    body: AssignmentOverrideRequest,
    database_name: str = Query(..., description="Database name for artifact lookup"),
):
    """Accept overrides or scope narrowing.

    Reads the current assignment, applies overrides, validates, writes a new
    versioned artifact, and returns validation warnings.
    Returns HTTP 422 for hard errors (e.g. query assigned to unanalyzed engine).
    """
    store = _require_store()

    # Read current assignment
    current_version = _latest_assignment_version(store, database_name, job_id)
    if current_version == 0:
        raise HTTPException(status_code=404, detail="No assignment artifact found to override")

    current = _read_assignment(store, database_name, job_id, current_version)

    # Build lookup for quick access
    qa_map: dict[str, QueryAssignment] = {qa.query_id: qa for qa in current.query_assignments}

    # Apply per-query overrides
    for override in body.overrides:
        qa = qa_map.get(override.query_id)
        if qa is None:
            raise HTTPException(
                status_code=400,
                detail=f"Query {override.query_id} not found in current assignment",
            )
        if override.assigned_engine is not None:
            qa.assigned_engine = override.assigned_engine
            qa.customer_override = True
        if override.in_scope is not None:
            qa.in_scope = override.in_scope

    # Apply table-level scope narrowing
    scope_warnings: list[str] = []
    if body.scope and body.scope.exclude_tables:
        excluded_tables = set(body.scope.exclude_tables)
        for qa in qa_map.values():
            tables = set(qa.source_tables)
            if tables and tables.issubset(excluded_tables):
                # All tables are excluded → mark query out-of-scope
                qa.in_scope = False
            elif tables & excluded_tables:
                # Query accesses both in-scope and out-of-scope tables
                scope_warnings.append(
                    f"WARNING [LOW]: Query {qa.query_id} accesses both "
                    f"in-scope and excluded tables "
                    f"({sorted(tables & excluded_tables)}). "
                    f"Keeping query in scope."
                )

    # Build new assignment
    new_version = current_version + 1
    new_assignment = current.model_copy(
        update={
            "version": new_version,
            "status": AssignmentStatus.CUSTOMER_MODIFIED,
            "timestamp": datetime.now(UTC),
            "query_assignments": list(qa_map.values()),
            "previous_version": current_version,
        }
    )

    # Validate
    collector_output = _read_collector_output(store, database_name, job_id)
    analysis_outputs = _read_analysis_outputs(store, database_name, job_id)
    validator = AssignmentValidator()
    validation = validator.validate(new_assignment, collector_output, analysis_outputs)

    # Hard errors → 422
    if not validation.valid:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Assignment validation failed with hard errors",
                "errors": validation.errors,
                "warnings": validation.warnings,
            },
        )

    # Persist warnings on the assignment (include scope warnings)
    all_warnings = scope_warnings + validation.warnings
    new_assignment.validation_warnings = all_warnings

    # Detect engines with zero in-scope queries (will be SKIPPED in schema design)
    engine_in_scope_counts: dict[str, int] = {}
    for qa in new_assignment.query_assignments:
        if qa.in_scope:
            engine_in_scope_counts.setdefault(qa.assigned_engine, 0)
            engine_in_scope_counts[qa.assigned_engine] = (
                engine_in_scope_counts[qa.assigned_engine] + 1
            )
    skipped_engines = [
        engine
        for engine in {qa.assigned_engine for qa in new_assignment.query_assignments}
        if engine_in_scope_counts.get(engine, 0) == 0
    ]

    # Write new versioned artifact
    path = f"{database_name}/{job_id}/assignment/v{new_version}/assignment.json"
    store.write_json(path, new_assignment.model_dump(mode="json"))

    return AssignmentResponse(
        assignment=new_assignment, validation=validation, skipped_engines=skipped_engines
    )
