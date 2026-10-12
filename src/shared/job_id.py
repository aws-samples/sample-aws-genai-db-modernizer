"""Shared validation for the ``job_id`` identifier used to build artifact paths.

Every entry point that turns a caller-supplied ``job_id`` into an artifact
key (``<database_name>/<job_id>/...``, or a bare one like
``.meta/<job_id>.json``) must reject a malformed or path-traversing value
before anything is read or written -- the same risk ``validate_database_name``
(#420) guards against, just on the other half of the path. #508 adds this
module next to it.

The accepted shape must cover every ``job_id`` the code already generates or
accepts: ``str(uuid.uuid4())[:8]`` (8 lowercase hex characters, e.g.
``scripts/run_assessment.py``, ``scripts/run_collect.py``) and a full
``str(uuid.uuid4())`` (36 lowercase hex characters and hyphens, e.g.
``src/api/routes/assessments.py``'s ``prepare_assessment``/``create_assessment``),
plus the human-chosen job IDs used throughout the test suite (``job1``,
``test-job-001``, ...). All of those are ASCII letters, digits, underscores
and hyphens, so this reuses ``validate_database_name``'s character class.
"""

from __future__ import annotations

import re

#: 1 to 128 ASCII letters, digits, underscores or hyphens -- no path
#: separators, no ``.`` (so ``.`` and ``..`` are rejected outright), no
#: whitespace, no non-ASCII. Covers both the short hex job IDs the pipeline
#: generates and a full uuid4 string.
JOB_ID_PATTERN = r"[A-Za-z0-9_-]{1,128}"
_JOB_ID_RE = re.compile(JOB_ID_PATTERN)

JOB_ID_ERROR = "job_id must contain 1 to 128 ASCII letters, digits, underscores or hyphens."


def validate_job_id(job_id: object) -> str:
    """Return ``job_id`` stripped of surrounding whitespace if it is a valid
    job identifier, else raise ``ValueError`` with a message safe to show
    the caller (it never echoes the rejected value, so it can't be used to
    smuggle attacker-controlled text into logs or error responses)."""
    if not isinstance(job_id, str) or not _JOB_ID_RE.fullmatch(job_id.strip()):
        raise ValueError(JOB_ID_ERROR)
    return job_id.strip()
