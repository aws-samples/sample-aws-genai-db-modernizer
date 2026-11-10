# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Table errors and validation failures while Prove polls (#2064): the progress line names
them (names and counts, never a value), an errored table ends the wait at once, the customer
gets read-only SQL for DMS's validation failure table, and the per-table reload runs only
when it is provably safe."""

from __future__ import annotations

import json

import pytest
from botocore.stub import ANY

from scripts import check_dms_step_gate
from scripts import run_dms_step as script
from tests.unit.scripts.test_run_dms_step import (
    TASK,
    _clients,
    _connections,
    _job,
    _no_pending,
    _resources,
    _run,
    _stack,
    _task_by_arn,
)
from tests.unit.tools.aws.aws_stubs import REGION, StubbedCredMgr
from tests.unit.tools.data_movement import gate_stubs as g
from tests.unit.tools.data_movement.dms_step_fixtures import make_bundle


def _row(table, state="Table completed", validation="Pending records", **fields):
    schema, _, name = table.rpartition(".")
    return {"SchemaName": schema, "TableName": name, "TableState": state,
            "ValidationState": validation, **fields}  # fmt: skip


def _running(cred, rows, percent=60, errored=0, settings=None):
    """One status read of a running task: the stack, the connections, the task, its tables."""
    _stack(cred)
    _connections(cred)
    task = {"ReplicationTaskArn": TASK, "Status": "running",
            "ReplicationTaskStats": {"FullLoadProgressPercent": percent, "TablesLoaded": 1,
                                     "TablesErrored": errored}}  # fmt: skip
    if settings is not None:
        task["ReplicationTaskSettings"] = json.dumps(settings)
    cred.stub("dms").add_response("describe_replication_tasks", {"ReplicationTasks": [task]})
    cred.stub("dms").add_response("describe_table_statistics",
                                  {"ReplicationTaskArn": TASK, "TableStatistics": rows})  # fmt: skip


def _status(tmp_path, capsys, cred, *extra):
    code = script.main(["status", "--job-id", "job1", "--db", "shop", "--artifact-root",
                        str(tmp_path), *extra], clients=_clients(cred), sleep=lambda _s: None)  # fmt: skip
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    return code, lines[:-1], lines[-1]


DUPLICATE = _row("shop.orders", state="Table error", FullLoadRows=40,
                 FullLoadStartTime="2026-11-09T00:00:00Z")  # fmt: skip


# --- No errors --------------------------------------------------------------------------------


def test_a_clean_load_carries_zero_errors_and_no_failure_check(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _running(cred, [_row("shop.orders"), _row("shop.customers")])
    code, _, out = _status(tmp_path, capsys, cred)
    assert code == 0 and out["phase"] == "loading" and out["tables_errored"] == 0
    assert out["table_problems"]["errored_tables"] == []
    assert out["table_problems"]["validation_failures"] == []
    assert "validation_failures_check" not in out and "table_recovery" not in out
    line = script.progress_line(out)
    assert line["tables_errored"] == 0 and line["errored_tables"] == []
    assert line["validation_failures"] == [] and line["errored_tables_not_shown"] == 0
    assert line["validation_failures_not_shown"] == 0


# --- One errored table ------------------------------------------------------------------------


def test_an_errored_table_ends_the_wait_at_once_and_is_named_on_the_progress_line(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _running(cred, [_row("shop.orders"), _row("shop.customers")], percent=40)
    _running(cred, [DUPLICATE, _row("shop.customers")], percent=87, errored=1)
    code, progress, out = _status(tmp_path, capsys, cred, "--until", "caught_up",
                                  "--wait-seconds", "540", "--interval-seconds", "60")  # fmt: skip
    _no_pending(cred)  # two reads, not nine: the wait stopped on the errored table
    assert code == 0 and out["phase"] == "table_errors" and out["reached"] is True
    assert out["tables_errored"] == 1
    assert out["table_problems"]["errored_tables"] == [
        {"table": "shop.orders", "table_state": "Table error"}
    ]
    assert [p["tables_errored"] for p in progress] == [0]
    assert script.progress_line(out)["errored_tables"] == ["shop.orders"]


def test_the_tasks_own_errored_count_alone_is_reported_and_never_ends_the_wait(tmp_path, capsys):
    # A stale TablesErrored with no table in an error state would end every later run.
    _job(tmp_path)
    cred = StubbedCredMgr()
    _running(cred, [_row("shop.orders")], errored=2)
    _, _, out = _status(tmp_path, capsys, cred)
    assert out["phase"] == "loading" and out["tables_errored"] == 0
    assert out["table_problems"]["task_tables_errored"] == 2 and "table_recovery" not in out


def test_rows_dms_counts_only_for_dynamodb_targets_are_never_an_error_signal(tmp_path, capsys):
    # FullLoadErrorRows is "valid only for migrations where DynamoDB is the target".
    _job(tmp_path)
    cred = StubbedCredMgr()
    _running(cred, [_row("shop.orders", FullLoadErrorRows=3, FullLoadCondtnlChkFailedRows=1)])
    _, _, out = _status(tmp_path, capsys, cred)
    assert out["phase"] == "loading" and out["table_problems"]["errored_tables"] == []


@pytest.mark.parametrize("written", [{"FullLoadStartTime": "2026-11-09T00:00:00Z"},
                                     {"FullLoadReloaded": True}])  # fmt: skip
def test_an_errored_table_with_zero_counters_but_a_load_start_or_reload_is_never_reloaded(
    tmp_path, capsys, written
):
    # DMS restarts its counters with each reload: a start time or a reload alone shows a write.
    _job(tmp_path)
    cred = StubbedCredMgr()
    row = _row("shop.orders", state="Table error", FullLoadRows=0, **written)
    _running(cred, [row])
    _, _, out = _status(tmp_path, capsys, cred)
    assert out["phase"] == "table_errors" and out["table_recovery"] is None
    assert "shop.orders was loaded" in out["table_recovery_refused"] and out["table_steps"]
    cred = StubbedCredMgr()
    _owned(cred, rows=[row])  # no reload_tables queued: a call would fail the stubber
    code, line = _run(tmp_path, "reload-tables", "--approve", clients=_clients(cred),
                      capsys=capsys)  # fmt: skip
    _no_pending(cred)
    assert code == 1 and line["kind"] == "target_may_hold_rows"


def test_a_table_cancelled_once_does_not_end_the_wait(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    done = _row("shop.orders", validation="Validated")
    _running(cred, [_row("shop.orders", state="Table cancelled")], percent=50)
    _running(cred, [_row("shop.orders")], percent=80)
    _running(cred, [done], percent=100)
    _, progress, out = _status(tmp_path, capsys, cred, "--until", "caught_up",
                               "--wait-seconds", "540", "--interval-seconds", "60")  # fmt: skip
    _no_pending(cred)
    assert out["phase"] == "caught_up" and [p["phase"] for p in progress] == ["loading", "loading"]
    assert progress[0]["errored_tables"] == ["shop.orders"] and progress[0]["tables_errored"] == 0


def test_a_table_cancelled_on_two_polls_in_a_row_ends_the_wait(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    for _ in range(2):
        _running(cred, [_row("shop.orders", state="Table cancelled")], percent=50)
    _, _, out = _status(tmp_path, capsys, cred, "--until", "caught_up", "--wait-seconds", "540",
                        "--interval-seconds", "60")  # fmt: skip
    _no_pending(cred)
    assert out["phase"] == "table_errors" and out["reached"] is True
    assert "two polls in a row" in out["table_errors_reason"]


def test_a_table_that_holds_rows_gets_the_customers_truncate_then_reload_steps(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _running(cred, [DUPLICATE], errored=1)
    _, _, out = _status(tmp_path, capsys, cred)
    assert out["table_recovery"] is None
    assert "shop.orders FullLoadRows is 40" in out["table_recovery_refused"]
    steps = out["table_steps"]
    assert (
        "SET FOREIGN_KEY_CHECKS = 0; TRUNCATE TABLE `shop`.`orders`; SET FOREIGN_KEY_CHECKS = 1;"
        in steps
    )
    reload = next(s for s in steps if "aws dms reload-tables" in s)
    assert f"--replication-task-arn {TASK}" in reload and "--reload-option data-reload" in reload
    assert "SchemaName=shop,TableName=orders" in reload and f"--region {REGION}" in reload


def test_an_errored_table_dms_never_wrote_to_is_offered_reload_tables(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _running(cred, [_row("shop.orders", state="Table does not exist", FullLoadRows=0)], errored=1)
    _, _, out = _status(tmp_path, capsys, cred)
    assert out["phase"] == "table_errors" and out["table_recovery"] == "reload-tables"
    assert "table_steps" not in out


# --- Validation failures, suspended validation --------------------------------------------------


def test_validation_failures_are_named_with_counts_and_point_at_dms_failure_table(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    rows = [_row("shop.orders", validation="Mismatched records", ValidationFailedRecords=4),
            _row("shop.customers", validation="Suspended records", ValidationSuspendedRecords=2),
            _row("shop.audit_log", validation="Validated")]  # fmt: skip
    _running(cred, rows, percent=100)
    _, _, out = _status(tmp_path, capsys, cred)
    assert out["phase"] != "table_errors" and out["tables_errored"] == 0
    line = script.progress_line(out)
    assert line["validation_failures"] == [
        {"table": "shop.customers", "validation_state": "Suspended records", "failed_records": 0,
         "suspended_records": 2},
        {"table": "shop.orders", "validation_state": "Mismatched records", "failed_records": 4,
         "suspended_records": 0}]  # fmt: skip
    check = out["validation_failures_check"]
    assert check["failures_table"] == "awsdms_control.awsdms_validation_failures_v1"
    sql = [s for s in check["check_on_target"] if s.startswith("SELECT")]
    assert sql and all("TASK_NAME = 'TASK'" in s or "information_schema" in s for s in sql)
    assert not any(s.split()[0] in ("UPDATE", "DELETE", "INSERT", "TRUNCATE") for s in sql)


@pytest.mark.parametrize("version, table", [("3.6.1", "public.awsdms_validation_failures_v2"),
                                            ("3.5.4", "public.awsdms_validation_failures_v1")])  # fmt: skip
def test_a_postgresql_step_picks_the_failure_table_from_the_dms_engine_version(
    tmp_path, capsys, version, table
):
    _job(tmp_path)
    plan = tmp_path / "shop" / "job1" / "data-movement" / "step-1" / "plan.json"
    data = json.loads(plan.read_text())
    data["source"]["engine"] = "postgresql"
    data["replication"]["engine_version"] = version
    plan.write_text(json.dumps(data))
    cred = StubbedCredMgr()
    _running(cred, [_row("public.orders", validation="Mismatched records",
                         ValidationFailedRecords=1)], percent=100)  # fmt: skip
    _, _, out = _status(tmp_path, capsys, cred)
    check = out["validation_failures_check"]
    assert check["failures_table"] == table
    assert any(f"FROM {table} WHERE \"TASK_NAME\" = 'TASK'" in s for s in check["check_on_target"])
    assert any("information_schema.tables" in s for s in check["check_on_target"])  # the lookup


@pytest.mark.passes_on_main  # guards the settings the failure table names rely on
def test_the_task_settings_keep_dms_control_tables_in_their_default_schema() -> None:
    # The failure tables named above are DMS's defaults: no ControlSchema may move them.
    from src.tools.data_movement.dms_step import bundle_inputs, task_settings

    settings = task_settings(bundle_inputs(make_bundle()))
    assert "ControlTablesSettings" not in settings and "ControlSchema" not in json.dumps(settings)


def test_a_table_that_errors_then_recovers_goes_back_to_loading(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _running(cred, [DUPLICATE], errored=1)
    _, _, first = _status(tmp_path, capsys, cred)
    assert first["phase"] == "table_errors"
    # After the customer emptied it and reloaded it, DMS restarts its statistics.
    _running(cred, [_row("shop.orders", state="Table is being reloaded")], percent=50)
    _, _, after = _status(tmp_path, capsys, cred)
    assert after["phase"] == "loading" and after["tables_errored"] == 0
    assert "table_recovery" not in after


# --- Many tables -------------------------------------------------------------------------------


def test_many_errored_tables_are_capped_with_a_count_of_the_rest(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    rows = [_row(f"shop.t{i:02}", state="Table error", FullLoadRows=1) for i in range(25)]
    rows += [_row(f"shop.v{i:02}", validation="Mismatched records", ValidationFailedRecords=1)
             for i in range(12)]  # fmt: skip
    _running(cred, rows, errored=25)
    _, _, out = _status(tmp_path, capsys, cred)
    problems = out["table_problems"]
    assert len(problems["errored_tables"]) == 10 and problems["errored_tables_not_shown"] == 15
    assert len(problems["validation_failures"]) == 10
    assert problems["validation_failures_not_shown"] == 2
    line = script.progress_line(out)
    assert len(line["errored_tables"]) == 10 and line["errored_tables_not_shown"] == 15
    assert line["validation_failures_not_shown"] == 2
    assert out["tables_errored"] == 25
    # The customer's steps empty every errored table, never only the ones shown.
    assert all(f"`shop`.`t{i:02}`" in " ".join(out["table_steps"]) for i in range(25))


# --- reload-tables and revalidate --------------------------------------------------------------


def _owned(cred, status="running", rows=(), settings=None):
    _stack(cred, job_tag="job1")
    _resources(cred)
    fields = {} if settings is None else {"ReplicationTaskSettings": json.dumps(settings)}
    _task_by_arn(cred, status, **fields)
    if status == "running" and rows is not None:
        cred.stub("dms").add_response("describe_table_statistics",
                                      {"ReplicationTaskArn": TASK, "TableStatistics": list(rows)})  # fmt: skip


@pytest.mark.parametrize("command", ["reload-tables", "revalidate"])
def test_the_table_writes_are_refused_without_approve_and_under_the_sandbox(
    tmp_path, capsys, monkeypatch, command
):
    _job(tmp_path)
    code, out = _run(tmp_path, command, clients=_clients(StubbedCredMgr()), capsys=capsys)
    assert code == 1 and "pass --approve" in out["message"]
    monkeypatch.setenv("MODERNIZER_CI_SANDBOX", "1")
    code, out = _run(tmp_path, command, "--approve", clients=_clients(StubbedCredMgr()),
                     capsys=capsys)  # fmt: skip
    assert code == 1 and "refused under MODERNIZER_CI_SANDBOX=1" in out["message"]


def test_reload_tables_reloads_only_errored_tables_dms_never_wrote_to(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    rows = [
        _row("shop.orders", state="Table does not exist", FullLoadRows=0),
        _row("shop.customers"),
    ]
    _owned(cred, rows=rows)
    cred.stub("dms").add_response("reload_tables", {"ReplicationTaskArn": TASK},
                                  {"ReplicationTaskArn": TASK, "ReloadOption": "data-reload",
                                   "TablesToReload": [{"SchemaName": "shop", "TableName": "orders"}]})  # fmt: skip
    code, out = _run(tmp_path, "reload-tables", "--approve", clients=_clients(cred),
                     capsys=capsys)  # fmt: skip
    _no_pending(cred)
    assert code == 0 and out["status"] == "reloading"
    assert out["tables_reloaded"] == ["shop.orders"]


def test_reload_tables_refuses_a_table_that_may_hold_rows_and_writes_nothing(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _owned(cred, rows=[DUPLICATE])  # no reload_tables queued: a call would fail the stubber
    code, out = _run(tmp_path, "reload-tables", "--approve", clients=_clients(cred),
                     capsys=capsys)  # fmt: skip
    _no_pending(cred)
    assert code == 1 and out["kind"] == "target_may_hold_rows"
    assert out["tables_to_empty"] == ["shop.orders"]
    assert any("TRUNCATE TABLE `shop`.`orders`" in s for s in out["table_steps"])


def test_the_printed_reload_command_shell_quotes_each_table(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _running(cred, [_row("shop.or$ders", state="Table error", FullLoadRows=5)])
    _, _, out = _status(tmp_path, capsys, cred)
    reload = next(s for s in out["table_steps"] if "aws dms reload-tables" in s)
    assert "--tables-to-reload 'SchemaName=shop,TableName=or$ders' --reload-option" in reload


def test_a_later_reload_batch_that_fails_names_the_tables_already_reloaded(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    rows = [_row(f"shop.t{i:02}", state="Table does not exist") for i in range(12)]
    _owned(cred, rows=rows)
    cred.stub("dms").add_response("reload_tables", {"ReplicationTaskArn": TASK})
    cred.stub("dms").add_client_error("reload_tables", "InvalidResourceStateFault", "not running")
    code, out = _run(tmp_path, "reload-tables", "--approve", clients=_clients(cred),
                     capsys=capsys)  # fmt: skip
    _no_pending(cred)
    assert code == 1 and "failed after 1 of its calls" in out["message"]
    done, rest = out["message"].split("DMS already took ", 1)[1].split("; not sent: ")
    assert done.split(", ") == [f"shop.t{i:02}" for i in range(10)]
    assert rest.split(", ") == ["shop.t10", "shop.t11"]


def test_reload_tables_refuses_a_task_that_is_not_running(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _owned(cred, status="stopped", rows=None)
    code, out = _run(tmp_path, "reload-tables", "--approve", clients=_clients(cred),
                     capsys=capsys)  # fmt: skip
    assert code == 1 and "acts only on a running task" in out["message"]


def test_reload_tables_with_no_errored_table_is_an_error(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _owned(cred, rows=[_row("shop.orders")])
    code, out = _run(tmp_path, "reload-tables", "--approve", clients=_clients(cred),
                     capsys=capsys)  # fmt: skip
    assert code == 1 and "nothing to reload" in out["message"]


def test_reload_tables_refuses_another_jobs_stack(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _stack(cred, job_tag="other-job")
    code, out = _run(tmp_path, "reload-tables", "--approve", clients=_clients(cred),
                     capsys=capsys)  # fmt: skip
    assert code == 1 and "not this job's step 1 DMS stack" in out["message"]


def test_revalidate_reloads_no_data_and_covers_failed_and_suspended_tables_in_batches(
    tmp_path, capsys
):
    _job(tmp_path)
    cred = StubbedCredMgr()
    rows = [_row(f"shop.t{i:02}", validation="Mismatched records", ValidationFailedRecords=1)
            for i in range(11)]  # fmt: skip
    rows += [_row("shop.s", validation="Suspended records", ValidationSuspendedRecords=3),
             _row("shop.ok", validation="Validated")]  # fmt: skip
    _owned(cred, rows=rows)
    for _ in range(2):
        cred.stub("dms").add_response("reload_tables", {"ReplicationTaskArn": TASK},
                                      {"ReplicationTaskArn": TASK, "ReloadOption": "validate-only",
                                       "TablesToReload": ANY})  # fmt: skip
    code, out = _run(tmp_path, "revalidate", "--approve", clients=_clients(cred),
                     capsys=capsys)  # fmt: skip
    _no_pending(cred)
    assert code == 0 and out["status"] == "revalidating" and out["calls"] == 2
    assert len(out["tables_revalidated"]) == 12 and "shop.ok" not in out["tables_revalidated"]


def test_revalidate_refuses_a_task_with_validation_off_or_no_failure(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _owned(cred, rows=None, settings={"ValidationSettings": {"EnableValidation": False}})
    code, out = _run(tmp_path, "revalidate", "--approve", clients=_clients(cred),
                     capsys=capsys)  # fmt: skip
    assert code == 1 and "turns validation off" in out["message"]
    cred = StubbedCredMgr()
    _owned(cred, rows=[_row("shop.orders", validation="Validated")])
    code, out = _run(tmp_path, "revalidate", "--approve", clients=_clients(cred),
                     capsys=capsys)  # fmt: skip
    assert code == 1 and "no table has failed or suspended" in out["message"]


# --- The gate's line --------------------------------------------------------------------------


def _gate(tmp_path, capsys, stats):
    from src.storage.local_store import LocalArtifactStore
    from src.tools.transformation_contract.hashing import canonical_json

    b = make_bundle()
    store = LocalArtifactStore(str(tmp_path))
    store.write_json("shop/job1/data-movement/step-1/plan.json", b.plan.model_dump(mode="json"))
    store.write_bytes(
        "shop/job1/transformation-contract/v2/step-1.json", canonical_json(b.contract)
    )
    cred = StubbedCredMgr()
    g.queue(cred, b.plan.contract.sha256, stats=stats)
    argv = ["--job-id", "job1", "--db", "shop", "--artifact-root", str(tmp_path),
            "--region", g.REGION, "--stack-name", g.STACK]  # fmt: skip
    assert check_dms_step_gate.main(argv, clients=g.clients(cred)) == 0
    return json.loads(capsys.readouterr().out)


def test_a_gate_mismatch_names_dms_failure_table_and_a_lob_hint(tmp_path, capsys):
    out = _gate(tmp_path, capsys, [g.stat("shop.audit_log", rows=12, validation="No primary key"),
                                   g.stat("shop.customers"),
                                   g.stat("shop.orders", validation="Mismatched records", failed=2)])  # fmt: skip
    assert out["status"] == "fail"
    # The same shape as status's table_problems and progress lines.
    assert out["validation_failures"] == [{"table": "shop.orders",
                                           "validation_state": "Mismatched records",
                                           "failed_records": 2, "suspended_records": 0}]  # fmt: skip
    assert out["validation_failures_not_shown"] == 0
    check = out["validation_failures_check"]
    assert check["failures_table"] == "awsdms_control.awsdms_validation_failures_v1"
    assert any("TASK_NAME = 'TASKRESOURCEID01'" in s for s in check["check_on_target"])
    assert len(check["hints"]) == 1 and "LobMaxSize (64 KB" in check["hints"][0]


def test_a_clean_gate_carries_no_validation_failures(tmp_path, capsys):
    out = _gate(tmp_path, capsys, g.ready_stats())
    assert out["validation_failures"] == [] and out["validation_failures_check"] is None


# --- /move ------------------------------------------------------------------------------------


def _prove() -> str:
    text = (script.REPO_ROOT / ".claude" / "commands" / "move.md").read_text()
    prove = text.split("## Stage 4: Prove", 1)[1].split("## Ending the step", 1)[0]
    return " ".join(prove.split())


def test_prove_stops_on_table_errors_and_never_reloads_or_revalidates_under_auto() -> None:
    prove = _prove()
    assert "A `phase` of `table_errors` means DMS could not load a table" in prove
    assert "never keep polling it; go to Table errors below" in prove
    assert "Under `--auto`, never run `reload-tables` and never ask" in prove
    assert "Under `--auto`, never run `revalidate` and never ask" in prove
    assert "`MOVE_RESULT: stopped stage=prove reason=table errors: <the errored tables>`" in prove
    assert "Offer at most one recovery per run (see One recovery per run)" in prove


def test_one_recovery_per_run_is_written_once_and_every_offer_points_at_it() -> None:
    prove = _prove()
    rule = prove.split("**One recovery per run.**", 1)[1].split("**", 1)[0]
    for command in ("`restart-validation`", "`resume`", "`retry`", "`reload-tables`",
                    "`revalidate`"):  # fmt: skip
        assert command in rule, command
    assert "never offer a second one" in rule
    for section in ("**Validation stalled.**", "**Stopped or failed task.**", "**Table errors.**",
                    "**Gate.**"):  # fmt: skip
        body = prove.split(section, 1)[1].split("**", 1)[0]
        assert "One recovery per run" in body, section
    assert prove.count("**One recovery per run.**") == 1


def test_plan_asks_whether_the_target_tables_are_empty_with_a_read_only_query(tmp_path, capsys):
    _job(tmp_path)
    code, out = _run(tmp_path, "plan", capsys=capsys)  # no clients: no AWS call
    assert (
        code == 0
        and "Is every table of the step empty on Aurora?" in out["empty_target"]["question"]
    )
    sql = out["empty_target"]["check_on_target"][-1]
    assert (
        sql.startswith("SELECT ")
        and "UNION ALL" in sql
        and "WHERE EXISTS (SELECT 1 FROM `shop`.`orders`)" in sql
    )
    assert not any(w in sql for w in ("DELETE", "TRUNCATE", "UPDATE", "INSERT"))


def test_move_asks_the_customer_to_confirm_empty_targets_before_starting() -> None:
    text = (script.REPO_ROOT / ".claude" / "commands" / "move.md").read_text()
    move = " ".join(text.split("## Stage 3: Move", 1)[1].split("## Stage 4: Prove", 1)[0].split())
    start = move.split("**5. Start the task.**", 1)[1]
    assert start.index("`empty_target`") < start.index("Start the replication task now?")
    assert "never run it yourself" in start


def test_prove_shows_the_failure_table_sql_for_the_customer_never_runs_it() -> None:
    prove = _prove()
    assert "show every entry of `check_on_target` verbatim" in prove
    assert "read-only SQL the customer runs on Aurora, never you" in prove


@pytest.mark.parametrize("version, table", [("3.6.0", "public.awsdms_validation_failures_v1"),
                                            ("3.6.1", "public.awsdms_validation_failures_v2"),
                                            ("3.6.2", "public.awsdms_validation_failures_v2")])  # fmt: skip
def test_the_v2_failure_table_starts_at_dms_3_6_1(version, table):
    from src.tools.data_movement.table_errors import failures_table

    assert failures_table("postgresql", version) == table


def test_the_empty_target_query_on_postgresql_quotes_identifiers_and_needs_no_dual():
    from src.tools.data_movement.table_errors import empty_target_check

    check = empty_target_check("postgresql", ["public.orders", 'public.we"ird'])
    assert check["check_on_target"][0].startswith("On Aurora PostgreSQL (the target)")
    sql = check["check_on_target"][-1]
    assert sql == ("SELECT 'public.orders' AS non_empty_table WHERE EXISTS (SELECT 1 FROM "
                   '"public"."orders") UNION ALL SELECT \'public.we"ird\' AS non_empty_table '
                   'WHERE EXISTS (SELECT 1 FROM "public"."we""ird");')  # fmt: skip


def test_auto_lets_approve_deploy_stand_for_the_empty_target_confirmation() -> None:
    text = (script.REPO_ROOT / ".claude" / "commands" / "move.md").read_text()
    headless = " ".join(
        text.split("## Headless (`--auto`)", 1)[1].split("## Result line", 1)[0].split()
    )
    assert (
        "`--approve-deploy` also stands for the customer's confirmation that every target table is empty"
        in headless
    )


def test_the_gate_judges_an_aurora_load_by_table_state_never_by_dynamodb_only_counters(
    tmp_path, capsys
):
    # FullLoadErrorRows is "valid only for migrations where DynamoDB is the target".
    out = _gate(tmp_path, capsys, [g.stat("shop.audit_log", rows=12, validation="No primary key"),
                                   g.stat("shop.customers", errors=3),
                                   g.stat("shop.orders")])  # fmt: skip
    assert not any(r.startswith("load: ") and "shop.customers" in r for r in out["reasons"])
    out = _gate(tmp_path / "e", capsys, [g.stat("shop.audit_log", rows=12, validation="No primary key"),
                                         g.stat("shop.customers", state="Table error"),
                                         g.stat("shop.orders")])  # fmt: skip
    assert "load: shop.customers: Table error (the table did not load)" in out["reasons"]
