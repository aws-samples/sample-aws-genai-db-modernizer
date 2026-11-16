# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's gate evidence as one block of facts (#1801, ADR-032, ADR-033 Decision 10).

``step_gate_facts`` words the gate file ``/move``'s read-only gate saved once: the result,
the time of the read, the full load, lag and validation lines, each table, the named
exceptions with their reasons, the findings, and the cutover message verbatim. The
deliverables attach it when they read the report (``with_step_gate``), so a gate read
after synthesis reaches them; a report whose step never ran its gate is left as it is.
"""

from __future__ import annotations

import copy
import json
import logging

import pytest

from src.contracts.synthesis_output import ReportFacts, StepGateFacts
from src.report.facts import report_facts, step_gate_of, with_step_gate
from src.shared.contract_versioning import ContractVersionError
from src.shared.cutover_message import CUTOVER_DISCLAIMER, REPLICATING_MESSAGE
from src.shared.report_facts import (
    STEP_GATE_FAIL_LINE,
    STEP_GATE_FINDINGS,
    STEP_GATE_PENDING_LINE,
    read_step_gate_facts,
    step_gate_facts,
    step_gate_key,
)
from src.storage.local_store import LocalArtifactStore
from src.tools.data_movement import dms_gate
from tests.unit.report import step_gate_fixtures as fx
from tests.unit.report.test_migration_waves_rendering import _legacy_report, _report_with_waves


def test_a_pass_says_only_the_replicating_message_and_the_cutover_message():
    facts = step_gate_facts(fx.passed())
    assert facts is not None
    assert facts["status"] == "pass" and facts["status_text"] == "Passed"
    assert facts["result_line"] == REPLICATING_MESSAGE == dms_gate.REPLICATING_MESSAGE
    assert facts["cutover_message"] == CUTOVER_DISCLAIMER
    assert facts["read_at"] == "2026-11-14T09:30:12Z"
    assert facts["read_at_text"] == "2026-11-14 09:30 UTC"
    assert facts["load_line"] == "Full load at 100%: 1,234,567 rows loaded across 3 tables."
    assert facts["lag_line"] == (
        "Highest replication lag in the 15 minutes before the read: 2 s at the source, "
        "3 s at the target; the gate accepts under 30 s."
    )
    assert facts["validation_line"] == (
        "2 of 2 tables with a key validated. 2 named exceptions, listed below."
    )
    assert (facts["table_count"], facts["keyed_table_count"], facts["validated_table_count"]) == (
        3, 2, 2
    )  # fmt: skip
    assert (facts["validating_table_count"], facts["failed_table_count"]) == (0, 0)
    assert facts["gate_findings"] == [] and facts["gate_findings_not_shown"] == 0
    StepGateFacts.model_validate(facts)


def test_each_table_keeps_what_dms_reported():
    rows = step_gate_facts(fx.passed())["gate_tables"]
    assert [r["table"] for r in rows] == ["shop.audit_log", "shop.customers", "shop.orders"]
    assert rows[0] == {"table": "shop.audit_log", "rows_loaded": 12,
                       "validation_state": "No primary key", "failed_records": 0,
                       "suspended_records": 0, "status": "exception",
                       "status_text": "Named exception"}  # fmt: skip
    assert rows[2]["rows_loaded"] == 1233321 and rows[2]["status_text"] == "Validated"


def test_the_named_exceptions_carry_their_reasons():
    exceptions = step_gate_facts(fx.passed())["named_exceptions"]
    assert exceptions == [
        {"table": "shop.audit_log", "column": None, "kind": "table",
         "kind_text": "Not validated", "reason": "not validated: no primary or unique key",
         "rows_loaded": 12},
        {"table": "shop.order_totals", "column": None, "kind": "view",
         "kind_text": "View, not validated", "reason": "not validated: no primary or unique key",
         "rows_loaded": None},
    ]  # fmt: skip
    assert step_gate_facts(fx.passed())["exception_count"] == 2


def test_a_pending_gate_names_its_finding_and_claims_nothing():
    facts = step_gate_facts(fx.pending())
    assert facts["status"] == "pending" and facts["status_text"] == "Pending"
    assert facts["result_line"] == STEP_GATE_PENDING_LINE != REPLICATING_MESSAGE
    assert facts["validating_table_count"] == 1 and facts["validated_table_count"] == 1
    assert "1 still validating" in facts["validation_line"]
    assert facts["gate_findings"] == [{
        "check": "parity", "check_text": "Validation",
        "text": "shop.orders: validation Pending records: the first validation has not finished",
    }]  # fmt: skip
    assert facts["cutover_message"] == CUTOVER_DISCLAIMER


def test_a_failed_gate_names_each_finding_and_its_check():
    facts = step_gate_facts(fx.failed())
    assert facts["status"] == "fail" and facts["status_text"] == "Failed"
    assert facts["result_line"] == STEP_GATE_FAIL_LINE
    assert facts["failed_table_count"] == 1
    assert "1 with validation failures" in facts["validation_line"]
    assert "42 s at the target" in facts["lag_line"]
    assert [(f["check_text"], f["text"].split(" ", 1)[0]) for f in facts["gate_findings"]] == [
        ("Lag", "2"), ("Validation", "shop.orders:")
    ]  # fmt: skip
    orders = next(r for r in facts["gate_tables"] if r["table"] == "shop.orders")
    assert (orders["failed_records"], orders["suspended_records"]) == (3, 1)
    assert orders["status_text"] == "Validation failed"
    assert facts["cutover_message"] == CUTOVER_DISCLAIMER


def test_findings_past_the_cap_are_counted_not_listed():
    facts = step_gate_facts(fx.many_findings(STEP_GATE_FINDINGS + 7))
    assert len(facts["gate_findings"]) == STEP_GATE_FINDINGS
    assert facts["gate_findings_not_shown"] == 7
    exact = step_gate_facts(fx.many_findings(STEP_GATE_FINDINGS))
    assert len(exact["gate_findings"]) == STEP_GATE_FINDINGS
    assert exact["gate_findings_not_shown"] == 0


def test_a_lag_metric_with_no_datapoint_says_so():
    gate = fx.passed()
    gate["checks"][1]["evidence"]["CDCLatencySource_max"] = "none"
    del gate["checks"][1]["evidence"]["CDCLatencyTarget_max"]
    facts = step_gate_facts(gate)
    assert facts["source_lag_seconds"] is None and facts["target_lag_seconds"] is None
    assert "no datapoint at the source, no datapoint at the target" in facts["lag_line"]


def test_a_gate_file_written_before_the_current_contract_still_reads():
    """A 1.0 gate (#1800): the earlier cutover message id and pending-record evidence."""
    gate = fx.passed()
    gate["contract_version"] = "1.0"
    gate["cutover_message_id"] = "cutover-decision-v1"
    gate["checks"][2]["evidence"].update(
        {"pending_records": "0", "tables_with_pending_records": "0"}
    )
    facts = step_gate_facts(gate)
    assert facts["result_line"] == REPLICATING_MESSAGE
    assert facts["cutover_message"] == CUTOVER_DISCLAIMER


def test_no_gate_file_means_no_block():
    assert step_gate_facts(None) is None
    assert read_step_gate_facts(None) is None


def test_a_gate_file_the_contract_refuses_is_left_out_and_logged(caplog):
    broken = fx.passed()
    broken["status"] = "fail"  # disagrees with its checks
    with pytest.raises(ValueError):
        step_gate_facts(broken)
    with caplog.at_level(logging.WARNING):
        assert read_step_gate_facts(broken) is None
    assert "could not be read" in caplog.text
    newer = {**fx.passed(), "contract_version": "9.0"}
    with pytest.raises(ContractVersionError):
        step_gate_facts(newer)
    assert read_step_gate_facts(newer) is None


def test_the_block_is_part_of_the_facts_contract():
    report = _report_with_waves()
    facts = {**report_facts(report), "step_gate": step_gate_facts(fx.failed())}
    loaded = ReportFacts.model_validate(facts)
    assert loaded.step_gate is not None and loaded.step_gate.status == "fail"
    assert ReportFacts.model_validate(report_facts(report)).step_gate is None


@pytest.mark.parametrize("report", [_report_with_waves, _legacy_report], ids=["current", "legacy"])
def test_a_report_whose_step_never_ran_its_gate_is_left_as_it_is(report):
    old = report()
    before = copy.deepcopy(old)
    assert with_step_gate(old, None) is old and old == before
    assert report_facts(old).get("step_gate") is None


def test_the_block_is_attached_without_touching_the_stored_report():
    report = _report_with_waves()
    before = copy.deepcopy(report)
    block = step_gate_facts(fx.passed())
    attached = with_step_gate(report, block)
    assert report == before
    assert report_facts(attached)["step_gate"] == block
    # Every other figure is the report's own.
    assert {k: v for k, v in report_facts(attached).items() if k != "step_gate"} == {
        k: v for k, v in report_facts(report).items() if k != "step_gate"
    }


def test_the_gate_file_is_read_from_the_jobs_artifacts(tmp_path):
    store = LocalArtifactStore(str(tmp_path))
    assert step_gate_key("shop", "job1") == "shop/job1/data-movement/step-1/gate.json"
    assert step_gate_of(store, "shop", "job1") is None
    store.write_json(step_gate_key("shop", "job1"), fx.pending())
    assert step_gate_of(store, "shop", "job1") == step_gate_facts(fx.pending())
    (tmp_path / "shop/job1/data-movement/step-1/gate.json").write_text("{not json")
    assert step_gate_of(store, "shop", "job1") is None


def test_the_ui_fixture_is_the_facts_of_the_failed_gate():
    """The results page's test renders this block; it must be what the facts layer computes."""
    from pathlib import Path

    path = (Path(__file__).resolve().parents[3]
            / "src/ui/src/components/__tests__/fixtures/stepGateFacts.json")  # fmt: skip
    assert json.loads(path.read_text()) == step_gate_facts(
        fx.failed()
    ), "regenerate it: uv run python -m tests.unit.report.step_gate_fixtures"


# #1801 review: a deliverable that lists no table, exception or finding gets lines that
# point to the engineering report, never "below".


@pytest.mark.parametrize("gate", ["passed", "pending", "failed"])
def test_the_brief_lines_never_point_below(gate):
    facts = step_gate_facts(getattr(fx, gate)())
    for key in ("brief_result_line", "brief_validation_line"):
        assert "below" not in facts[key], (gate, key)
    if gate == "passed":
        assert facts["brief_result_line"] == REPLICATING_MESSAGE
    assert facts["brief_validation_line"].startswith(facts["validation_line"].split(".", 1)[0])


def test_the_brief_lines_name_the_engineering_report_for_the_lists():
    facts = step_gate_facts(fx.failed())
    assert "below" in facts["result_line"] and "below" in facts["validation_line"]
    assert facts["brief_result_line"] == (
        "The gate did not pass: the engineering report names each check that did not."
    )
    assert facts["brief_validation_line"] == (
        "1 of 2 tables with a key validated; 1 with validation failures. "
        "2 named exceptions, named in the engineering report."
    )


# #1801 review: the wording maps and the facts contract's literals follow the gate contract.


def test_the_wording_maps_cover_every_value_the_gate_contract_allows():
    from typing import get_args

    from src.contracts import dms_gate as gate
    from src.contracts.synthesis_output import StepGateExceptionFact, StepGateFinding
    from src.shared import report_facts as rf

    kinds = set(get_args(gate.GateException.model_fields["kind"].annotation))
    checks = set(get_args(gate.GateCheck.model_fields["id"].annotation))
    verdicts = set(get_args(gate.GateTable.model_fields["status"].annotation))
    statuses = set(get_args(gate.GateStatus))
    assert set(rf.STEP_GATE_EXCEPTION_TEXT) == kinds
    assert set(rf.STEP_GATE_CHECK_TEXT) == checks
    assert set(rf.STEP_GATE_TABLE_TEXT) == verdicts
    assert set(rf.STEP_GATE_STATUS_TEXT) == statuses
    assert set(get_args(StepGateExceptionFact.model_fields["kind"].annotation)) == kinds
    assert set(get_args(StepGateFinding.model_fields["check"].annotation)) == checks


def test_a_kind_with_no_wording_yet_renders_generically(monkeypatch):
    from src.shared import report_facts as rf

    monkeypatch.delitem(rf.STEP_GATE_EXCEPTION_TEXT, "view")
    monkeypatch.delitem(rf.STEP_GATE_CHECK_TEXT, "parity")
    monkeypatch.delitem(rf.STEP_GATE_TABLE_TEXT, "pending")
    facts = step_gate_facts(fx.pending())
    assert facts is not None
    view = next(e for e in facts["named_exceptions"] if e["kind"] == "view")
    assert view["kind_text"] == rf.STEP_GATE_GENERIC_EXCEPTION_TEXT
    assert facts["gate_findings"][0]["check_text"] == "Parity"
    orders = next(t for t in facts["gate_tables"] if t["table"] == "shop.orders")
    assert orders["status_text"] == "Pending"


def test_the_intro_is_the_results_pages_description():
    """The engineering report's intro and the results page's description are one sentence."""
    from pathlib import Path

    from src.report.step_gate import STEP_GATE_INTRO

    en = json.loads(
        (Path(__file__).resolve().parents[3] / "src/ui/src/locales/en.json").read_text()
    )
    assert en["analysis-results.step-gate.description"] == STEP_GATE_INTRO.format(
        read_at="{{readAt}}"
    )
