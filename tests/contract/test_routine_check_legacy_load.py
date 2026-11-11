# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A routine check result saved before #2113 still loads.

#1947 named a MySQL trigger or event already on the target, before the triggers-enable
script creates it, ``present_before_cutover``; #2113 writes ``present_before_triggers_enable``
(routine check 1.1). A 1.0 ``routines-check.json`` with the old code reads through the store
and validates, the code read as today's (AGENTS.md, "Before asking for review", item 6).
"""

from __future__ import annotations

import json

from src.contracts.routine_check import LEGACY_PROBLEMS, RoutineCheckResult
from src.storage.local_store import LocalArtifactStore

KEY = "shop/job1/data-movement/step-1/routines-check.json"
OLD_CODE = next(iter(LEGACY_PROBLEMS))

# What #1947's ``run_dms_step.py routines-check`` wrote (contract 1.0) for a MySQL job with
# one procedure confirmed and one deferred trigger already on the target.
OLD_FILE = {
    "contract_version": "1.0",
    "job_id": "job1",
    "database_name": "shop",
    "step": 1,
    "target_engine": "aurora_mysql",
    "contract_sha256": "a" * 64,
    "checked_at": "2026-11-09T12:00:00Z",
    "proof": "static",
    "label": "checked, not load-tested",
    "status": "fail",
    "carried": 1,
    "confirmed": 1,
    "deferred": 1,
    "rows_read": 2,
    "mismatches": [{"routine_id": "shop.orders_bi", "kind": "trigger", "problem": OLD_CODE}],
    "not_carried": [],
}


def test_an_old_routines_check_json_loads_with_todays_code(tmp_path) -> None:
    assert LEGACY_PROBLEMS == {OLD_CODE: "present_before_triggers_enable"}
    path = tmp_path / KEY
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(OLD_FILE))
    loaded = LocalArtifactStore(base_dir=str(tmp_path)).read_json(KEY)
    result = RoutineCheckResult.model_validate(loaded)
    assert [m.problem for m in result.mismatches] == ["present_before_triggers_enable"]
    assert OLD_CODE not in result.model_dump_json()
