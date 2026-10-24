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


# ---------------------------------------------------------------------------
# KEYS_ONLY GSI: the base-table fetch per returned item (#1503)
# ---------------------------------------------------------------------------


def _gsi_draft(projection: str, base_units: float, *, item_bytes: int = 1000, **pattern) -> dict:
    """One table with a GSI of ``projection`` and a read pattern on that GSI."""
    gsi: dict = {"gsi_name": "ByTag", "projection": projection}
    if projection == "INCLUDE":
        gsi["projected_attributes"] = ["title"]
    ap = {
        "pattern_id": "DDB-AP-1",
        "operation": "Query",
        "table_name": "Posts",
        "gsi_name": "ByTag",
        "design_rps": 10.0,
        "avg_items_returned": 20,
        "item_size_bytes": 100,
        "strongly_consistent": False,
        "in_scope": True,
        **pattern,
    }
    entry = {**OK_READ, "table_name": "Posts", "rcu_or_wcu_per_second": base_units}
    entry["utilization_pct"] = round(base_units / 30, 1)
    return {
        "table_definitions": [
            {"table_name": "Posts", "item_size_bytes": item_bytes, "gsis": [gsi]}
        ],
        "access_patterns": [ap],
        "hot_partition_analysis": [entry],
    }


def test_keys_only_fetch_is_priced_per_returned_item() -> None:
    # 10 rps x 20 items = 200 GetItem/s; 1000 B rounds to one 4 KB unit, 0.5 eventually consistent.
    report = check_draft_costs(_gsi_draft("KEYS_ONLY", base_units=100.0))

    assert report["passed"] is True
    assert report["keys_only_fetch"] == [
        {
            "table_name": "Posts",
            "gsi_name": "ByTag",
            "pattern_ids": ["DDB-AP-1"],
            "extra_reads_per_second": 200.0,
            "extra_read_units_per_second": 100.0,
        }
    ]


def test_base_read_entry_that_leaves_the_fetch_out_fails() -> None:
    report = check_draft_costs(_gsi_draft("KEYS_ONLY", base_units=99.0))

    assert report["passed"] is False
    assert any("KEYS_ONLY" in e["error"] and "100" in e["error"] for e in report["errors"])


def test_fetch_rounds_each_item_up_to_4_kb_and_doubles_when_strongly_consistent() -> None:
    # 5000 B -> 2 units of 4 KB; strongly consistent -> 1 per unit: 200 x 2 x 1 = 400.
    draft = _gsi_draft("KEYS_ONLY", base_units=399.0, item_bytes=5000, strongly_consistent=True)
    assert check_draft_costs(draft)["passed"] is False
    draft = _gsi_draft("KEYS_ONLY", base_units=400.0, item_bytes=5000, strongly_consistent=True)
    report = check_draft_costs(draft)
    assert report["passed"] is True
    assert report["keys_only_fetch"][0]["extra_read_units_per_second"] == 400.0


def test_missing_base_read_entry_fails() -> None:
    draft = _gsi_draft("KEYS_ONLY", base_units=500.0)
    draft["hot_partition_analysis"][0]["gsi_name"] = "ByTag"  # no base-table read entry
    assert check_draft_costs(draft)["passed"] is False


@pytest.mark.parametrize("projection", ["INCLUDE", "ALL"])
def test_include_and_all_projections_add_no_fetch(projection: str) -> None:
    report = check_draft_costs(_gsi_draft(projection, base_units=1.0))

    assert report["passed"] is True
    assert report["keys_only_fetch"] == []


def test_writes_and_base_table_reads_add_no_fetch() -> None:
    draft = _gsi_draft("KEYS_ONLY", base_units=1.0, operation="PutItem", avg_items_returned=None)
    assert check_draft_costs(draft)["keys_only_fetch"] == []
    draft = _gsi_draft("KEYS_ONLY", base_units=1.0, gsi_name=None)
    assert check_draft_costs(draft)["keys_only_fetch"] == []
    draft = _gsi_draft("KEYS_ONLY", base_units=1.0, in_scope=False)
    assert check_draft_costs(draft)["keys_only_fetch"] == []


def test_null_avg_items_counts_one_item_per_call() -> None:
    draft = _gsi_draft("KEYS_ONLY", base_units=5.0, avg_items_returned=None)  # 10 x 1 x 0.5
    report = check_draft_costs(draft)
    assert report["passed"] is True
    assert report["keys_only_fetch"][0]["extra_reads_per_second"] == 10.0


def test_tool_applies_the_same_check_when_given_the_design() -> None:
    from src.tools.schema.dynamodb_schema_agent import compute_performances_and_costs

    draft = _gsi_draft("KEYS_ONLY", base_units=99.0)
    entries = draft["hot_partition_analysis"]
    args = {
        "access_patterns_json": json.dumps(draft["access_patterns"]),
        "table_definitions_json": json.dumps(draft["table_definitions"]),
    }
    with pytest.raises(ValueError, match="KEYS_ONLY"):
        compute_performances_and_costs(json.dumps(entries), **args)
    entries[0]["rcu_or_wcu_per_second"] = 100.0
    assert (
        compute_performances_and_costs(json.dumps(entries), **args)[0]["rcu_or_wcu_per_second"]
        == 100.0
    )
