# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1989: a hot-partition entry's utilization is never capped at 100%.

An entry is the table or GSI total against one partition key value's limit; above 100%
the figure says how far the total is over that limit, which the per-key estimate then
qualifies. The contract, the cost check and the group merge all keep the real ratio.
"""

from __future__ import annotations

from src.agents.schema_design.dynamodb_merge import aggregate_hot_partitions
from src.contracts.dynamodb_model_output import HotPartitionEntry
from src.tools.schema.dynamodb_cost_check import computed_entry


def _entry(units: float, **extra: object) -> dict:
    return {
        "table_name": "Options",
        "operation": "read",
        "rcu_or_wcu_per_second": units,
        "contributing_patterns": ["q1"],
        "mitigation": "Reads spread over the option names",
        **extra,
    }


def test_the_contract_accepts_a_total_above_one_key_values_limit() -> None:
    entry = HotPartitionEntry.model_validate(
        _entry(9_087.95, partition_limit=3000, utilization_pct=302.9, at_risk=True)
    )
    assert entry.utilization_pct == 302.9


def test_the_cost_check_computes_the_real_ratio() -> None:
    computed = computed_entry(_entry(9_087.95))
    assert computed["utilization_pct"] == 302.9
    assert computed["at_risk"] is True


def test_the_merge_sums_two_groups_without_a_cap() -> None:
    a = _entry(2_400.0, partition_limit=3000, utilization_pct=80.0, at_risk=False)
    b = _entry(2_400.0, partition_limit=3000, utilization_pct=80.0, at_risk=False)
    (merged,) = aggregate_hot_partitions([a, b], {"Options"})
    assert merged["utilization_pct"] == 160.0
    assert merged["at_risk"] is True
