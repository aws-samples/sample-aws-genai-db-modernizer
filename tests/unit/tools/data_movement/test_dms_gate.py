# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The step 1 gate's checks, with stubbed AWS responses (#1800)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from src.contracts.dms_gate import NOT_VALIDATED
from src.contracts.transformation_contract import Finding
from src.shared.cutover_message import CUTOVER_MESSAGES
from src.storage.local_store import LocalArtifactStore
from src.tools.data_movement.dms_gate import (
    lag_check,
    load_check,
    parity_check,
    read_task,
    run_gate,
    task_resource_id,
    write_gate,
)
from src.tools.data_movement.dms_step import DmsStepError, SkippedRule, UnsupportedField
from src.tools.transformation_contract.hashing import canonical_json
from tests.unit.tools.aws.aws_stubs import StubbedCredMgr

from . import gate_stubs as g
from .dms_step_fixtures import make_bundle, make_step1_inputs

INPUTS = make_step1_inputs()


def _view(**kwargs):
    cred = StubbedCredMgr()
    g.queue(cred, INPUTS.contract_sha256, **kwargs)
    view = read_task(g.clients(cred), g.request(), now=g.NOW)
    for stub in cred.stubbers.values():
        stub.assert_no_pending_responses()
    return view


def test_the_task_resource_id_is_the_arns_last_segment():
    assert task_resource_id(g.TASK) == "TASKRESOURCEID01"


def test_a_task_in_cdc_with_every_table_passes_the_load_check():
    check = load_check(INPUTS, _view())
    assert check.status == "pass" and check.evidence["full_load_percent"] == "100"


@pytest.mark.parametrize(("kwargs", "status", "words"), [
    ({"status": "stopped"}, "fail", "stopped"),
    ({"status": "starting"}, "pending", "starting"),
    ({"progress": 60}, "pending", "60%"),
    ({"errored": 1}, "fail", "errored"),
    ({"migration": "full-load"}, "fail", "full-load-and-cdc"),
])  # fmt: skip
def test_the_load_check_reads_the_task(kwargs, status, words):
    check = load_check(INPUTS, _view(**kwargs))
    assert check.status == status and any(words in r for r in check.reasons)


def test_a_table_missing_after_the_full_load_fails_and_during_it_is_pending():
    stats = [g.stat("shop.audit_log"), g.stat("shop.customers")]
    assert load_check(INPUTS, _view(stats=stats)).status == "fail"
    assert load_check(INPUTS, _view(stats=stats, progress=40)).status == "pending"


def test_a_table_error_fails():
    stats = [
        g.stat("shop.audit_log"),
        g.stat("shop.customers", state="Table error"),
        g.stat("shop.orders"),
    ]
    check = load_check(INPUTS, _view(stats=stats))
    assert check.status == "fail" and any("shop.customers" in r for r in check.reasons)


def test_a_task_from_another_contract_fails():
    check = load_check(INPUTS, _view(tag="b" * 64))
    assert check.status == "fail" and any(
        "another transformation contract" in r for r in check.reasons
    )


def test_lag_under_the_threshold_passes():
    assert lag_check(_view(), 5, 15).status == "pass"


def test_lag_over_the_threshold_fails():
    check = lag_check(_view(target=(1.0, 7.5, 2.0)), 5, 15)
    assert check.status == "fail" and check.evidence["CDCLatencyTarget_max"] == "7.5"
    assert lag_check(_view(source=(), target=()), 5, 15).status == "pending"


def test_the_window_is_configurable():
    cred = StubbedCredMgr()
    g.queue(cred, INPUTS.contract_sha256)
    seen = []
    cred.clients["cloudwatch"].meta.events.register("provide-client-params.cloudwatch.GetMetricData",
                                                     lambda params, **_: seen.append(params))  # fmt: skip
    read_task(g.clients(cred), g.request(window_minutes=60), now=g.NOW)
    assert (g.NOW - seen[0]["StartTime"]).total_seconds() == 3600 and seen[0]["EndTime"] == g.NOW
    dims = seen[0]["MetricDataQueries"][0]["MetricStat"]["Metric"]["Dimensions"]
    assert {"Name": "ReplicationTaskIdentifier", "Value": "TASKRESOURCEID01"} in dims


def test_table_statistics_follow_the_marker():
    cred = StubbedCredMgr()
    g.queue(
        cred,
        INPUTS.contract_sha256,
        stat_pages=[[g.stat("shop.audit_log")], [g.stat("shop.orders")]],
    )
    view = read_task(g.clients(cred), g.request(), now=g.NOW)
    assert sorted(view.statistics) == ["shop.audit_log", "shop.orders"]


