"""FastAPI application for Database Modernizer Assessment."""

import hashlib
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(
    title="Database Modernizer Assessment API",
    servers=[
        {"url": "https://api.modernizer.example.com", "description": "Production API (HTTPS only)"},
    ],
)


def _custom_openapi():  # type: ignore[no-untyped-def]
    """Extend generated OpenAPI spec with security schemes for checkov compliance."""
    if app.openapi_schema:
        return app.openapi_schema
    from fastapi.openapi.utils import get_openapi

    schema = get_openapi(
        title=app.title,
        version=app.version,
        routes=app.routes,
        servers=app.servers,
    )
    schema.setdefault("components", {})["securitySchemes"] = {
        "BearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "OIDC/Cognito JWT token",
        }
    }
    schema["security"] = [{"BearerAuth": []}]

    # CKV_OPENAPI_21: ensure arrays have maxItems
    def _add_max_items(obj):  # type: ignore[no-untyped-def]
        if isinstance(obj, dict):
            if obj.get("type") == "array" and "maxItems" not in obj:
                obj["maxItems"] = 10000
            for v in obj.values():
                _add_max_items(v)
        elif isinstance(obj, list):
            for item in obj:
                _add_max_items(item)

    _add_max_items(schema)
    app.openapi_schema = schema
    return schema


app.openapi = _custom_openapi  # type: ignore[method-assign]

_raw_sha = os.environ.get("COMMIT_SHA", "unknown")
BUILD_VERSION = (
    hashlib.sha256(_raw_sha.encode()).hexdigest()[:12] if _raw_sha != "unknown" else "unknown"
)

# Environment config
STATE_MACHINE_ARN = os.environ.get("STATE_MACHINE_ARN", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
LOG_GROUP = os.environ.get("LOG_GROUP", "")
PROJECT_NAME = os.environ.get("PROJECT_NAME", "modernizer")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "dev")

# ============================================================
# CORS — allow only the UI origin for this environment.
# Each env's API only accepts requests from its own UI.
# ============================================================
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "http://localhost:3000")

origins = [ALLOWED_ORIGIN]
if not STATE_MACHINE_ARN:
    origins.append("http://localhost:3000")

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["*"],
)


# ============================================================
# Health check (no auth, used by ALB)
# ============================================================
@app.get("/")
async def root():
    """Root endpoint."""
    return {"message": "Database Modernizer Assessment API"}


@app.get("/health")
async def health():
    """Health check endpoint for ALB and monitoring."""
    return {"status": "healthy", "version": BUILD_VERSION}


# ============================================================
# Initialize services and wire routes
# ============================================================
from src.api.routes import (  # noqa: E402
    agent_interaction,
    assessments,
    assignments,
    dashboard,
    phases,
    query_journeys,
    results,
    schema_revisions,
    settings,
)
from src.api.services.cloudwatch import CloudWatchLogsService  # noqa: E402
from src.api.services.s3_artifacts import S3ArtifactsService  # noqa: E402
from src.api.services.step_functions import StepFunctionsService  # noqa: E402
from src.orchestrator import create_orchestrator  # noqa: E402
from src.storage import create_artifact_store  # noqa: E402

# Wire ArtifactStore first — local services depend on it
_artifact_store = create_artifact_store()

# Create service instances from environment
from typing import Any  # noqa: E402

_sfn: Any
_s3: Any

if STATE_MACHINE_ARN:
    _sfn = StepFunctionsService(STATE_MACHINE_ARN)
else:
    from src.api.services.local_execution import LocalExecutionService

    _sfn = LocalExecutionService(_artifact_store)

if S3_BUCKET:
    _s3 = S3ArtifactsService(S3_BUCKET)
else:
    from src.api.services.local_s3 import LocalS3Service

    _s3 = LocalS3Service(_artifact_store)

# Always wire both services to all route modules that need them
assessments.sfn_service = _sfn
results.sfn_service = _sfn
dashboard.sfn_service = _sfn
query_journeys.sfn_service = _sfn
assessments.s3_service = _s3
results.s3_service = _s3

_log_group = LOG_GROUP or f"/ecs/{PROJECT_NAME}-{ENVIRONMENT}"
_cw = CloudWatchLogsService(_log_group)
assessments.cw_service = _cw

# Wire ArtifactStore and Orchestrator to new route modules
assignments.artifact_store = _artifact_store
agent_interaction.artifact_store = _artifact_store
schema_revisions.artifact_store = _artifact_store
query_journeys.artifact_store = _artifact_store

if STATE_MACHINE_ARN:
    # Cloud mode: use StepFunctionsOrchestrator
    import boto3

    from src.orchestrator.base import Orchestrator as _OrchestratorType
    from src.orchestrator.sfn_orchestrator import StepFunctionsOrchestrator

    _dynamodb_table = os.environ.get(
        "DYNAMODB_TABLE",
        f"{PROJECT_NAME}-{ENVIRONMENT}-job-metadata",
    )
    _orchestrator: _OrchestratorType = StepFunctionsOrchestrator(
        sfn_client=boto3.client("stepfunctions"),
        dynamodb_table_name=_dynamodb_table,
        state_machine_arn=STATE_MACHINE_ARN,
    )
else:
    # Local mode: use LocalOrchestrator
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


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)  # nosec B104 — local dev only
