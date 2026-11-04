# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""scripts/check_dms_step_gate.py: one JSON status line with the cutover message (#1800)."""

from __future__ import annotations

import json
from pathlib import Path

from scripts import check_dms_step_gate as script
from src.shared.cutover_message import cutover_message_text
from src.storage.local_store import LocalArtifactStore
from src.tools.transformation_contract.hashing import canonical_json
from tests.unit.tools.aws.aws_stubs import StubbedCredMgr
from tests.unit.tools.data_movement import gate_stubs as g
from tests.unit.tools.data_movement.dms_step_fixtures import make_bundle


def _job(tmp_path):
    b = make_bundle()
    store = LocalArtifactStore(str(tmp_path))
    store.write_json("shop/job1/data-movement/step-1/plan.json", b.plan.model_dump(mode="json"))
    store.write_bytes(
        "shop/job1/transformation-contract/v2/step-1.json", canonical_json(b.contract)
    )
    return b


def _argv(tmp_path, *extra):
    return ["--job-id", "job1", "--db", "shop", "--artifact-root", str(tmp_path), "--region", g.REGION,
            "--stack-name", g.STACK, *extra]  # fmt: skip


def test_the_status_line_carries_the_decision_and_the_message(tmp_path, capsys):
    b = _job(tmp_path)
    cred = StubbedCredMgr()
    g.queue(cred, b.plan.contract.sha256, progress=70)
    assert script.main(_argv(tmp_path), clients=g.clients(cred)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "pending" and out["cutover_decision"] == "customer"
    assert (
        out["cutover_message"] == cutover_message_text()
        and out["contract_sha256"] == b.plan.contract.sha256
    )
    assert any("70%" in r for r in out["reasons"])


def test_a_window_outside_the_bounds_is_refused(tmp_path, capsys):
    _job(tmp_path)
    assert (
        script.main(_argv(tmp_path, "--window-minutes", "0"), clients=g.clients(StubbedCredMgr()))
        == 1
    )
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_a_stack_without_the_task_output_is_an_error(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    cred.stub("cloudformation").add_response("describe_stacks", {"Stacks": [{
        "StackName": g.STACK, "CreationTime": g.NOW, "StackStatus": "CREATE_COMPLETE", "Outputs": []}]})  # fmt: skip
    assert script.main(_argv(tmp_path), clients=g.clients(cred)) == 1
    assert "ReplicationTaskArn" in json.loads(capsys.readouterr().out)["message"]


def test_reasons_carry_their_check_and_exceptions_are_listed(tmp_path, capsys):
    b = _job(tmp_path)
    cred = StubbedCredMgr()
    g.queue(cred, b.plan.contract.sha256, progress=70)
    script.main(_argv(tmp_path), clients=g.clients(cred))
    out = json.loads(capsys.readouterr().out)
    assert any(r.startswith("load: ") and "70%" in r for r in out["reasons"])
    assert all(r.split(": ", 1)[0] in ("load", "lag", "parity") for r in out["reasons"])
    assert {"table": "shop.audit_log", "kind": "table"} in out["exceptions"]


def test_stale_seconds_is_passed_to_the_lag_check_and_bounded(tmp_path, capsys):
    b = _job(tmp_path)
    cred = StubbedCredMgr()
    g.queue(cred, b.plan.contract.sha256, age_minutes=3)  # newest datapoint 240 s old
    assert script.main(_argv(tmp_path, "--stale-seconds", "200"), clients=g.clients(cred)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "pending" and any(
        r.startswith("lag: ") and "200 s" in r for r in out["reasons"]
    )
    assert (
        script.main(_argv(tmp_path, "--stale-seconds", "0"), clients=g.clients(StubbedCredMgr()))
        == 1
    )
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_the_readme_quotes_the_cutover_message_exactly():
    readme = Path(
        script.REPO_ROOT, "infrastructure/cloudformation/data-movement/README.md"
    ).read_text()
    assert cutover_message_text() in readme


def test_a_fail_result_still_exits_zero(tmp_path, capsys):
    b = _job(tmp_path)
    cred = StubbedCredMgr()
    g.queue(cred, b.plan.contract.sha256, status="stopped", errored=1)
    assert script.main(_argv(tmp_path), clients=g.clients(cred)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "fail" and any(r.startswith("load: ") for r in out["reasons"])
