# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A triggers-enable check result saved before #2113 still loads.

#1948 wrote the result at ``data-movement/step-1/cutover-check.json`` as
``CutoverCheckResult``; #2113 renamed the file (``triggers-enable-check.json``) and the model
(``TriggersEnableCheckResult``) without changing its shape. The old file, as #1948's writer
saved it, reads through the store and validates as the renamed model (AGENTS.md, "Before
asking for review", item 6).
"""

from __future__ import annotations

import json

from src.contracts.triggers_enable_check import TriggersEnableCheckResult
from src.shared.contract_versioning import contract_for_artifact
from src.storage.local_store import LocalArtifactStore

OLD_KEY = "shop/job1/data-movement/step-1/cutover-check.json"  # legacy alias (#2113)
NEW_KEY = "shop/job1/data-movement/step-1/triggers-enable-check.json"

# Exactly what #1948's ``run_dms_step.py`` wrote (contract 1.0): a MySQL job with one
# trigger confirmed, one event missing, one trigger not carried and the scheduler OFF.
OLD_FILE = {
    "contract_version": "1.0",
    "job_id": "job1",
    "database_name": "shop",
    "step": 1,
    "target_engine": "aurora_mysql",
    "contract_sha256": "a" * 64,
    "checked_at": "2026-11-10T12:00:00Z",
    "status": "fail",
    "expected": 2,
    "confirmed": 1,
    "rows_read": 1,
    "mismatches": [{"routine_id": "shop.nightly", "kind": "event", "problem": "missing"}],
    "not_carried": [
        {"routine_id": "shop.orders_au", "kind": "trigger", "reasons": ["depends_on_not_carried"]}
    ],
    "left_out": [
        {"routine_id": "shop.orders_bu", "kind": "trigger", "reason": "trigger_order_unknown"}
    ],
    "events_scheduled": 1,
    "event_scheduler": "OFF",
}


def test_the_old_and_the_new_path_hold_the_renamed_model() -> None:
    assert contract_for_artifact(OLD_KEY) is TriggersEnableCheckResult
    assert contract_for_artifact(NEW_KEY) is TriggersEnableCheckResult


def test_an_old_cutover_check_json_loads_as_the_renamed_model(tmp_path) -> None:
    path = tmp_path / OLD_KEY
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(OLD_FILE))
    loaded = LocalArtifactStore(base_dir=str(tmp_path)).read_json(OLD_KEY)
    result = TriggersEnableCheckResult.model_validate(loaded)
    assert (result.status, result.expected, result.confirmed) == ("fail", 2, 1)
    assert [m.routine_id for m in result.mismatches] == ["shop.nightly"]
    assert [m.reason for m in result.left_out] == ["trigger_order_unknown"]
    assert result.scheduler_off
    assert result.model_dump(mode="json")["mismatches"] == OLD_FILE["mismatches"]
