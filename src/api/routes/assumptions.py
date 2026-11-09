# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The results page's assumptions panel (#1991).

``GET .../assumptions``: each assumption a customer can set (growth and the peak), with
the value and origin the job's report states, its range (``src.shared.assumptions``
``SETTABLE``, the one the assumptions file is checked against) and whether this server
can re-run what a change affects.

``POST .../assumptions``: a body in the assumptions file's own form, such as
``{"growth": 3}``, checked by the file's own parser (``parse_assumptions``): an unknown
key, a collected item, a value out of range or not a finite number, a duplicate key, or a
body that is not one small JSON object is refused before anything is written. The change
is recorded under ``decisions/assumptions.json`` with origin ``ui`` and re-runs only the
phases it affects (``src.orchestrator.assumption_change``).

The routes are on the local API, behind its loopback bind, Host allowlist, CORS origin
and body cap, as every other write route. The POST also requires a JSON content type
(it reads its body itself, so FastAPI's own JSON check does not run) and caps its body at
the assumptions file's size.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from src.api.dependencies import job_id_param, require_json_content_type
from src.api.services.local_execution import LocalExecutionService
from src.orchestrator.assumption_change import (
    AssumptionChangeRefused,
    AssumptionRerunFailed,
    apply_assumption_change,
    panel_state,
)
from src.orchestrator.job_lock import JobBusyError
from src.shared.assumptions_file import MAX_FILE_BYTES, AssumptionsFileError, parse_assumptions
from src.shared.database_name import validate_database_name
from src.shared.job_id import validate_job_id
from src.storage.artifact_store import ArtifactStore

router = APIRouter(prefix="/api/v1/assessments", tags=["assumptions"])

# Injected by main.py at startup
artifact_store: ArtifactStore | None = None
orchestrator: object | None = None  # LocalOrchestrator
sfn_service: LocalExecutionService | None = None

_TOO_LARGE = f"Request body is larger than {MAX_FILE_BYTES} bytes."


def _services() -> tuple[ArtifactStore, object, LocalExecutionService]:
    if not artifact_store or not orchestrator or not sfn_service:
        raise HTTPException(status_code=503, detail="Services not configured")
    return artifact_store, orchestrator, sfn_service


def _execution(store: ArtifactStore, sfn: LocalExecutionService, job_id: str) -> tuple[str, str]:
    """The job's database name and execution status; 404 for an unknown job.

    ``job_id`` is already checked (``job_id_param``); the database name the execution
    states is checked by the same rule as every route's (``validate_database_name``),
    and the job's folder must resolve inside the artifact root (a symlinked database or
    job folder pointing elsewhere is no job here), before any store call (#1991
    review)."""
    execution = sfn.describe_execution(validate_job_id(job_id))
    if not execution:
        raise HTTPException(status_code=404, detail="Assessment not found")
    try:
        db_name = validate_database_name(str(execution.get("input", {}).get("database_name", "")))
    except ValueError:
        raise HTTPException(status_code=404, detail="Assessment not found") from None
    root = getattr(store, "base_dir", None)
    if root is not None:
        base = os.path.realpath(str(root))
        job_dir = os.path.realpath(os.path.join(base, db_name, job_id))
        if not job_dir.startswith(base + os.sep):
            raise HTTPException(status_code=404, detail="Assessment not found")
    return db_name, str(execution.get("status") or "")


@router.get("/{job_id}/assumptions")
def get_assumptions(job_id: str = Depends(job_id_param)):
    """The assumptions a customer can set, as the job's report states them."""
    store, orch, sfn = _services()
    db_name, status = _execution(store, sfn, job_id)
    try:
        return panel_state(store, db_name, job_id, str(getattr(orch, "llm_mode", "none")), status)
    except ValueError:  # the store refused a path outside the artifact root
        raise HTTPException(status_code=404, detail="Assessment not found") from None


@router.post("/{job_id}/assumptions", dependencies=[Depends(require_json_content_type)])
async def post_assumptions(request: Request, job_id: str = Depends(job_id_param)):
    """Record a change (origin ``ui``) and re-run the phases it needs.

    413: the body is larger than an assumptions file, by its length or as read. 422: the
    body breaks the assumptions file's rules; the detail names the field, never a key or
    value sent. 409: the job has not finished (it is running, waiting, cancelled or
    failed), has no results, another change to it is running (``/resume`` and the
    schema confirm routes take the same lock), or a re-run needs a model this server
    does not have. 500: a re-run failed and the job was put back as it was. Nothing is
    left written on any of them.
    """
    store, orch, sfn = _services()
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail=_TOO_LARGE)
    raw = await request.body()
    if len(raw) > MAX_FILE_BYTES:  # a chunked body sends no length
        raise HTTPException(status_code=413, detail=_TOO_LARGE)
    try:
        values = parse_assumptions(raw, subject="the request", echo=False).values
    except AssumptionsFileError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    db_name, status = _execution(store, sfn, job_id)
    root = str(getattr(store, "base_dir", ""))
    if not root:
        raise HTTPException(status_code=503, detail="Artifact store not configured")
    try:
        result = await run_in_threadpool(
            apply_assumption_change, orch, store, db_name, job_id, values, root, status
        )
    except ValueError:  # the store refused a path outside the artifact root
        raise HTTPException(status_code=404, detail="Assessment not found") from None
    except (AssumptionChangeRefused, JobBusyError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except AssumptionRerunFailed as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from None
    return {"job_id": job_id, **result}
