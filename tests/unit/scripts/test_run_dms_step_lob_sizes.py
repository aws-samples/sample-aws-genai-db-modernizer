# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``run_dms_step.py lob-sizes`` (#2123): no AWS call and no database connection; the
customer's ``lob-sizes.tsv`` is read strictly, the measured sizes go into the plan and the
stack is written again; skipping the measurement is the customer's explicit choice only."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

import pytest

from src.contracts.lob_sizes import LobSizes
from src.shared.catalog.dms_sizing import dms_sizing
from src.shared.contract_versioning import load_contract
from src.storage.local_store import LocalArtifactStore
from src.tools.data_movement.dms_preflight import check_lob_sizes
from src.tools.data_movement.dms_step import (
    load_step1,
    lob_measure_columns,
    table_mappings,
    task_settings,
    write_step1_delivery,
)
from src.tools.data_movement.lob_sizes import END, MAGIC, layout_sha
from tests.unit.scripts.test_run_dms_step import INPUTS, _run
from tests.unit.tools.data_movement.dms_step_fixtures import write_job

SENTINEL = "s3cr3t-v4lue"


@pytest.fixture(autouse=True)
def _no_aws(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("lob-sizes made an AWS call")

    monkeypatch.setattr("src.tools.aws.credentials.AWSCredentialManager", refuse)


def _step(tmp_path: Path) -> Path:
    return tmp_path / "shop" / "job1" / "data-movement" / "step-1"


def _job(tmp_path: Path, **inputs) -> LocalArtifactStore:
    """A MySQL job whose ``customers.bio`` LONGTEXT the collection did not measure."""
    store = write_job(tmp_path)
    write_step1_delivery(store, "shop", "job1")
    (_step(tmp_path) / "inputs.json").write_text(json.dumps({**INPUTS, **inputs}))
    return store


def _request(lob_sizes: Literal["measure", "skip"] = "measure"):
    from src.tools.data_movement.dms_preflight import PreflightRequest

    return PreflightRequest(region="us-east-1", aurora_cluster_id="c", source_secret_arn="s",
                            target_secret_arn="t", lob_sizes=lob_sizes)  # fmt: skip


def _step1(store):
    return load_step1(store, "shop", "job1", with_ddl=False)


def _tsv(store, longest: int) -> str:
    step = _step1(store)
    cols = lob_measure_columns(step)
    lines = [f"{MAGIC}\t1\t{step.contract_sha256}\t{layout_sha(cols)}\t2026-11-11T10:00:00.000000"]
    lines += [f"column\t{c.n}\t{c.k}\t{longest}\t3" for c in cols]
    return "\n".join([*lines, f"{END}\t\t\t\t"]) + "\n"


def test_an_unmeasured_mysql_table_stops_preflight_and_step_1_writes_the_script(tmp_path):
    store = _job(tmp_path)
    assert (_step(tmp_path) / "lob-sizes.sql").is_file() and (
        _step(tmp_path) / "lob-sizes.md"
    ).is_file()
    row = check_lob_sizes(_step1(store), None, None, {})
    assert row.status == "fail" and "shop.customers" in row.summary
    [t] = [t for t in _step1(store).plan.replication.lob_tables if t.table == "shop.customers"]
    assert t.size_source == "unmeasured"


def test_the_customers_output_sizes_the_tables_and_writes_the_stack_again(tmp_path, capsys):
    store = _job(tmp_path)
    (_step(tmp_path) / "lob-sizes.tsv").write_text(_tsv(store, 79_934))
    code, line = _run(tmp_path, "lob-sizes", capsys=capsys)
    assert code == 0, line
    # 79,934 bytes at 2 bytes per byte, 1.25 times: 196 KB, so 256 KB in MySQL's 64 KB steps
    # (never rounded up to 1 MB, which the instance would pay for per LOB column).
    assert line["status"] == "measured" and line["unmeasured_tables"] == 0
    assert {"table": "shop.customers", "max_size_kb": 256, "size_source": "measured"} in line[
        "tables"
    ]
    step = _step1(store)
    assert check_lob_sizes(step, None, None, {}).status == "pass"
    target = task_settings(step)["TargetMetadata"]
    # The only LOB table: the task runs limited at its measured size, with no rule of its own.
    assert (target["FullLobMode"], target["LimitedSizeLobMode"], target["LobMaxSize"]) == (
        False,
        True,
        256,
    )
    assert [r for r in table_mappings(step)["rules"] if r["rule-type"] == "table-settings"] == []
    # The script is gone (nothing left to measure); the stack on disk is the new one.
    assert not (_step(tmp_path) / "lob-sizes.sql").exists()
    assert '"LimitedSizeLobMode": true' in (_step(tmp_path) / "dms-step-1.yaml").read_text()
    saved = load_contract(
        LobSizes,
        store.read_json("shop/job1/data-movement/step-1/lob-sizes.json"),
        artifact="lob sizes",
    )
    assert saved.choice == "measured" and [
        (c.table, c.column, c.max_value_bytes) for c in saved.columns
    ] == [("shop.customers", "bio", 79_934)]
    assert any("LOB sizes measured by lob-sizes.sql" in n for n in step.plan.notes)
    # A rerun has nothing left to measure.
    again = _run(tmp_path, "lob-sizes", capsys=capsys)[1]
    assert again["status"] == "measured" and again["unmeasured_tables"] == 0


def test_a_measured_table_above_the_task_size_gets_its_own_limited_rule(tmp_path, capsys):
    store = _job(tmp_path)
    plan_key = "shop/job1/data-movement/step-1/plan.json"
    (_step(tmp_path) / "lob-sizes.tsv").write_text(_tsv(store, 3_000_000))
    assert _run(tmp_path, "lob-sizes", capsys=capsys)[1]["status"] == "measured"
    rep = store.read_json(plan_key)["replication"]
    sizes = {t["table"]: t["max_size_kb"] for t in rep["lob_tables"]}
    assert sizes["shop.customers"] == 7360
    assert rep["sizing"]["instance_class"] != "dms.t3.medium"  # sized for the 8 MB limit


def test_skip_runs_unmeasured_tables_at_one_mb_with_a_warning(tmp_path, capsys):
    store = _job(tmp_path, lob_sizes="skip")
    code, line = _run(tmp_path, "lob-sizes", capsys=capsys)
    assert code == 0, line
    kb = dms_sizing().lob.skipped_max_kb
    assert line["status"] == "skipped" and kb == 1024
    assert {"table": "shop.customers", "max_size_kb": kb, "size_source": "skipped"} in line[
        "tables"
    ]
    assert (
        "truncated" in line["warning"]
        and "validation" in line["warning"]
        and "memory" in line["warning"]
    )
    step = _step1(store)
    row = check_lob_sizes(step, _request("skip"), None, {})
    assert row.status == "pass" and "by your choice" in row.summary and "truncated" in row.summary
    assert any(n.startswith("LOB sizes not measured, by your choice") for n in step.plan.notes)
    # A 1 MB task-wide limit needs more than dms.t3.medium's 64 KB.
    assert step.plan.replication.sizing.instance_class == "dms.r6i.large"
    assert step.plan.replication.lob_max_size_kb == kb


def test_without_the_explicit_choice_nothing_is_skipped(tmp_path, capsys):
    store = _job(tmp_path)  # no "lob_sizes" key, no output file
    code, line = _run(tmp_path, "lob-sizes", capsys=capsys)
    assert (
        code == 0 and line["status"] == "no_output" and line["expected"].endswith("lob-sizes.tsv")
    )
    assert check_lob_sizes(_step1(store), None, None, {}).status == "fail"
    assert not store.exists("shop/job1/data-movement/step-1/lob-sizes.json")


def test_a_bad_output_file_is_refused_whole_and_never_echoed(tmp_path, capsys):
    store = _job(tmp_path)
    good = _tsv(store, 79_934).splitlines()
    good[1] = f"column\t1\t1\t{SENTINEL}\t3"
    (_step(tmp_path) / "lob-sizes.tsv").write_text("\n".join(good) + "\n")
    code, line = _run(tmp_path, "lob-sizes", capsys=capsys)
    assert code == 1 and line["status"] == "error" and "refused" in line["message"]
    assert "line 2" in line["message"] and SENTINEL not in line["message"]
    assert not store.exists("shop/job1/data-movement/step-1/lob-sizes.json")
    assert check_lob_sizes(_step1(store), None, None, {}).status == "fail"


def test_an_output_for_another_plan_is_refused(tmp_path, capsys):
    store = _job(tmp_path)
    text = _tsv(store, 10).replace(layout_sha(lob_measure_columns(_step1(store))), "0" * 64)
    (_step(tmp_path) / "lob-sizes.tsv").write_text(text)
    code, line = _run(tmp_path, "lob-sizes", capsys=capsys)
    assert code == 1 and "other tables or columns" in line["message"]


def test_measuring_after_a_skip_drops_the_skip(tmp_path, capsys):
    store = _job(tmp_path, lob_sizes="skip")
    assert _run(tmp_path, "lob-sizes", capsys=capsys)[1]["status"] == "skipped"
    (_step(tmp_path) / "inputs.json").write_text(json.dumps(INPUTS))
    # No output file yet: the skip is gone and the tables need measuring again.
    assert _run(tmp_path, "lob-sizes", capsys=capsys)[1]["status"] == "no_output"
    assert check_lob_sizes(_step1(store), None, None, {}).status == "fail"


def test_the_measurement_survives_a_new_plan(tmp_path, capsys):
    from src.tools.data_movement.plan import write_plans

    store = _job(tmp_path)
    (_step(tmp_path) / "lob-sizes.tsv").write_text(_tsv(store, 79_934))
    assert _run(tmp_path, "lob-sizes", capsys=capsys)[1]["status"] == "measured"
    write_plans(store, "shop", "job1")  # Prepare run again
    assert check_lob_sizes(_step1(store), None, None, {}).status == "pass"


def test_taking_the_skip_out_of_the_inputs_makes_the_tables_unmeasured_again(tmp_path, capsys):
    # The review's scenario: skip, then take "lob_sizes" out of inputs.json and write the plan
    # again (Prepare): lob-sizes.json still records the skip, but the inputs decide.
    from src.tools.data_movement.plan import write_plans

    store = _job(tmp_path, lob_sizes="skip")
    assert _run(tmp_path, "lob-sizes", capsys=capsys)[1]["status"] == "skipped"
    assert check_lob_sizes(_step1(store), _request("skip"), None, {}).status == "pass"
    (_step(tmp_path) / "inputs.json").write_text(json.dumps(INPUTS))
    # The plan written under the skip, with the inputs taken back: preflight refuses it.
    stale = check_lob_sizes(_step1(store), _request(), None, {})
    assert stale.status == "fail" and "no longer says" in stale.summary
    assert store.read_json("shop/job1/data-movement/step-1/lob-sizes.json")["choice"] == "skipped"
    write_plans(store, "shop", "job1")  # Prepare again: the inputs no longer skip
    [t] = [t for t in _step1(store).plan.replication.lob_tables if t.table == "shop.customers"]
    assert t.size_source == "unmeasured"
    assert check_lob_sizes(_step1(store), _request(), None, {}).status == "fail"


def test_measuring_after_a_skip_takes_one_run_when_the_output_is_there(tmp_path, capsys):
    store = _job(tmp_path, lob_sizes="skip")
    assert _run(tmp_path, "lob-sizes", capsys=capsys)[1]["status"] == "skipped"
    (_step(tmp_path) / "inputs.json").write_text(json.dumps(INPUTS))
    # The output of the script as it reads once the skip is gone (the same columns).
    cols_store = write_job(tmp_path / "fresh")
    (_step(tmp_path) / "lob-sizes.tsv").write_text(_tsv(cols_store, 79_934))
    line = _run(tmp_path, "lob-sizes", capsys=capsys)[1]
    assert line["status"] == "measured" and "warning" not in line
    assert check_lob_sizes(_step1(store), _request(), None, {}).status == "pass"


def test_plan_and_deploy_refuse_while_a_mysql_lob_table_is_unmeasured(tmp_path, capsys):
    _job(tmp_path)
    # No stack is written with a guessed size, and plan and deploy say why (no AWS call).
    assert not (_step(tmp_path) / "dms-step-1.yaml").exists()
    for command in ("plan", "deploy"):
        extra = ("--approve",) if command == "deploy" else ()
        code, line = _run(tmp_path, command, *extra, capsys=capsys)
        assert (
            code == 1 and "not measured" in line["message"] and "lob-sizes" in line["message"]
        ), command


def test_plan_refuses_a_skipped_plan_once_the_inputs_take_the_skip_back(tmp_path, capsys):
    store = _job(tmp_path, lob_sizes="skip")
    assert _run(tmp_path, "lob-sizes", capsys=capsys)[1]["status"] == "skipped"
    assert _run(tmp_path, "plan", capsys=capsys)[0] == 0
    (_step(tmp_path) / "inputs.json").write_text(json.dumps(INPUTS))
    code, line = _run(tmp_path, "plan", capsys=capsys)
    assert code == 1 and "no longer says" in line["message"]
    assert store.exists("shop/job1/data-movement/step-1/dms-step-1.yaml")  # the old one, refused
