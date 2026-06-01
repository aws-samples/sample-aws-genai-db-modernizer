"""Schema revision loop API routes.

Endpoints for retrieving schema versions, submitting revision requests,
and confirming schema designs per engine or in bulk.

Requirements: schema-design revision loop spec (2026-05-01)
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from src.contracts.schema_revision_models import (
    SchemaConfirmation,
    SchemaRevisionRequest,
    SchemaVersionMeta,
    VerificationResult,
)
from src.storage.artifact_store import ArtifactStore

router = APIRouter(prefix="/api/v1/assessments", tags=["schema-revisions"])

# Injected by main.py at startup
artifact_store: ArtifactStore | None = None
orchestrator: object | None = None  # LocalOrchestrator or StepFunctionsOrchestrator


def _require_store() -> ArtifactStore:
    if not artifact_store:
        raise HTTPException(status_code=503, detail="ArtifactStore not configured")
    return artifact_store


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class SchemaResponse(BaseModel):
    """Schema output + version metadata for a single version."""

    schema_output: dict
    meta: SchemaVersionMeta


class VersionsResponse(BaseModel):
    """All version metadata for an engine, ordered ascending."""

    versions: list[SchemaVersionMeta]


class ConfirmResponse(BaseModel):
    """Result of confirming a single engine schema."""

    confirmed_version: int
    engine: str


class ConfirmAllResponse(BaseModel):
    """Result of confirming all active engine schemas."""

    confirmed: dict[str, int]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _latest_schema_version(store: ArtifactStore, db: str, job_id: str, engine: str) -> int:
    """Return the highest version number found under schema-{engine}/v*/.

    Returns 0 if no versions exist.
    """
    prefix = f"{db}/{job_id}/schema-{engine}/"
    keys = store.list_prefix(prefix)
    versions: list[int] = []
    for key in keys:
        relative = key.replace(prefix, "")
        parts = relative.split("/")
        if parts and parts[0].startswith("v"):
            try:
                versions.append(int(parts[0][1:]))
            except ValueError:
                continue
    return max(versions) if versions else 0


def _read_version_meta(
    store: ArtifactStore, db: str, job_id: str, engine: str, version: int
) -> SchemaVersionMeta:
    path = f"{db}/{job_id}/schema-{engine}/v{version}/version_meta.json"
    if store.exists(path):
        data = store.read_json(path)
        return SchemaVersionMeta.model_validate(data)
    # Initial design run doesn't write version_meta — synthesize a default
    return SchemaVersionMeta(
        version=version,
        base_version=None,
        initiated_by="system",
        timestamp=datetime.now(UTC),
        modifications=None,
        redesigned_groups=[],
        verification=VerificationResult(passed=True, hard_errors=[], warnings=[]),
        changelog=[],
    )


def _read_schema_output(
    store: ArtifactStore, db: str, job_id: str, engine: str, version: int
) -> dict:
    path = f"{db}/{job_id}/schema-{engine}/v{version}/schema_output.json"
    return store.read_json(path)


def _get_active_engines(store: ArtifactStore, db: str, job_id: str) -> list[str]:
    """Return the list of engines selected by triage for this job."""
    triage_path = f"{db}/{job_id}/referee-triage/triage.json"
    triage_data = store.read_json(triage_path)
    return [a["agent_type"] for a in triage_data.get("selected_agents", [])]


def _write_confirmed_json(
    store: ArtifactStore,
    db: str,
    job_id: str,
    engine: str,
    confirmed_version: int,
) -> None:
    confirmation = SchemaConfirmation(
        confirmed_version=confirmed_version,
        confirmed_at=datetime.now(UTC),
        engine=engine,
    )
    path = f"{db}/{job_id}/schema-{engine}/confirmed.json"
    store.write_json(path, confirmation.model_dump(mode="json"))


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/{job_id}/schema/{engine}", response_model=SchemaResponse)
async def get_schema(
    job_id: str,
    engine: str,
    database_name: str = Query(..., description="Database name for artifact lookup"),
    version: int | None = Query(None, description="Schema version (omit for latest)"),
):
    """Return schema output and version metadata for a specific engine and version."""
    store = _require_store()

    latest = _latest_schema_version(store, database_name, job_id, engine)
    if latest == 0:
        raise HTTPException(
            status_code=404,
            detail=f"No schema found for engine '{engine}'",
        )

    # Resolve target version
    target = version if version is not None else latest

    # Verify the requested version actually exists
    meta_path = f"{database_name}/{job_id}/schema-{engine}/v{target}/version_meta.json"
    schema_path = f"{database_name}/{job_id}/schema-{engine}/v{target}/schema_output.json"
    if not store.exists(meta_path) and not store.exists(schema_path):
        raise HTTPException(
            status_code=404,
            detail=f"Schema version {target} not found for engine '{engine}'",
        )

    schema_output = _read_schema_output(store, database_name, job_id, engine, target)
    meta = _read_version_meta(store, database_name, job_id, engine, target)

    return SchemaResponse(schema_output=schema_output, meta=meta)


@router.get("/{job_id}/schema/{engine}/versions", response_model=VersionsResponse)
async def get_schema_versions(
    job_id: str,
    engine: str,
    database_name: str = Query(..., description="Database name for artifact lookup"),
):
    """Return full version history for an engine, ordered by version number ascending."""
    store = _require_store()

    prefix = f"{database_name}/{job_id}/schema-{engine}/"
    keys = store.list_prefix(prefix)

    # Collect distinct version numbers that have a version_meta.json
    version_nums: list[int] = []
    for key in keys:
        relative = key.replace(prefix, "")
        parts = relative.split("/")
        if len(parts) >= 2 and parts[0].startswith("v") and parts[1] == "version_meta.json":
            try:
                version_nums.append(int(parts[0][1:]))
            except ValueError:
                continue

    version_nums.sort()

    metas: list[SchemaVersionMeta] = []
    for v in version_nums:
        metas.append(_read_version_meta(store, database_name, job_id, engine, v))

    return VersionsResponse(versions=metas)


@router.put("/{job_id}/schema/{engine}/revisions")
async def put_schema_revision(
    job_id: str,
    engine: str,
    body: SchemaRevisionRequest,
    database_name: str = Query(..., description="Database name for artifact lookup"),
):
    """Submit a schema revision request for an engine.

    Performs optimistic concurrency check: returns 409 if base_version != latest.
    Full revision execution will be wired in a subsequent task (currently returns 501).
    """
    # Sanitize free-form customer input before any processing
    from src.api.services.input_sanitizer import InputSanitizationError, sanitize_revision_request

    try:
        sanitize_revision_request(body.model_dump())
    except InputSanitizationError as e:
        raise HTTPException(
            status_code=422,
            detail=f"Input validation failed: {e.reason} (field: {e.field})",
        ) from None

    store = _require_store()

    latest = _latest_schema_version(store, database_name, job_id, engine)
    if latest == 0:
        raise HTTPException(
            status_code=404,
            detail=f"No schema found for engine '{engine}' — cannot submit revision",
        )

    if body.base_version != latest:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Stale base_version: request has {body.base_version} but latest is {latest}. "
                "Re-fetch the current schema and resubmit."
            ),
        )

    # Concurrency check passes — execute the revision pipeline
    from src.agents.schema_design.revision_handler import VerificationError, execute_revision

    try:
        new_schema, meta = execute_revision(
            job_id=job_id,
            database_name=database_name,
            engine=engine,
            request=body,
            store=store,
        )
    except VerificationError as e:
        raise HTTPException(
            status_code=422,
            detail={"message": str(e), "verification": e.result.model_dump(mode="json")},
        ) from None

    return SchemaResponse(schema_output=new_schema, meta=meta)


@router.post("/{job_id}/schema/{engine}/confirm", response_model=ConfirmResponse)
async def confirm_schema(
    job_id: str,
    engine: str,
    database_name: str = Query(..., description="Database name for artifact lookup"),
):
    """Confirm the latest schema version for an engine. Writes confirmed.json."""
    store = _require_store()

    latest = _latest_schema_version(store, database_name, job_id, engine)
    if latest == 0:
        raise HTTPException(
            status_code=404,
            detail=f"No schema found for engine '{engine}' — cannot confirm",
        )

    _write_confirmed_json(store, database_name, job_id, engine, latest)

    return ConfirmResponse(confirmed_version=latest, engine=engine)


@router.post("/{job_id}/schema/confirm-all", response_model=ConfirmAllResponse)
async def confirm_all_schemas(
    job_id: str,
    database_name: str = Query(..., description="Database name for artifact lookup"),
):
    """Confirm the latest schema version for every active engine.

    Active engines are read from the triage output. Returns a mapping of
    engine → confirmed_version. Returns 400 if no engines are found.
    """
    store = _require_store()

    engines = _get_active_engines(store, database_name, job_id)
    if not engines:
        raise HTTPException(
            status_code=400,
            detail="No active engines found in triage output — nothing to confirm",
        )

    confirmed: dict[str, int] = {}
    for engine in engines:
        latest = _latest_schema_version(store, database_name, job_id, engine)
        if latest == 0:
            # Engine was selected by triage but has no schema yet — skip silently
            continue
        _write_confirmed_json(store, database_name, job_id, engine, latest)
        confirmed[engine] = latest

    # Transition SCHEMA_DESIGN phase to COMPLETED
    if orchestrator and hasattr(orchestrator, "confirm_schema_design"):
        orchestrator.confirm_schema_design(job_id)

    return ConfirmAllResponse(confirmed=confirmed)
