# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's deploy gate on the routine test (#2411, ADR-034 Decision 5 as amended).

The maintainer decision of 2026-11-17 on #1925: results from the checks alone are not a
valid basis for deploying routines. ``routines-deploy.sql`` and the triggers-enable script
carry a routine only when the latest test on a test Aurora for the job (``/test-routines``:
the throwaway test cluster, or the scratch-schema test, #1945) found no difference on the
calls made, outcome ``proved``, and that test ran on the job's routines file as it is now.
Every other routine is held back, by id, with one of :data:`HELD_REASONS`:

- ``not_tested``: no test ran on this routines file (none ran, the artifact predates the
  test or its digest, the routines file changed since, or the test could not run it);
- ``not_proved``: a call failed on the test cluster, or it could not be run or compared;
- ``needs_attention``: it did not load, the strict check found an error, a call behaved
  differently, or a call left the role it runs as.
- ``depends_on_held``: the test found no difference on it, but it calls a held routine,
  directly or not (``routine_holds``, the same rule ``facts.routines`` counts).

The gate reads the routine-migration artifact's ``proof`` entries and its ``proof_run``,
and the routines file's digest (``routine_sources_digest``); nothing else.
"""

from __future__ import annotations

from typing import Any

from src.contracts.routine_migration import RoutineMigrationContract
from src.shared.routine_facts import HELD_REASONS, TEST_COMMAND, proof_is_current, routine_holds

__all__ = ["HELD_REASONS", "HELD_WHY", "TEST_COMMAND", "held_routines", "proof_state"]

#: What each held-back reason means and what to do, for the command output and the files.
HELD_WHY: dict[str, str] = {
    "not_tested": (
        "no test on a test Aurora has run on this routines file for it: test the routines "
        f"with {TEST_COMMAND}, then generate the routine scripts again"
    ),
    "not_proved": (
        "the test on a test Aurora could not show it has no difference on the calls made "
        f"(a call failed there, or it could not be run or compared): {TEST_COMMAND} names "
        "why; settle it and test again"
    ),
    "needs_attention": (
        "the test on a test Aurora found a difference or an error (it did not load, the "
        f"strict check failed, or a call behaved differently): {TEST_COMMAND} names why; fix "
        "it and test again"
    ),
    "depends_on_held": (
        "it calls a routine that is held back, so its CREATE could fail without it: settle "
        f"that routine with {TEST_COMMAND}, then generate the routine scripts again"
    ),
}


def proof_state(store: Any, db: str, job_id: str, migration: RoutineMigrationContract) -> str:
    """``current`` when the artifact's test ran on the job's routines file as it is now,
    ``stale`` when one ran on another (or before the digest), ``not_run`` when none ran."""
    from src.agents.collector.routine_sources import routine_sources_digest

    if migration.proof_run is None:
        return "not_run"
    digest = routine_sources_digest(store, db, job_id)
    return "current" if proof_is_current(migration.proof_run, digest) else "stale"


def held_routines(
    store: Any, db: str, job_id: str, migration: RoutineMigrationContract
) -> dict[tuple[str, str], str]:
    """Each routine of ``migration`` the deploy gate holds back, by ``(kind, routine_id)``,
    with its reason (:data:`HELD_REASONS`); a routine not listed was proved by the current
    test."""
    current = proof_state(store, db, job_id, migration) == "current"
    rows = [row.model_dump(mode="json") for row in migration.routines]
    return routine_holds(rows, current)
