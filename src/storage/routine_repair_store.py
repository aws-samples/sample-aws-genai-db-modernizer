# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Where the routine repair seam's files live, and the result's reader (#1940).

``<db>/<job>/routines/v<N>/routine_repair.json`` is written by
``src.agents.routine_repair.handler``; the model's request and answer sit beside it under
``repair/``. The reader lives here, apart from the handler, so synthesis can read the
result without reaching the routine checks (ADR-034 Decision 8).
"""

from __future__ import annotations

from typing import Any

from src.contracts.routine_repair import RoutineRepairContract


def routine_repair_path(database_name: str, job_id: str, version: int) -> str:
    """The result's key: ``<db>/<job>/routines/v<N>/routine_repair.json``."""
    return f"{database_name}/{job_id}/routines/v{version}/routine_repair.json"


def routine_repair_request_path(database_name: str, job_id: str, version: int) -> str:
    """The external session's request: ``routines/v<N>/repair/llm_input.json``."""
    return f"{database_name}/{job_id}/routines/v{version}/repair/llm_input.json"


def routine_repair_response_path(database_name: str, job_id: str, version: int) -> str:
    """The external session's answer: ``routines/v<N>/repair/llm_response.json``."""
    return f"{database_name}/{job_id}/routines/v{version}/repair/llm_response.json"


def routine_repair_state_path(database_name: str, job_id: str, version: int) -> str:
    """The repair loop's progress between external rounds (#1941):
    ``routines/v<N>/repair/loop_state.json`` (ids, attempts and words, never routine text)."""
    return f"{database_name}/{job_id}/routines/v{version}/repair/loop_state.json"


def read_routine_repair(
    store: Any, database_name: str, job_id: str, version: int
) -> RoutineRepairContract | None:
    """The repair result for routine-migration version ``version``; None when none ran."""
    from src.shared.contract_versioning import load_contract

    key = routine_repair_path(database_name, job_id, version)
    if store.exists(key) is not True:
        return None
    return load_contract(RoutineRepairContract, store.read_json(key), artifact=key)
