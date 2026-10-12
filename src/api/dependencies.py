"""Shared FastAPI dependencies for routes that take an artifact-path
identifier as a plain query or path parameter, rather than through a
request body model.

``AssessmentRequest`` and ``PrepareAssessmentRequest`` validate
``database_name`` through a pydantic ``field_validator`` (#420). Every other
route that builds the same ``<database_name>/<job_id>/...`` artifact path
from a query parameter needs the same check; a FastAPI dependency is the
equivalent mechanism there -- it runs while FastAPI resolves the request,
before the route body executes, so a rejected value never reaches a store
or service call (#505).

``delete_upload``'s ``filename`` path parameter is a different shape (real
filenames, e.g. ``collector-output.json``, are not valid artifact names --
they contain a dot), but the same principle applies: it must be a single
path component before it's joined onto the upload prefix and deleted.

``job_id`` is a path parameter (``/{job_id}/...``) on nearly every route in
this API, and is used the same unchecked way ``database_name`` was before
#420/#505 -- including bare, job-id-only keys like ``.meta/<job_id>.json``
(``src/orchestrator/local_orchestrator.py``). #508 adds ``job_id_param`` to
cover it the same way.
"""

from __future__ import annotations

import re

from fastapi import HTTPException, Query

from src.shared.database_name import validate_database_name
from src.shared.job_id import validate_job_id

_FILENAME_ERROR = (
    "filename must be a single path component: no '/', '\\', NUL, and not '.' or '..'."
)
_FILENAME_UNSAFE_RE = re.compile(r"[/\\\x00]")


def database_name_param(
    database_name: str = Query(..., description="Database name for artifact lookup"),
) -> str:
    """FastAPI dependency: validate the ``database_name`` query parameter
    the same way ``AssessmentRequest``/``PrepareAssessmentRequest`` validate
    their field. Raises ``HTTPException(422)`` instead of ``ValueError`` so
    FastAPI turns it into the same kind of response a body validation error
    would produce."""
    try:
        return validate_database_name(database_name)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def job_id_param(job_id: str) -> str:
    """FastAPI dependency: validate the ``job_id`` path parameter the same
    way ``database_name_param`` validates ``database_name``."""
    try:
        return validate_job_id(job_id)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def validated_filename(filename: str) -> str:
    """FastAPI dependency: the ``filename`` path parameter, constrained to a
    single path component so it can't escape the upload prefix it's joined
    onto. Checked against the decoded value (not the raw URL), so a
    percent-encoded separator or ``..`` that the router already decoded
    into the parameter is still caught."""
    if (
        not isinstance(filename, str)
        or not filename
        or filename in (".", "..")
        or _FILENAME_UNSAFE_RE.search(filename)
    ):
        raise HTTPException(status_code=422, detail=_FILENAME_ERROR)
    return filename
