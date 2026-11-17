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
- ``rebuild_differs`` (#2384): a model-repaired routine whose repair is not the one the
  test ran, or whose exact text, rebuilt here from the routines file and the repair, no
  longer passes the repair screen or the routine checks.

The gate reads the routine-migration artifact's ``proof`` entries and its ``proof_run``,
the routines file's digest (``routine_sources_digest``) and, for the routines the repair
loop carried (``model_repaired`` in ``routine_repair.json``, #2384), their candidates. A
repaired routine is deployed only when the current test ran that same repair (its
``repair_sha256``) and found no difference; step 1 never trusts the saved state alone, so
:func:`deploy_gate` rebuilds each such repair in this process (``rebuild_repairs``: the
rebuild, the screen with ``create_refusal`` and every routine check again) and hands the
scripts the rebuilt text, in memory only: no artifact, log or printed line gets it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.contracts.routine_migration import RoutineMigrationContract
from src.shared.routine_facts import (
    HELD_REASONS,
    REBUILD_DIFFERS,
    TEST_COMMAND,
    proof_is_current,
    repaired_routines,
    routine_holds,
    with_repairs,
)

__all__ = [
    "HELD_REASONS",
    "REBUILD_DIFFERS",
    "HELD_WHY",
    "TEST_COMMAND",
    "DeployGate",
    "deploy_gate",
    "held_routines",
    "proof_state",
]

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
    REBUILD_DIFFERS: (
        "a model repaired it, and the repair it has now is not the one the test on a test "
        "Aurora ran, or its text rebuilt from the routines file no longer passes its checks: "
        f"test the routines again with {TEST_COMMAND}, then generate the routine scripts again"
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


Ref = tuple[str, str]


@dataclass
class DeployGate:
    """What the deploy gate decided for one routine-migration artifact: the test's state
    (:func:`proof_state`), each routine it holds back with its reason, and the rebuilt exact
    text of each model-repaired routine step 1 deploys (memory only, never written but into
    the scripts)."""

    proof: str
    held: dict[Ref, str]
    repaired: set[Ref] = field(default_factory=set)
    texts: dict[Ref, str] = field(default_factory=dict, repr=False)
    rows: list[dict[str, Any]] = field(default_factory=list, repr=False)
    forced: dict[Ref, str] = field(default_factory=dict)

    def held_with(self, forced: dict[Ref, str]) -> dict[Ref, str]:
        """``held`` with more routines held back whatever the test says (a step 1 writer's
        contract row that disagrees with the gate on a repair, #2384), and every routine
        that calls one of them as ``depends_on_held``: the one rule, ``routine_holds``."""
        if not forced:
            return self.held
        return routine_holds(self.rows, self.proof == "current", {**self.forced, **forced})


def deploy_gate(
    store: Any, db: str, job_id: str, migration: RoutineMigrationContract
) -> DeployGate:
    """The deploy gate for ``migration`` (module docstring). A carried routine not in
    ``held`` is deployed; a model-repaired one with its text in ``texts``."""
    from src.storage.routine_repair_store import read_routine_repair
    from src.tools.routines.repair_proof import rebuild_repairs

    proof = proof_state(store, db, job_id, migration)
    current = proof == "current"
    repair = read_routine_repair(store, db, job_id, migration.assignment_version)
    digests = repaired_routines(repair, migration.assignment_version)
    rows = with_repairs([row.model_dump(mode="json") for row in migration.routines], digests)
    repaired = {
        (str(r["kind"]), str(r["routine_id"])) for r in rows if r.get("status") == "model_repaired"
    }
    held = routine_holds(rows, current)
    candidates: dict[Ref, str] = {
        (row.kind, row.routine_id): str(row.repaired_create)
        for row in (repair.routines if repair is not None else [])
        if (row.kind, row.routine_id) in repaired - held.keys()
    }
    texts, failed = rebuild_repairs(store, db, job_id, migration, candidates)
    forced = dict.fromkeys(failed, REBUILD_DIFFERS)
    if forced:
        # A repair that no longer rebuilds is held back, and what calls it with it.
        held = routine_holds(rows, current, forced)
    return DeployGate(
        proof=proof,
        held=held,
        repaired=repaired,
        texts={ref: text for ref, text in texts.items() if ref not in held},
        rows=rows,
        forced=forced,
    )


def held_routines(
    store: Any, db: str, job_id: str, migration: RoutineMigrationContract
) -> dict[tuple[str, str], str]:
    """Each routine of ``migration`` the deploy gate holds back, by ``(kind, routine_id)``,
    with its reason (:data:`HELD_REASONS`); a routine not listed was proved by the current
    test (a model-repaired one on its own repair, rebuilt again here)."""
    return deploy_gate(store, db, job_id, migration).held