@pytest.mark.parametrize(
    ("state", "status"),
    [("Full load", "pending"), ("Table is being reloaded", "pending"), ("Before load", "pending"),
     ("", "pending"), ("Table cancelled", "fail"), ("Table does not exist", "fail"), ("Table error", "fail")],
)  # fmt: skip
def test_a_contract_table_must_be_completed_to_pass(state, status):
    stats = [g.stat("shop.audit_log"), g.stat("shop.customers", state=state), g.stat("shop.orders")]
    check = load_check(INPUTS, _view(stats=stats))
    assert check.status == status
    assert any("shop.customers" in r and (state in r or not state) for r in check.reasons)


def test_a_load_error_row_or_failed_conditional_check_fails():
    stats = [g.stat("shop.audit_log"), g.stat("shop.customers"), g.stat("shop.orders")]
    assert load_check(INPUTS, _view(stats=stats)).status == "pass"
    stats[1]["FullLoadCondtnlChkFailedRows"] = 2
    check = load_check(INPUTS, _view(stats=stats))
    assert check.status == "fail" and any("shop.customers" in r for r in check.reasons)


def test_a_table_dms_reports_but_the_contract_does_not_select_fails():
    stats = [*g.ready_stats(), g.stat("shop.extra")]
    check = load_check(INPUTS, _view(stats=stats))
    assert check.status == "fail"
    assert "DMS reports shop.extra, which the contract does not select" in check.reasons


def test_one_errored_table_reads_in_the_singular():
    check = load_check(INPUTS, _view(errored=1))
    assert "1 table errored" in " ".join(check.reasons) and "1 tables" not in " ".join(
        check.reasons
    )


def test_a_missing_metric_or_an_empty_latency_map_is_pending():
    view = _view()
    assert lag_check(replace(view, latency={}), 5, 15).status == "pending"
    only_source = {"CDCLatencySource": view.latency["CDCLatencySource"]}
    check = lag_check(replace(view, latency=only_source), 5, 15)
    assert check.status == "pending" and any("CDCLatencyTarget" in r for r in check.reasons)


def test_one_metric_empty_and_the_other_populated_is_pending():
    check = lag_check(_view(source=()), 5, 15)
    assert check.status == "pending" and any("CDCLatencySource" in r for r in check.reasons)


def test_a_datapoint_exactly_at_the_threshold_fails_and_just_under_passes():
    assert lag_check(_view(target=(5.0,)), 5, 15).status == "fail"
    assert lag_check(_view(target=(4.9,)), 5, 15).status == "pass"


def test_the_dimension_is_discovered_from_the_listed_metrics():
    cred = StubbedCredMgr()
    g.queue(cred, INPUTS.contract_sha256, task_dim="dm-job1-step-1")
    # the listed metric carries the task's name, not its resource id: that value is queried
    seen = []
    cred.clients["cloudwatch"].meta.events.register(
        "provide-client-params.cloudwatch.GetMetricData", lambda params, **_: seen.append(params)
    )
    view = read_task(g.clients(cred), g.request(), now=g.NOW)
    dims = seen[0]["MetricDataQueries"][0]["MetricStat"]["Metric"]["Dimensions"]
    assert {"Name": "ReplicationTaskIdentifier", "Value": "dm-job1-step-1"} in dims
    assert lag_check(view, 5, 15).status == "pass"


def test_no_listed_metric_for_the_task_is_pending():
    view = _view(task_dim=None)
    check = lag_check(view, 5, 15)
    assert check.status == "pending"
    assert "no CDC latency metric found for the task" in " ".join(check.reasons)


def test_stale_latency_is_pending():
    check = lag_check(_view(age_minutes=10), 5, 15)
    assert check.status == "pending" and any("no recent latency data" in r for r in check.reasons)
    assert lag_check(_view(age_minutes=1), 5, 15).status == "pass"


def test_a_task_without_an_instance_arn_is_an_error():
    cred = StubbedCredMgr()
    g.queue(cred, INPUTS.contract_sha256, instance_arn="")
    with pytest.raises(DmsStepError, match="replication instance"):
        read_task(g.clients(cred), g.request(), now=g.NOW)


def test_a_missing_stack_is_an_error():
    cred = StubbedCredMgr()
    g.clients(cred)
    cred.stub("cloudformation").add_client_error(
        "describe_stacks", "ValidationError", "does not exist"
    )
    with pytest.raises(DmsStepError, match=g.STACK):
        read_task(g.clients(cred), g.request(), now=g.NOW)


def test_the_window_must_be_positive():
    with pytest.raises(DmsStepError, match="window"):
        g.request(window_minutes=0)


