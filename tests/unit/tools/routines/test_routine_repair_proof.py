# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The repair loop's checks on the rebuilt exact text (#1941, ``repair_proof``).

The screen and the routine checks run again on the text with its literals put back,
never on the redacted candidate the model wrote. These tests call the verifier directly,
with synthetic routines, so each check is seen on its own.
"""

from __future__ import annotations

from typing import Any

from src.contracts.routine_sources import RoutineSource
from src.tools.routines.migration import run_routine_migration_deterministic
from src.tools.routines.repair_proof import RepairItem, RepairVerifier

ROLE_EXACT = (
    "CREATE OR REPLACE FUNCTION public.as_admin(p integer)\n RETURNS integer\n"
    " LANGUAGE plpgsql\nAS $function$BEGIN RAISE NOTICE 'see search_path docs'; RETURN p; END$function$\n"
)


def _job(exact: str) -> tuple[Any, dict[str, Any], list[RoutineSource]]:
    collector = {
        "metadata": {"source_database": {"engine": "postgresql", "version": "16.4"}},
        "database_schema": {
            "tables": [],
            "procedures": [
                {
                    "procedure_id": "as_admin", "procedure_name": "as_admin",
                    "schema_name": "public", "procedure_type": "FUNCTION",
                    "specific_name": "as_admin_1", "language": "plpgsql", "owner": "app",
                    "security_definer": False, "body_status": "collected",
                    "definition": exact, "depends_on": [],
                }  # fmt: skip
            ],
        },
    }
    sources = [
        RoutineSource(kind="function", routine_id="as_admin", schema_name="public",
                      specific_name="as_admin_1", create_statement=exact,
                      origin="pg_get_functiondef")  # fmt: skip
    ]
    built = run_routine_migration_deterministic(
        collector, None, sources, job_id="j", database_name="d", assignment_version=1
    )
    return built, collector, sources


def test_a_restored_literal_the_proofs_screen_refuses_is_caught_on_the_exact_text() -> None:
    """The candidate the model saw reads RAISE NOTICE '?': nothing for the screen to
    refuse, and nothing that stops the rebuild. With the literal back, the text names
    search_path, which the proof's screen reads in every string, and the screen run again
    on the rebuilt text refuses it."""
    migration, collector, sources = _job(ROLE_EXACT)
    (row,) = migration.routines
    seen = ROLE_EXACT.replace("'see search_path docs'", "'?'")
    candidate = seen.replace("RETURN p;", "RETURN p + 0;")
    verifier = RepairVerifier(
        migration, collector, lambda: sources, refs=[(row.kind, row.routine_id)]
    )
    assert verifier.blocker(row.kind, row.routine_id, seen) is None
    (check,) = verifier.verify([RepairItem(row.kind, row.routine_id, seen, candidate)]).values()
    assert (check.stage, check.outcome, check.code) == ("rescreen", "failed", "screen_refused")
    assert "docs" not in (check.error or "")


def test_a_routine_with_no_exact_text_is_never_rebuilt() -> None:
    migration, collector, _ = _job(ROLE_EXACT)
    (row,) = migration.routines
    verifier = RepairVerifier(migration, collector, lambda: [], refs=[(row.kind, row.routine_id)])
    assert verifier.blocker(row.kind, row.routine_id, ROLE_EXACT)[0] == "no_exact_text"
    (check,) = verifier.verify(
        [RepairItem(row.kind, row.routine_id, ROLE_EXACT, ROLE_EXACT)]
    ).values()
    assert (check.stage, check.code) == ("rebuild", "no_exact_text")
