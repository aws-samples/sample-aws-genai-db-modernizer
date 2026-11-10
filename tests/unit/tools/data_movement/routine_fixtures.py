# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Routine-migration artifacts and routine sources for step 1's routine tests (#1947)."""

from __future__ import annotations

from typing import Any

from src.contracts.routine_migration import (
    RoutineDependencyEntry,
    RoutineMigration,
    RoutineMigrationContract,
    RoutineRef,
    RoutineRewriteEntry,
)
from src.contracts.routine_sources import RoutineSession, RoutineSource
from src.contracts.transformation_contract import StepContract, StepRoutine, StepRoutines
from src.tools.data_movement.step1_routines import build_step_routines
from tests.unit.tools.data_movement.dms_step_fixtures import make_contract

#: A literal only the routine sources and the deploy script may ever hold.
SENTINEL = "s3cr3t-1947"
TARGETS = {"postgresql": "aurora_postgresql", "mysql": "aurora_mysql"}


def dep_table(name: str) -> RoutineDependencyEntry:
    return RoutineDependencyEntry(kind="table", name=name, found=True)


def dep_routine(kind: str, routine_id: str) -> RoutineDependencyEntry:
    return RoutineDependencyEntry.model_validate({
        "kind": "routine", "name": routine_id.split("(")[0], "routine_kind": kind, "found": True,
        "routines": [{"kind": kind, "routine_id": routine_id}],
    })  # fmt: skip


def row(
    routine_id: str,
    kind: str = "function",
    *,
    schema: str = "public",
    name: str | None = None,
    specific: str | None = None,
    status: str = "as_is",
    reasons: list[str] | None = None,
    dependencies: list[RoutineDependencyEntry] | None = None,
    table: str | None = None,
    signature: str | None = None,
    returns: str | None = None,
    cycle: int | None = None,
) -> dict[str, Any]:
    own = name or routine_id.split("(")[0].rpartition(".")[2]
    return {
        "routine_id": routine_id, "kind": kind, "schema_name": schema, "name": own,
        "specific_name": specific or own, "table": table, "signature": signature,
        "returns": returns, "source_text": "exact", "body_status": "collected",
        "status": status, "reasons": reasons or ([] if status != "needs_attention" else ["x"]),
        "deploy_order": 1, "cycle": cycle, "dependencies": dependencies or [],
        "dependencies_complete": True,
        "rewrites": [RoutineRewriteEntry(rule_id="mysql-routine-definer-to-target-user",
                                         rewrite="definer")] if status == "rewritten" else [],
        "rules_passed": 1,
    }  # fmt: skip


def migration(
    rows: list[dict[str, Any]],
    engine: str = "postgresql",
    cycles: list[list[RoutineRef]] | None = None,
) -> RoutineMigrationContract:
    """An artifact with ``rows`` in the order given (their deploy order)."""
    built = [
        RoutineMigration.model_validate({**r, "deploy_order": i + 1}) for i, r in enumerate(rows)
    ]
    counts = {s: 0 for s in ("as_is", "rewritten", "model_repaired", "needs_attention")}
    for r in built:
        counts[r.status] += 1
    pg = engine == "postgresql"
    return RoutineMigrationContract.model_validate({
        "job_id": "job1", "database_name": "shop", "assignment_version": 2,
        "source_engine": engine, "source_version": "16.2" if pg else "8.0.36",
        "target_engine": TARGETS[engine], "target_line": "16" if pg else "8.0",
        "target_user": "postgres" if pg else "admin", "routine_sources": True,
        "total": len(built), "counts": counts, "cycles": cycles or [], "routines": built,
    })  # fmt: skip


def contract_with_routines(
    artifact: RoutineMigrationContract,
    engine: str | None = None,
    *,
    triggers: dict[str, dict[str, Any]] | None = None,
) -> StepContract:
    """The step contract with the artifact's routines section; ``triggers`` sets, by
    routine id, what the collection gives a trigger (``source_state``, ``fires``,
    ``action_order``), as ``attach_step_routines`` reads it."""
    target = engine or artifact.target_engine
    contract = make_contract(target)
    section = build_step_routines(artifact, contract, version=2)
    if triggers:
        rows = [StepRoutine.model_validate({**r.model_dump(), **triggers.get(r.routine_id, {})})
                for r in section.routines]  # fmt: skip
        section = StepRoutines.model_validate({**section.model_dump(), "routines": rows})
    return contract.model_copy(update={"routines": section})


def pg_source(
    specific: str, text: str, kind: str = "function", schema: str = "public"
) -> RoutineSource:
    origin = "pg_get_triggerdef" if kind == "trigger" else "pg_get_functiondef"
    return RoutineSource.model_validate({
        "kind": kind, "routine_id": specific, "schema_name": schema, "specific_name": specific,
        "create_statement": text, "origin": origin,
    })  # fmt: skip


def mysql_source(
    name: str,
    text: str,
    kind: str = "procedure",
    *,
    sql_mode: str = "STRICT_TRANS_TABLES",
    collation: str | None = "utf8mb4_0900_ai_ci",
) -> RoutineSource:
    return RoutineSource.model_validate({
        "kind": kind, "routine_id": name, "schema_name": "shop", "specific_name": name,
        "create_statement": text, "origin": "show_create",
        "session": RoutineSession(sql_mode=sql_mode, character_set_client="utf8mb4",
                                  collation_connection=collation),
    })  # fmt: skip