# --- parity, exceptions and the result (Task 4) ---


def _gate(inputs=INPUTS, **kwargs):
    cred = StubbedCredMgr()
    g.queue(cred, inputs.contract_sha256, **kwargs)
    return run_gate(inputs, g.request(), g.clients(cred), now=g.NOW)


def _cust(**kw):
    return [g.stat("shop.audit_log"), g.stat("shop.customers", **kw), g.stat("shop.orders")]


def test_a_task_in_cdc_with_validated_tables_passes():
    gate = _gate()
    assert gate.status == "pass" and [c.status for c in gate.checks] == ["pass", "pass", "pass"]


@pytest.mark.parametrize("validation", ["Pending records", "Pending validation", "Pending revalidation",
                                        "Preparing table"])  # fmt: skip
def test_a_table_still_validating_keeps_the_gate_pending(validation):
    gate = _gate(stats=_cust(validation=validation))
    assert gate.status == "pending" and gate.checks[2].status == "pending"
    assert next(t for t in gate.tables if t.table == "shop.customers").status == "pending"


@pytest.mark.parametrize("validation", ["Mismatched records", "Suspended records", "Table error", "Error",
                                        "Not enabled", "No primary key"])  # fmt: skip
def test_every_failure_state_on_a_keyed_table_fails_the_gate(validation):
    gate = _gate(stats=_cust(validation=validation))
    assert gate.status == "fail" and any("shop.customers" in r for r in gate.checks[2].reasons)


@pytest.mark.parametrize("validation", ["Brand new state", ""])
def test_an_unknown_or_missing_state_never_passes(validation):
    assert _gate(stats=_cust(validation=validation)).checks[2].status == "pending"


def test_failed_records_fail_even_when_the_state_reads_validated():
    assert _gate(stats=_cust(failed=1)).checks[2].status == "fail"


def test_suspended_records_fail_even_when_the_state_reads_validated():
    assert _gate(stats=_cust(suspended=2)).checks[2].status == "fail"


def test_pending_records_keep_a_validated_state_from_passing():
    stats = _cust()
    stats[1]["ValidationPendingRecords"] = 4
    assert _gate(stats=stats).checks[2].status == "pending"


def test_a_keyed_table_missing_from_the_statistics_is_pending_in_parity():
    check, tables, _ = parity_check(
        INPUTS, _view(stats=[g.stat("shop.audit_log"), g.stat("shop.orders")])
    )
    assert (
        check.status == "pending"
        and next(t for t in tables if t.table == "shop.customers").status == "pending"
    )


def test_a_keyless_table_and_a_view_are_named_exceptions():
    gate = _gate()
    assert [(e.table, e.kind, e.reason, e.rows_loaded) for e in gate.exceptions] == [
        ("shop.audit_log", "table", NOT_VALIDATED, 12), ("shop.v_orders", "view", NOT_VALIDATED, None)]  # fmt: skip
    assert next(t for t in gate.tables if t.table == "shop.audit_log").status == "exception"
    assert gate.status == "pass"


def test_a_keyless_table_dms_validated_anyway_is_still_an_exception():
    stats = [
        g.stat("shop.audit_log", validation="Validated"),
        g.stat("shop.customers"),
        g.stat("shop.orders"),
    ]
    assert any(e.table == "shop.audit_log" for e in _gate(stats=stats).exceptions)


def test_a_keyless_table_with_lob_columns_is_named_for_lost_lob_values():
    plan = INPUTS.plan.model_copy(deep=True)
    plan.replication.lob_tables.append(type(plan.replication.lob_tables[0])(table="shop.audit_log", mode="full",
                                                                            columns=["msg"]))  # fmt: skip
    gate = _gate(replace(INPUTS, plan=plan))
    lob = [e for e in gate.exceptions if e.kind == "keyless_lob"]
    assert [(e.table, e.rows_loaded) for e in lob] == [
        ("shop.audit_log", 12)
    ] and "LOB values can be lost" in lob[0].reason
    assert gate.status == "pass"


def test_keyed_lob_tables_are_not_keyless_lob_exceptions():
    assert not [e for e in _gate().exceptions if e.kind == "keyless_lob"]


def test_unsupported_fields_and_skipped_rules_are_named_exceptions():
    inputs = replace(INPUTS, unsupported=(UnsupportedField("shop.orders", "note", "no op"),),
                     skipped=(SkippedRule("shop.legacy", "no target type"),))  # fmt: skip
    gate = _gate(inputs)
    got = {(e.table, e.kind, e.column, e.reason) for e in gate.exceptions}
    assert ("shop.orders", "unsupported_field", "note", "no op") in got
    assert ("shop.legacy", "skipped_rule", None, "no target type") in got
    assert gate.status == "pass"
    assert [e.table for e in gate.exceptions] == sorted(e.table for e in gate.exceptions)


