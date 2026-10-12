"""Shared validation for the ``database_name`` identifier used to build artifact paths.

Every entry point that turns a caller-supplied ``database_name`` into an
artifact key (``<database_name>/<job_id>/...``) must reject a malformed or
path-traversing value before anything is read or written. The AWS Transform
subagent invocation path (``src/atx_orchestrator``) got this check first
(#418); this module is the single pattern it and every local entry point --
the ``scripts/run_*.py`` CLI scripts and the local API's assessment request
models -- share, so there is exactly one regex to keep in sync (#420).
"""

from __future__ import annotations

import re

#: 1 to 128 ASCII letters, digits, underscores or hyphens -- no path
#: separators, no ``.`` (so ``.`` and ``..`` are rejected outright), no
#: whitespace, no non-ASCII.
DATABASE_NAME_PATTERN = r"[A-Za-z0-9_-]{1,128}"
_DATABASE_NAME_RE = re.compile(DATABASE_NAME_PATTERN)

DATABASE_NAME_ERROR = (
    "database_name must contain 1 to 128 ASCII letters, digits, underscores or hyphens."
)

#: Real database names can be things like "Sales Data", "app.prod" or "café"
#: -- names this validator correctly rejects as artifact-path components. A
#: caller that derived the name from a collector file's own metadata (rather
#: than an explicit --db) needs to be told how to recover, not just that the
#: derived value was rejected.
DERIVED_DATABASE_NAME_ERROR = (
    "The collector file's metadata.database_name is not a valid artifact name "
    "(it must contain 1 to 128 ASCII letters, digits, underscores or hyphens); "
    "pass --db <name> to use a different one."
)


def validate_database_name(name: object) -> str:
    """Return ``name`` stripped of surrounding whitespace if it is a valid
    database identifier, else raise ``ValueError`` with a message safe to
    show the caller (it never echoes the rejected value, so it can't be used
    to smuggle attacker-controlled text into logs or error responses)."""
    if not isinstance(name, str) or not _DATABASE_NAME_RE.fullmatch(name.strip()):
        raise ValueError(DATABASE_NAME_ERROR)
    return name.strip()
