# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Files written before #802 still load (ADR-008 Decision 5).

DynamoDB schema output 1.4 adds ``unresolved_merge_conflicts``, whose entries require a
``status``; synthesis output 1.72 adds ``facts.dynamodb_merge_conflicts``. A 1.3 schema
output with merge failures (failure strings only) and a report written before 1.72 validate unchanged,
and the reader lists the old failures as not recorded, never as served.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.agents.schema_design.dynamodb_merge import (
    NOT_RECORDED,
    merge_failures,
    unresolved_merge_conflicts,
)
from src.agents.schema_design.group_merger import merge_group_drafts
from src.contracts.dynamodb_model_output import DynamoDBModelOutputContract
from src.contracts.synthesis_output import SynthesisOutputContract
from src.report.facts import report_facts
from src.report.waves import resolve_migration_waves
from src.shared.adoption_facts import adoption_facts, slice_plan_facts
from src.shared.cutover_message import CUTOVER_MESSAGE
from src.shared.decision_headline import decision_headline
from src.shared.exposure_wording import group_exposures
from src.shared.report_facts import (
    engine_query_count_facts,
    query_group_facts,
    trade_off_topic_facts,
    with_gate_conditions,
)
from tests.unit.agents.schema_design.valid_designs import ddb_design, ddb_pattern, ddb_table
from tests.unit.report.cache_role import as_read_back

OLD_REPORT = (
    Path(__file__).resolve().parents[1]
    / "fixtures/old_jobs/v0.4.0-beta.2/wordpress/aa4e840a/synthesis/v2/report.json"
)

SAID = "cutover_responsibility"


def _design_1_3() -> dict:
    """A merged design with one merge failure, as a 1.3 writer saved it."""
    users = ddb_design(
        [ddb_table("Main", "users", pk="user_id")],
        [ddb_pattern("DDB-AP-1", "Main", "users", "q-users")],
    )
    orders = ddb_design(
        [ddb_table("Main", "orders", pk="order_id")],
        [ddb_pattern("DDB-AP-1", "Main", "orders", "q-order")],
    )
    merged = merge_group_drafts([users, orders], "dynamodb")
    del merged["unresolved_merge_conflicts"]
    return {**json.loads(json.dumps(merged)), "contract_version": "1.3"}


def test_a_1_3_schema_output_with_merge_failures_validates() -> None:
    design = _design_1_3()

    parsed = DynamoDBModelOutputContract.model_validate(design)

    assert parsed.unresolved_merge_conflicts == []
    assert parsed.validation_passed is False


def test_its_failures_read_as_not_recorded() -> None:
    design = _design_1_3()

    conflicts = unresolved_merge_conflicts(design)

    assert [c["failure"] for c in conflicts] == merge_failures(design)
    assert {c["status"] for c in conflicts} == {NOT_RECORDED}


def test_status_is_required_only_inside_a_new_entry() -> None:
    design = _design_1_3()
    design["unresolved_merge_conflicts"] = [{"failure": merge_failures(design)[0]}]

    with pytest.raises(ValidationError, match="status"):
        DynamoDBModelOutputContract.model_validate(design)


def test_a_report_written_before_1_72_validates_and_reads_back_unchanged() -> None:
    """The v0.4.0-beta.2 job's report (synthesis output 1.69)."""
    old = json.loads(OLD_REPORT.read_text(encoding="utf-8"))
    stored = copy.deepcopy(old["facts"])

    SynthesisOutputContract.model_validate(old)

    assert "dynamodb_merge_conflicts" not in stored
    # Unchanged, but for the trade-off topic groups a block written before #1221 gains.
    facts = report_facts(old)
    assert "trade_off_topics" not in stored
    assert facts["trade_off_topics"] == trade_off_topic_facts(
        old, stored["reality_check_trade_offs"]
    )
    # #2107: and the cutover sentence, which a block written before it reads back with.
    assert facts["cutover_responsibility"] == CUTOVER_MESSAGE
    # #1128: and the slice plan and adoption paths, filled at read time (ADR-032 Decision 4).
    assert facts["slice_plan"] == (plan := slice_plan_facts(old))
    assert facts["adoption"] == adoption_facts(old, plan)
    # #2358: and each engine's final query count, from the ranking.
    assert facts["engine_query_counts"] == engine_query_count_facts(old, facts["engines"])
    assert facts["engine_query_counts"]
    # #2324: and the executive summary's exposures, grouped and counted.
    brief = stored["executive_brief"]
    # #2315: and its lead line, from the same function.
    assert facts["executive_brief"] == {
        **brief,
        "exposures": group_exposures(brief["exposures"]),
        "decision_headline": decision_headline(brief, old.get("database_name")),
    }
    # #2325: this report's source queries name no tables, so it has no per-table query
    # rows to fill and its query group counts read back as stored.
    assert "table_queries" not in stored["query_group_counts"]
    assert (query_group_facts(old) or {})["table_queries"] == []
    filled = (
        "trade_off_topics",
        SAID,
        "slice_plan",
        "adoption",
        "engine_query_counts",
        "target_engines",
        "executive_brief",
        # #2484: the objects each Aurora design leaves out (none here), from its notes.
        "design_left_out",
        # #2289: each Aurora design's changes from the source (none compared here).
        "design_changes",
        # #2287: each wave gate's conditions, from the same function.
        "waves",
    )
    assert facts["waves"] == with_gate_conditions(
        as_read_back(stored)["waves"], resolve_migration_waves(old)
    )
    assert all(w["gate_conditions"] for w in facts["waves"])
    assert facts["design_left_out"] == []
    assert facts["design_changes"] == []
    assert {k: v for k, v in facts.items() if k not in filled} == {
        k: v for k, v in as_read_back(stored).items() if k not in filled
    }
