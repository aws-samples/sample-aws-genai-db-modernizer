"""FastAPI application for Database Modernizer Assessment."""

import hashlib
import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.shared.bedrock_guardrail import require_valid_guardrail_config
from src.shared.contract_versioning import ContractVersionError
from src.shared.package_version import get_package_version

#: The background thread of the one-time redaction of stored inputs (#723), for tests.
redaction_thread: threading.Thread | None = None


@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global redaction_thread
    # A partial guardrail setting stops the API at start, never at the first model call.
    require_valid_guardrail_config()
    # In the background, after start-up: it can never delay or stop the API.
    redaction_thread = threading.Thread(
        target=_redact_stored_inputs, name="stored-input-redaction", daemon=True
    )
    redaction_thread.start()
    yield


app = FastAPI(
    title="Database Modernizer Assessment API",
    version=get_package_version(),
    lifespan=_lifespan,
)

_raw_sha = os.environ.get("COMMIT_SHA", "unknown")
BUILD_VERSION = (
    hashlib.sha256(_raw_sha.encode()).hexdigest()[:12] if _raw_sha != "unknown" else "unknown"
)

# ============================================================
# CORS — the API only runs locally, so it only accepts requests
# from the local UI (ALLOWED_ORIGIN overrides for a non-default UI port).
# ============================================================
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "http://localhost:3000")

origins = [ALLOWED_ORIGIN, "http://localhost:3000"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)

# Cap every request body (413) before any route or parser sees it (#723).
from src.api.body_limit import BodySizeLimitMiddleware, max_body_bytes_from_env  # noqa: E402

# Only answer requests addressed to the loopback host names
# (MODERNIZER_ALLOWED_HOSTS overrides; see src/api/host_guard.py). Added last
# so it runs first.
from src.api.host_guard import HostAllowlistMiddleware, allowed_hosts_from_env  # noqa: E402

app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_body_bytes_from_env())

_allowed_hosts = allowed_hosts_from_env()
if _allowed_hosts is not None:
    app.add_middleware(HostAllowlistMiddleware, allowed_hosts=_allowed_hosts)


# A request carrying a secret is refused (src/api/secret_guard.py, #723); keep the
# server's access log from recording one sent in a query string.
import logging  # noqa: E402

from src.api.secret_guard import RedactSecretQueryFilter  # noqa: E402

logging.getLogger("uvicorn.access").addFilter(RedactSecretQueryFilter())


# ============================================================
# Health check
# ============================================================
@app.get("/")
async def root():
    """Root endpoint."""
    return {"message": "Database Modernizer Assessment API"}


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "healthy", "version": BUILD_VERSION}


# ============================================================
# Validation errors — never echo the rejected value back
# ============================================================
@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """FastAPI's default 422 handler puts the rejected value in each error's
    "input" field (and sometimes mirrors it into "ctx") -- including a
    pydantic field_validator's own ValueError, e.g. AssessmentRequest's
    database_name/job_id checks (#420/#505/#508), which deliberately never
    echo the rejected value in their *message* so it can't carry
    attacker-controlled text into a response, only for FastAPI's default
    serialization to put it right back via "input"/"ctx". Strip both here,
    for every validation error, not just those two fields."""
    errors = jsonable_encoder(exc.errors())
    cleaned = []
    for error in errors:
        rejected = error.pop("input", None)
        ctx = error.get("ctx")
        if isinstance(ctx, dict):
            error["ctx"] = {k: v for k, v in ctx.items() if v != rejected}
            if not error["ctx"]:
                del error["ctx"]
        cleaned.append(error)
    return JSONResponse(status_code=422, content={"detail": cleaned})


# ============================================================
# Stored artifact at another contract MAJOR (ADR-008 Decision 4)
# ============================================================
@app.exception_handler(ContractVersionError)
async def contract_version_exception_handler(request: Request, exc: ContractVersionError):
    """A stored artifact this code cannot read (another contract MAJOR) is a
    conflict with the stored job, not a server fault. The response says so in
    plain words (no artifact path, class name or policy reference); the full
    detail goes to the server log."""
    logging.getLogger(__name__).warning("refused stored artifact: %s", exc)
    return JSONResponse(status_code=409, content={"detail": exc.public_message})


# ============================================================
# Initialize services and wire routes
# ============================================================
from src.api.routes import (  # noqa: E402
    agent_interaction,
    assessments,
    assignments,
    dashboard,
    graph,
    phases,
    query_journeys,
    results,
    schema_revisions,
)
from src.api.services.local_execution import LocalExecutionService  # noqa: E402
from src.api.services.local_s3 import LocalS3Service  # noqa: E402
from src.orchestrator import create_orchestrator  # noqa: E402
from src.storage import create_artifact_store  # noqa: E402

# Wire ArtifactStore first — local services depend on it
_artifact_store = create_artifact_store()

# Create service instances. The API always runs against the local
# filesystem-backed services (ADR: hosted deployment retired, #175).
_sfn = LocalExecutionService(_artifact_store)
_s3 = LocalS3Service(_artifact_store)


def _redact_stored_inputs() -> None:
    """Once (see the marker in stored_input_redaction): redact credentials stored by API
    versions before #723. Never raises."""
    from src.api.stored_input_redaction import redact_stored_inputs_once

    base_dir = getattr(_artifact_store, "base_dir", None)
    if base_dir is not None:
        redact_stored_inputs_once(base_dir)


# Wire both services to all route modules that need them
assessments.sfn_service = _sfn
results.sfn_service = _sfn
dashboard.sfn_service = _sfn
query_journeys.sfn_service = _sfn
phases.sfn_service = _sfn
schema_revisions.sfn_service = _sfn
assessments.s3_service = _s3
results.s3_service = _s3

# Wire ArtifactStore and Orchestrator to new route modules
assignments.artifact_store = _artifact_store
agent_interaction.artifact_store = _artifact_store
schema_revisions.artifact_store = _artifact_store
query_journeys.artifact_store = _artifact_store

# Wire graph route
from src.graph import GraphStoreCache  # noqa: E402
from src.graph.persistence import GraphPersistence  # noqa: E402

graph.artifact_store = _artifact_store
graph.graph_cache = GraphStoreCache(
    max_size=5,
    base_dir=(
        str(_artifact_store.base_dir) if hasattr(_artifact_store, "base_dir") else "./artifacts"
    ),
)
graph.graph_persistence = GraphPersistence(_artifact_store)
graph.sfn_service = _sfn

_orchestrator = create_orchestrator(store=_artifact_store)

phases.orchestrator = _orchestrator
schema_revisions.orchestrator = _orchestrator

# Register routers
app.include_router(assessments.router)
app.include_router(results.router)
app.include_router(dashboard.router)
app.include_router(assignments.router)
app.include_router(phases.router)
app.include_router(agent_interaction.router)
app.include_router(schema_revisions.router)
app.include_router(query_journeys.router)
app.include_router(graph.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)  # nosec B104 — local dev only
