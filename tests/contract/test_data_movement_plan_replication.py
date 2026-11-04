# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""DataMovementPlan.replication (#1798): optional, so earlier plan files still load."""

from __future__ import annotations

import pytest

from src.contracts.data_movement_plan import DataMovementPlan, DmsReplication

_MIN = {"job_id": "j", "database_name": "d", "assignment_version": 2, "wave": 1, "wave_count": 1, "title": "t",
        "source": {"engine": "mysql", "database": "d"}, "cdc": {"tool": "dms"},
        "initial_load": {"strategy": "dms_full_load"}, "gates": {"wave_gate": "g", "max_lag_seconds": 5}}  # fmt: skip


def test_a_plan_without_replication_loads_with_none() -> None:
    assert DataMovementPlan.model_validate(_MIN).replication is None


def test_the_replication_section_round_trips() -> None:
    rep = {"target_engine": "aurora_mysql", "target_engine_line": "8.0", "lob_max_size_kb": 64,
           "sizing": {"row": "small", "instance_class": "dms.t3.medium", "allocated_storage_gb": 50,
                      "database_gb": 0.1, "writes_per_second": 21.3, "reason": "r"},
           "lob_tables": [{"table": "wp.wp_posts", "mode": "limited", "max_size_kb": 64, "columns": ["post_content"]}]}  # fmt: skip
    plan = DataMovementPlan.model_validate({**_MIN, "replication": rep})
    assert (
        isinstance(plan.replication, DmsReplication) and plan.replication.multi_az_default is True
    )
    assert plan.replication.unbounded_varchar.max_bytes is None
    assert DataMovementPlan.model_validate(plan.model_dump(mode="json")) == plan


def test_a_limited_lob_table_needs_its_size() -> None:
    rep = {"target_engine": None, "lob_max_size_kb": 32,
           "sizing": {"row": "small", "instance_class": "dms.t3.medium", "allocated_storage_gb": 50, "reason": "r"},
           "lob_tables": [{"table": "a.b", "mode": "limited", "columns": ["c"]}]}  # fmt: skip
    with pytest.raises(ValueError, match="max_size_kb"):
        DataMovementPlan.model_validate({**_MIN, "replication": rep})
