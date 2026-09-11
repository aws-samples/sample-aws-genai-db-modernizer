"""Assignment routes — read, override, and scope-narrow query assignments.

Requirements: 14.1, 14.2, 3.2
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from src.agents.referee.assignment_overrides import (
    AssignmentValidationFailed,
    NoAssignmentFound,
    QueryOverrideInput,
    UnknownQuery,
    apply_assignment_overrides,
)
from src.contracts.assignment_models import Assignment, AssignmentSource, ValidationResult
from src.storage.artifact_store import ArtifactStore
from src.storage.assignment_versioning import resolve_effective_assignment_version

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
    """Find the latest assignment version (0 when none). ADR-028 shared resolver."""
    return resolve_effective_assignment_version(store, db, job_id)


def _read_assignment(store: ArtifactStore, db: str, job_id: str, version: int) -> Assignment:
    """Read a specific assignment version."""
    path = f"{db}/{job_id}/assignment/v{version}/assignment.json"
    data = store.read_json(path)
    return Assignment.model_validate(data)


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

    Delegates to the shared ``apply_assignment_overrides`` helper (ADR-028) so the
    web route and the ATX review gate write byte-identical artifacts, then maps
    the helper's domain errors to HTTP status codes.
    Returns HTTP 422 for hard errors (e.g. query assigned to unanalyzed engine).
    """
    store = _require_store()

    overrides = [
        QueryOverrideInput(
            query_id=o.query_id,
            assigned_engine=o.assigned_engine,
            in_scope=o.in_scope,
        )
        for o in body.overrides
    ]
    exclude_tables = body.scope.exclude_tables if body.scope else None

    try:
        result = apply_assignment_overrides(
            store,
            database_name,
            job_id,
            overrides,
            exclude_tables,
            source=AssignmentSource.CUSTOMER_GATE,
        )
    except NoAssignmentFound:
        raise HTTPException(
            status_code=404, detail="No assignment artifact found to override"
        ) from None
    except UnknownQuery as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    except AssignmentValidationFailed as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Assignment validation failed with hard errors",
                "errors": exc.errors,
                "warnings": exc.warnings,
            },
        ) from None

    return AssignmentResponse(
        assignment=result.assignment,
        validation=result.validation,
        skipped_engines=result.skipped_engines,
    )