def test_exceptions_do_not_hide_a_failure():
    inputs = replace(INPUTS, skipped=(SkippedRule("shop.legacy", "blocked"),))
    assert _gate(inputs, stats=_cust(failed=1)).status == "fail"


def test_the_overall_status_is_the_worst_check():
    assert _gate(progress=50).status == "pending"
    assert _gate(source=(9.0, 9.0)).status == "fail"


def test_the_result_carries_the_hash_decision_and_message_id():
    gate = _gate()
    assert gate.contract_sha256 == INPUTS.contract_sha256 and gate.cutover_decision == "customer"
    assert (
        gate.cutover_message_id in CUTOVER_MESSAGES
        and gate.max_lag_seconds == 5
        and gate.task_arn == g.TASK
    )


def test_the_gate_writes_only_its_file(tmp_path):
    b = make_bundle()
    store = LocalArtifactStore(str(tmp_path))
    store.write_json("shop/job1/data-movement/step-1/plan.json", b.plan.model_dump(mode="json"))
    store.write_bytes(
        "shop/job1/transformation-contract/v2/step-1.json", canonical_json(b.contract)
    )
    before = set(store.list_prefix("shop/job1/"))
    cred = StubbedCredMgr()
    g.queue(cred, b.plan.contract.sha256)
    path, gate = write_gate(store, "shop", "job1", g.request(), g.clients(cred), now=g.NOW)
    assert path == "shop/job1/data-movement/step-1/gate.json"
    assert set(store.list_prefix("shop/job1/")) - before == {path}
    assert store.read_json(path)["status"] == gate.status == "pass"


def test_no_keyed_table_says_nothing_was_validated_and_stays_pass():
    contract = INPUTS.contract.model_copy(deep=True)
    for r in contract.rules:
        r.primary_key = []
    for r in contract.rules:
        if not any(f.code == "no_key" for f in r.findings):
            r.findings.append(
                Finding(code="no_key", severity="warning", message="x", table_id=r.source_table)
            )
    stats = [g.stat(r.source_table, validation="No primary key") for r in contract.rules]
    gate = _gate(replace(INPUTS, contract=contract), stats=stats)
    parity = gate.checks[2]
    assert parity.status == "pass" and parity.summary.startswith(
        "No table has a key DMS can validate with"
    )
    assert "nothing was validated" in parity.summary
    assert {e.table for e in gate.exceptions if e.kind == "table"} == {
        r.source_table for r in contract.rules
    }


def _with_key_type(inputs, table_id, column, target_type):
    contract = inputs.contract.model_copy(deep=True)
    rule = next(r for r in contract.rules if r.source_table == table_id)
    next(f for f in rule.fields if f.source_column == column).target_type = target_type
    return replace(inputs, contract=contract)


def _parity(inputs):
    stats = [g.stat(r.source_table) for r in inputs.contract.rules]
    cred = StubbedCredMgr()
    g.queue(cred, inputs.contract_sha256, stats=stats)
    return parity_check(inputs, read_task(g.clients(cred), g.request(), now=g.NOW))


@pytest.mark.parametrize(("engine", "schema", "bad"), [
    ("aurora_mysql", "shop", ["LONGTEXT", "TEXT", "BLOB", "MEDIUMBLOB", "BINARY(16)", "VARBINARY(16)", "VARCHAR(2048)",
                              "VARCHAR(1024)", "varchar"]),
    ("aurora_postgresql", "public", ["TEXT", "BYTEA", "VARCHAR", "character varying", "VARCHAR(1024)"]),
])  # fmt: skip
def test_a_key_column_validation_refuses_makes_the_table_an_exception(engine, schema, bad):
    inputs = make_step1_inputs(engine)
    for type_ in bad:
        check, tables, exceptions = _parity(_with_key_type(inputs, f"{schema}.orders", "id", type_))
        assert [
            (e.table, e.kind, e.reason) for e in exceptions if e.table == f"{schema}.orders"
        ] == [(f"{schema}.orders", "table", NOT_VALIDATED)], type_
        assert next(t for t in tables if t.table == f"{schema}.orders").status == "exception"
        assert check.evidence["keyed_tables"] == "1"


