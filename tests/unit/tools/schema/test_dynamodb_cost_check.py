"""DynamoDB cost / hot-partition check shared by Bedrock and external mode (issue #198).

The skill only lets ``validation_passed`` be true once
``compute_performances_and_costs`` ran successfully, but that check existed only
as a Strands tool inside the Bedrock agent. The computation now lives in a plain
function the tool wraps, and ``run_schema_design.py --check-costs`` runs the
same function on a group draft in external (Claude Code) mode.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from src.tools.schema.dynamodb_cost_check import (
    check_draft_costs,
    compute_performances_and_costs_entries,
)

OK_READ = {
    "table_name": "Options",
    "operation": "read",
    "rcu_or_wcu_per_second": 300.0,
    "partition_limit": 3000,
    "utilization_pct": 10.0,
    "at_risk": False,
    "contributing_patterns": ["q1"],
}
HOT_MITIGATED = {
    "table_name": "Options",
    "gsi_name": "ByAutoload",
    "operation": "write",
    "rcu_or_wcu_per_second": 900.0,
    "partition_limit": 1000,
    "utilization_pct": 90.0,
    "at_risk": True,
    "contributing_patterns": ["q2"],
    "mitigation": "write sharding on autoload with 4 suffixes",
}
HOT_UNMITIGATED = {**HOT_MITIGATED, "mitigation": None}


def test_plain_function_matches_the_strands_tool() -> None:
    from src.tools.schema.dynamodb_schema_agent import compute_performances_and_costs

    entries = [OK_READ, HOT_MITIGATED]
    assert compute_performances_and_costs(json.dumps(entries)) == (
        compute_performances_and_costs_entries(entries)
    )


def test_tool_and_plain_function_reject_the_same_entries() -> None:
    from src.tools.schema.dynamodb_schema_agent import compute_performances_and_costs

    with pytest.raises(ValidationError):
        compute_performances_and_costs(json.dumps([HOT_UNMITIGATED]))
    with pytest.raises(ValidationError):
        compute_performances_and_costs_entries([HOT_UNMITIGATED])


def test_check_draft_passes_and_reports_at_risk_findings() -> None:
    report = check_draft_costs({"hot_partition_analysis": [OK_READ, HOT_MITIGATED]})

    assert report["passed"] is True
    assert report["errors"] == []
    assert report["results"] == compute_performances_and_costs_entries([OK_READ, HOT_MITIGATED])
    assert report["hot_partition_findings"] == [
        {
            "table_name": "Options",
            "gsi_name": "ByAutoload",
            "operation": "write",
            "utilization_pct": 90.0,
            "mitigation": "write sharding on autoload with 4 suffixes",
            "contributing_patterns": ["q2"],
        }
    ]
    assert report["per_table"] == [
        {
            "table_name": "Options",
            "gsi_name": None,
            "operation": "read",
            "rcu_or_wcu_per_second": 300.0,
            "partition_limit": 3000.0,
            "utilization_pct": 10.0,
            "at_risk": False,
        },
        {
            "table_name": "Options",
            "gsi_name": "ByAutoload",
            "operation": "write",
            "rcu_or_wcu_per_second": 900.0,
            "partition_limit": 1000.0,
            "utilization_pct": 90.0,
            "at_risk": True,
        },
    ]


def test_check_draft_fails_on_the_entry_the_tool_rejects() -> None:
    report = check_draft_costs({"hot_partition_analysis": [OK_READ, HOT_UNMITIGATED]})

    assert report["passed"] is False
    assert len(report["errors"]) == 1
    assert report["errors"][0]["index"] == 1
    assert "mitigation is required" in report["errors"][0]["error"]
    # The valid entry is still reported.
    assert report["results"] == compute_performances_and_costs_entries([OK_READ])


@pytest.mark.parametrize("draft", [{}, {"hot_partition_analysis": "nope"}])
def test_check_draft_fails_without_a_hot_partition_list(draft: dict) -> None:
    report = check_draft_costs(draft)

    assert report["passed"] is False
    assert report["errors"][0]["index"] is None
