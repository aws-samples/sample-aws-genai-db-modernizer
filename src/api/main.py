"""FastAPI application for Database Modernizer Assessment."""

import hashlib
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Database Modernizer Assessment API")

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

# Only answer requests addressed to the loopback host names
# (MODERNIZER_ALLOWED_HOSTS overrides; see src/api/host_guard.py). Added last
# so it runs first.
from src.api.host_guard import HostAllowlistMiddleware, allowed_hosts_from_env  # noqa: E402

_allowed_hosts = allowed_hosts_from_env()
if _allowed_hosts is not None:
    app.add_middleware(HostAllowlistMiddleware, allowed_hosts=_allowed_hosts)


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
    settings,
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

# Wire both services to all route modules that need them
assessments.sfn_service = _sfn
results.sfn_service = _sfn
dashboard.sfn_service = _sfn
query_journeys.sfn_service = _sfn
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
app.include_router(settings.router)
app.include_router(assignments.router)
app.include_router(phases.router)
app.include_router(agent_interaction.router)
app.include_router(schema_revisions.router)
app.include_router(query_journeys.router)
app.include_router(graph.router)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)  # nosec B104 — local dev only