@pytest.mark.parametrize(
    ("engine", "schema"), [("aurora_mysql", "shop"), ("aurora_postgresql", "public")]
)
@pytest.mark.parametrize(
    "good", ["BIGINT", "VARCHAR(255)", "VARCHAR(1023)", "CHAR(36)", "char", "INT"]
)
def test_a_key_column_validation_accepts_stays_keyed(engine, schema, good):
    inputs = _with_key_type(make_step1_inputs(engine), f"{schema}.orders", "id", good)
    check, _, exceptions = _parity(inputs)
    assert check.evidence["keyed_tables"] == "2" and not [
        e for e in exceptions if e.table == f"{schema}.orders"
    ]


def test_skipped_and_unsupported_exceptions_carry_the_rows_dms_loaded():
    inputs = replace(INPUTS, unsupported=(UnsupportedField("shop.orders", "note", "no op"),),
                     skipped=(SkippedRule("shop.audit_log", "blocked by an error"),))  # fmt: skip
    stats = [
        g.stat("shop.audit_log", rows=12),
        g.stat("shop.customers"),
        g.stat("shop.orders", rows=77),
    ]
    got = {(e.kind, e.rows_loaded) for e in _gate(inputs, stats=stats).exceptions}
    assert ("unsupported_field", 77) in got and ("skipped_rule", 12) in got


def test_no_keyless_lob_exception_when_the_plan_has_no_replication_section():
    plan = INPUTS.plan.model_copy(deep=True)
    plan.replication = None
    check, _, exceptions = parity_check(replace(INPUTS, plan=plan), _view())
    assert not [e for e in exceptions if e.kind == "keyless_lob"] and check.status == "pass"


# --- review round: staleness, discovered dimensions, evidence ---


def test_staleness_limit_is_five_minutes_by_default_and_overridable():
    # newest datapoint ages: age_minutes + 1 minute
    assert lag_check(_view(age_minutes=3), 5, 15).status == "pass"  # 240 s
    assert lag_check(_view(age_minutes=4), 5, 15).status == "pass"  # 300 s: at the limit
    stale = lag_check(_view(age_minutes=5), 5, 15)  # 360 s
    assert stale.status == "pending" and any("300 s" in r for r in stale.reasons)
    assert lag_check(_view(age_minutes=3), 5, 15, stale_seconds=200).status == "pending"
    assert lag_check(_view(age_minutes=5), 5, 15, stale_seconds=360).status == "pass"


def test_the_newest_datapoint_age_is_evidence():
    check = lag_check(_view(age_minutes=1), 5, 15)
    assert check.evidence["CDCLatencySource_newest_age_s"] == "120"
    assert check.evidence["CDCLatencyTarget_newest_age_s"] == "120"
    assert (
        lag_check(_view(source=(), target=()), 5, 15).evidence["CDCLatencyTarget_newest_age_s"]
        == "none"
    )


def test_a_request_rejects_a_non_positive_stale_limit():
    with pytest.raises(DmsStepError):
        g.request(stale_seconds=0)


def test_the_listed_metrics_dimensions_are_passed_unchanged():
    cred = StubbedCredMgr()
    extra = {"Name": "SomethingElse", "Value": "x"}
    g.queue(cred, INPUTS.contract_sha256, metric_dims=(extra,))
    seen = []
    cred.clients["cloudwatch"].meta.events.register(
        "provide-client-params.cloudwatch.GetMetricData", lambda params, **_: seen.append(params)
    )
    read_task(g.clients(cred), g.request(), now=g.NOW)
    dims = seen[0]["MetricDataQueries"][0]["MetricStat"]["Metric"]["Dimensions"]
    assert dims == [extra, {"Name": "ReplicationTaskIdentifier", "Value": "TASKRESOURCEID01"}]


def test_pending_record_totals_are_parity_evidence():
    stats = _cust()
    stats[1]["ValidationPendingRecords"] = 4
    stats[2]["ValidationPendingRecords"] = 3
    check = _gate(stats=stats).checks[2]
    assert check.status == "pending"
    assert (
        check.evidence["pending_records"] == "7"
        and check.evidence["tables_with_pending_records"] == "2"
    )
    assert _gate().checks[2].evidence["pending_records"] == "0"


def test_the_exception_count_names_distinct_tables():
    plan = INPUTS.plan.model_copy(deep=True)
    lob = type(plan.replication.lob_tables[0])
    plan.replication.lob_tables.append(lob(table="shop.audit_log", mode="full", columns=["msg"]))
    gate = _gate(replace(INPUTS, plan=plan))
    assert len(gate.exceptions) == 3 and {e.table for e in gate.exceptions} == {
        "shop.audit_log",
        "shop.v_orders",
    }
    assert "3 named exceptions (2 tables)" in gate.checks[2].summary
