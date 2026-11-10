# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Where the routine-migration artifact lives, and its readers (#1934, #1938).

``<db>/<job>/routines/v<N>/routine_migration.json`` is written by the routine-migration
phase (``src.tools.routines.migration``, which re-exports these). The readers live here,
apart from the phase, so synthesis can read the artifact without reaching the routine
checks and rewrites (ADR-034 Decision 8: no routing or synthesis module reaches them).
"""

from __future__ import annotations

from typing import Any

from src.contracts.routine_migration import RoutineMigrationContract


def routine_migration_path(database_name: str, job_id: str, version: int) -> str:
    """The artifact key for ``version``: ``<db>/<job>/routines/v<N>/routine_migration.json``."""
    return f"{database_name}/{job_id}/routines/v{version}/routine_migration.json"


def read_routine_migration(
    store: Any, database_name: str, job_id: str, assignment_version: int
) -> RoutineMigrationContract | None:
    """The routine-migration artifact for ``assignment_version``, or None when the job has
    none (a job saved before #1934, or one with no routines)."""
    from src.shared.contract_versioning import load_contract

    key = routine_migration_path(database_name, job_id, assignment_version)
    if store.exists(key) is not True:
        return None
    return load_contract(RoutineMigrationContract, store.read_json(key), artifact=key)


def latest_routine_migration(
    store: Any, database_name: str, job_id: str, assignment_version: int
) -> tuple[int, RoutineMigrationContract] | None:
    """The artifact of the newest assignment version at or below ``assignment_version``
    that has one, with the version it came from; None when none has. A version written
    where the phase does not run (a customer override through the API or
    ``scripts/run_assignment.py``, the ``assess()`` library, or an intermediate reroute
    version a rollback replaced; the final reroute version, Bedrock's included, has its
    own) reads its nearest earlier one: routine statuses do not depend on the assignment, only
    ``tables_leaving_aurora`` does, so the caller says which version it used."""
    for version in range(assignment_version, 0, -1):
        artifact = read_routine_migration(store, database_name, job_id, version)
        if artifact is not None:
            return version, artifact
    return None
