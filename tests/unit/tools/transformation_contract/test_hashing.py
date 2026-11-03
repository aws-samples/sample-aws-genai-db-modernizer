# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The contract hash a later step pins (#1762)."""

from __future__ import annotations

import json

from src.contracts.transformation_contract import StepContract
from src.tools.transformation_contract.hashing import canonical_json, contract_sha256

_C = {"job_id": "j1", "assignment_version": 2, "step": 1, "target": {"engine": "aurora_mysql"},
      "status": "valid", "rules": [{"input_schema": "collector", "source_table": "wp.posts",
      "target_table": "posts", "primary_key": ["ID"], "fields": [{"source_column": "ID",
      "target_column": "ID", "target_type": "BIGINT", "op": {"op": "copy",
      "column": {"table_id": "wp.posts", "column": "ID"}}}]}]}  # fmt: skip


def test_the_hash_ignores_key_order_and_whitespace():
    a = StepContract.model_validate(_C)
    b = StepContract.model_validate(dict(reversed(list(_C.items()))))
    assert contract_sha256(a) == contract_sha256(b)
    assert len(contract_sha256(a)) == 64 and contract_sha256(a) == contract_sha256(a).lower()
    assert b" " not in canonical_json(a)
    assert canonical_json(a) == json.dumps(
        json.loads(canonical_json(a)), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def test_the_hash_changes_with_any_rule():
    a = StepContract.model_validate(_C)
    changed = dict(_C, rules=[dict(_C["rules"][0], target_table="posts2")])
    assert contract_sha256(a) != contract_sha256(StepContract.model_validate(changed))
