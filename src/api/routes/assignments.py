"""Assignment routes — read, override, and scope-narrow query assignments.

Requirements: 14.1, 14.2, 3.2
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from src.agents.referee.assignment_overrides import (
    AssignmentOverrideError,
    AssignmentValidationFailed,
    NoAssignmentFound,
    QueryOverrideInput,
    apply_assignment_overrides,
    apply_engine_choices,
    pending_choices_for_job,
    reroute_split_writes,
)
from src.agents.referee.pending_choices import EngineChoiceError
from src.api.dependencies import database_name_param, job_id_param
from src.contracts.assignment_models import (
    Assignment,
    AssignmentSource,
    PendingEngineChoice,
    ValidationResult,
)
from src.shared.contract_versioning import load_contract
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
    cached: bool | None = Field(
        None,
        description=(
            "Pin (true) or remove (false) the ElastiCache cache overlay. The cache never "
            "owns a query; the owner engine is unchanged (#296)."
        ),
    )


class TableChoice(BaseModel):
    """The customer's pick for one pending engine choice (#1003): every query
    listed under the table moves to ``engine``, one of its tied engines."""

    choice_id: str = Field(..., description="The choice's id (its table, from pending_choices)")
    engine: str = Field(..., description="One of the choice's tied engines")


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
    table_choices: list[TableChoice] = Field(
        default_factory=list,
        description=(
            "Per-table picks for pending engine choices (#1003); each applies to every "
            "query listed under the table"
        ),
    )
    decide_for_me: bool = Field(
        False,
        description=(
            "Decide every pending engine choice not picked here the way --auto does, and "
            "record each as decided automatically (#1003)"
        ),
    )
    base_version: int | None = Field(
        None,
        ge=1,
        description=(
            "The assignment version the edits were made on (#992). When the routing has "
            "moved on since (a newer version), the edit is refused with HTTP 409 so the "
            "customer reloads and re-applies it to the version they now see."
        ),
    )


class AssignmentResponse(BaseModel):
    """Response wrapper for assignment data with validation info."""

    assignment: Assignment
    validation: ValidationResult | None = None
    skipped_engines: list[str] = Field(
        default_factory=list,
        description="Engines with zero in-scope queries (schema design will be SKIPPED)",
    )
    pending_choices: list[PendingEngineChoice] = Field(
        default_factory=list,
        description=(
            "The open ties grouped per table (#1003): one customer choice each, with "
            "its queries, each engine's scores and trade-off, and why they tie"
        ),
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _latest_assignment_version(store: ArtifactStore, db: str, job_id: str) -> int:
    """Find the latest assignment version (0 when none). ADR-028 shared resolver."""
    return resolve_effective_assignment_version(store, db, job_id)


def _read_assignment(store: ArtifactStore, db: str, job_id: str, version: int) -> Assignment:
    """Read a specific assignment version (legacy cache owners moved, #296)."""
    from src.agents.referee.assignment_overrides import load_assignment_for_edit

    data, _, _, _ = load_assignment_for_edit(store, db, job_id, version)
    return load_contract(
        Assignment, data, artifact=f"{db}/{job_id}/assignment/v{version}/assignment.json"
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/{job_id}/assignments", response_model=AssignmentResponse)
async def get_assignments(
    job_id: str = Depends(job_id_param),
    database_name: str = Depends(database_name_param),
):
    """Read the current (latest version) assignment artifact."""
    store = _require_store()
    # A saved assignment that split a table's writes is re-routed to one source of
    # truth before it is shown (#992), so the customer reviews and edits that version.
    try:
        reroute_split_writes(store, database_name, job_id)
    except AssignmentOverrideError as exc:  # a reroute outside the engine scope (#1327)
        raise HTTPException(status_code=409, detail=str(exc)) from None
    version = _latest_assignment_version(store, database_name, job_id)
    if version == 0:
        raise HTTPException(status_code=404, detail="No assignment artifact found")

    assignment = _read_assignment(store, database_name, job_id, version)
    choices, _ = pending_choices_for_job(store, database_name, job_id, version)
    return AssignmentResponse(assignment=assignment, pending_choices=choices)


@router.put("/{job_id}/assignments", response_model=AssignmentResponse)
async def put_assignments(
    body: AssignmentOverrideRequest,
    job_id: str = Depends(job_id_param),
    database_name: str = Depends(database_name_param),
):
    """Accept overrides or scope narrowing.

    Delegates to the shared ``apply_assignment_overrides`` helper (ADR-028) so the
    web route and the ATX review gate write byte-identical artifacts, then maps
    the helper's domain errors to HTTP status codes. Returns HTTP 404 when there
    is no assignment to override, HTTP 422 for hard validation errors (e.g. query
    assigned to an unanalyzed engine), and HTTP 400 for every other rejected
    override -- an unknown query, the #381 losing-Aurora-engine override, or an
    engine a hard exclusion rules out for it (#548) -- caught through the shared
    ``AssignmentOverrideError`` base class (round 2 review) so a future rejection
    gets a real status code for free instead of an unhandled 500.
    """
    store = _require_store()

    overrides = [
        QueryOverrideInput(
            query_id=o.query_id,
            assigned_engine=o.assigned_engine,
            in_scope=o.in_scope,
            cached=o.cached,
        )
        for o in body.overrides
    ]
    exclude_tables = body.scope.exclude_tables if body.scope else None

    # The edits were made on ``base_version``; a newer version since (another edit,
    # or a source-of-truth re-route) means they were made on a routing that is no
    # longer current, so they are refused rather than applied to it blindly (#992).
    current_version = _latest_assignment_version(store, database_name, job_id)
    if body.base_version is not None and current_version and body.base_version != current_version:
        raise HTTPException(
            status_code=409,
            detail=(
                f"The routing changed since you opened it (version {body.base_version}, now "
                f"version {current_version}). Reload the assignment review and re-apply "
                "your changes."
            ),
        )
    rerouted = None
    try:
        # A saved assignment that split a table's writes is re-routed to one source
        # of truth first, as a new version the overrides then edit (#992).
        rerouted = reroute_split_writes(store, database_name, job_id)
        if body.table_choices or body.decide_for_me:
            # #1003: per-table picks (and "decide for me") go into the same version
            # as the per-query edits, through the shared gate.
            applied = apply_engine_choices(
                store,
                database_name,
                job_id,
                {c.choice_id: c.engine for c in body.table_choices},
                decide_for_me=body.decide_for_me,
                source=AssignmentSource.CUSTOMER_GATE,
                overrides=overrides,
                exclude_tables=exclude_tables,
            )
            result = applied.override if applied is not None else None
        else:
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
    except AssignmentValidationFailed as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "message": "Assignment validation failed with hard errors",
                "errors": exc.errors,
                "warnings": exc.warnings,
            },
        ) from None
    except (AssignmentOverrideError, EngineChoiceError) as exc:
        # Every other rejected override (UnknownQuery, LosingAuroraEngineOverride,
        # ExcludedEngineOverride, and any future AssignmentOverrideError subclass)
        # maps to the same 400 -- round 2 review: before this, only UnknownQuery
        # and ExcludedEngineOverride were caught by name here, so
        # LosingAuroraEngineOverride fell through to an unhandled 500.
        detail = str(exc)
        if rerouted is not None:
            # The re-route version stands even though these edits were refused.
            detail += (
                f" (The saved routing was first re-routed so each table's writes land on "
                f"one engine: assignment version {rerouted.assignment.version}.)"
            )
        raise HTTPException(status_code=400, detail=detail) from None

    if result is None:
        # "Decide for me" with nothing pending and no edit: the current version.
        version = _latest_assignment_version(store, database_name, job_id)
        choices, _ = pending_choices_for_job(store, database_name, job_id, version)
        return AssignmentResponse(
            assignment=_read_assignment(store, database_name, job_id, version),
            pending_choices=choices,
        )
    choices, _ = pending_choices_for_job(store, database_name, job_id, result.assignment.version)
    return AssignmentResponse(
        assignment=result.assignment,
        validation=result.validation,
        skipped_engines=result.skipped_engines,
        pending_choices=choices,
    )
