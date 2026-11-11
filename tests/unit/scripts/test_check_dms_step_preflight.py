# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""scripts/check_dms_step_preflight.py: one JSON status line (#1799)."""

from __future__ import annotations

import json

import pytest

from scripts import check_dms_step_preflight as script
from src.contracts.dms_preflight import CHECK_ORDER
from src.storage.local_store import LocalArtifactStore
from src.tools.transformation_contract.hashing import canonical_json
from tests.unit.tools.aws.aws_stubs import StubbedCredMgr
from tests.unit.tools.data_movement import preflight_stubs as s
from tests.unit.tools.data_movement.dms_step_fixtures import make_bundle, make_step1_inputs

PATH = "shop/job1/data-movement/step-1/preflight.json"


def _argv(tmp_path, *extra):
    return ["--job-id", "job1", "--db", "shop", "--artifact-root", str(tmp_path), "--region", s.REGION,
            "--aurora-cluster-id", s.CLUSTER, "--source-secret-arn", s.SOURCE_SECRET,
            "--target-secret-arn", s.TARGET_SECRET, *extra]  # fmt: skip


def _full(tmp_path, *extra):
    return _argv(tmp_path, "--source-certificate-arn", s.SOURCE_CERT, "--target-certificate-arn", s.TARGET_CERT,
                 "--subnet-ids", ",".join(s.SUBNETS), *extra)  # fmt: skip


def _job(tmp_path):
    b = make_bundle()
    plan = make_step1_inputs(measured=True).plan  # LOB sizes measured: lob_sizes passes (#2123)
    store = LocalArtifactStore(str(tmp_path))
    store.write_json("shop/job1/data-movement/step-1/plan.json", plan.model_dump(mode="json"))
    store.write_bytes(
        "shop/job1/transformation-contract/v2/step-1.json", canonical_json(b.contract)
    )
    return store


def _out(capsys):
    return json.loads(capsys.readouterr().out)


def test_a_run_prints_its_status_and_writes_the_file(tmp_path, capsys):
    store = _job(tmp_path)
    cred = StubbedCredMgr()
    s.queue_ready(cred)
    assert script.main(_full(tmp_path, "--confirm", "source_cdc"), clients=s.clients(cred)) == 0
    out = _out(capsys)
    assert out["status"] == "pass" and out["preflight"] == PATH
    assert out["first_failure"] is None and out["message"] is None and out["to_confirm"] == []
    assert any("--source-kind" in n for n in out["notes"])  # the default source kind is called out
    assert any("443" in n for n in out["notes"])  # a passing row's checklist
    assert store.exists(PATH)


def test_the_old_flag_is_an_alias_for_confirm_source_cdc(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    s.queue_ready(cred)
    assert script.main(_full(tmp_path, "--confirm-source-checklist"), clients=s.clients(cred)) == 0
    assert _out(capsys)["status"] == "pass"


def test_without_a_confirmation_the_run_needs_one(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    s.queue_ready(cred)
    assert script.main(_full(tmp_path), clients=s.clients(cred)) == 0
    out = _out(capsys)
    assert out["status"] == "needs_confirmation" and out["first_failure"] is None and out["message"]


def test_an_unknown_confirm_id_is_one_error_line(tmp_path, capsys):
    _job(tmp_path)
    assert (
        script.main(_full(tmp_path, "--confirm", "nope"), clients=s.clients(StubbedCredMgr())) == 1
    )
    out = _out(capsys)
    assert out["status"] == "error" and "nope" in out["message"] and "source_cdc" in out["message"]


def test_a_failure_names_the_check_and_what_to_do(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    s.queue_aurora(cred, status="pending-reboot")
    assert script.main(_argv(tmp_path), clients=s.clients(cred)) == 0
    out = _out(capsys)
    assert (
        out["status"] == "fail"
        and out["first_failure"] == "aurora_parameters"
        and "Reboot" in out["message"]
    )


def test_every_request_field_has_a_flag(tmp_path, monkeypatch):
    seen = {}

    def fake(store, db, job_id, request, clients, *, confirmed):
        seen["request"], seen["confirmed"] = request, confirmed
        raise FileNotFoundError("stop here")

    monkeypatch.setattr(script, "write_preflight", fake)
    argv = _full(tmp_path, "--source-secret-kms-key-arn", "k1", "--target-secret-kms-key-arn", "k2",
                 "--instance-class", "dms.r6i.large", "--source-kind", "rds_instance", "--source-id", "db1",
                 "--secrets-manager-endpoint-override", "https://sm.example", "--confirm", "source_cdc",
                 "--confirm", "secrets")  # fmt: skip
    assert script.main(argv, clients=s.clients(StubbedCredMgr())) == 1
    r = seen["request"]
    assert (r.source_certificate_arn, r.target_certificate_arn) == (s.SOURCE_CERT, s.TARGET_CERT)
    assert r.subnet_ids == s.SUBNETS and r.secrets_manager_endpoint_override == "https://sm.example"
    assert (r.source_secret_kms_key_arn, r.target_secret_kms_key_arn) == ("k1", "k2")
    assert (r.instance_class, r.source_kind, r.source_id) == (
        "dms.r6i.large",
        "rds_instance",
        "db1",
    )
    assert seen["confirmed"] == {"source_cdc", "secrets"}


def test_a_source_id_is_required_for_an_rds_source(tmp_path, capsys):
    _job(tmp_path)
    assert (
        script.main(
            _argv(tmp_path, "--source-kind", "rds_instance"), clients=s.clients(StubbedCredMgr())
        )
        == 1
    )
    assert "--source-id" in _out(capsys)["message"]


def test_no_step1_plan_is_an_error(tmp_path, capsys):
    assert script.main(_argv(tmp_path), clients=s.clients(StubbedCredMgr())) == 1
    assert _out(capsys)["status"] == "error"


@pytest.mark.parametrize("flag,value", [("--job-id", "../escape"), ("--db", "../escape")])
def test_a_bad_name_is_refused(tmp_path, capsys, flag, value):
    argv = _argv(tmp_path)
    argv[argv.index(flag) + 1] = value
    assert script.main(argv, clients=s.clients(StubbedCredMgr())) == 1
    assert _out(capsys)["status"] == "error"


def test_the_sandbox_refuses_a_path_outside_the_repo(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("MODERNIZER_CI_SANDBOX", "1")
    assert script.main(_argv("/etc"), clients=s.clients(StubbedCredMgr())) == 1
    assert _out(capsys)["status"] == "error"


def test_a_failure_after_a_confirm_row_is_the_first_failure(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    s.queue_aurora(cred)
    s.queue_secrets(cred)
    s.queue_instance_class(cred)
    s.queue_roles(cred)
    rds = cred.stub("rds")
    rds.add_response("describe_db_instances", {"DBInstances": [{"DBInstanceIdentifier": "src1", "Engine": "mysql", "BackupRetentionPeriod": 7,
                     "DBParameterGroups": [{"DBParameterGroupName": "g", "ParameterApplyStatus": "in-sync"}]}]},
                     {"DBInstanceIdentifier": "src1"})  # fmt: skip
    rds.add_response("describe_db_parameters", s.parameters(binlog_format="ROW", binlog_row_image="FULL"),
                     {"DBParameterGroupName": "g"})  # fmt: skip
    argv = _argv(tmp_path, "--source-kind", "rds_instance", "--source-id", "src1", "--source-certificate-arn",
                 s.SOURCE_CERT, "--subnet-ids", ",".join(s.SUBNETS))  # fmt: skip
    s.queue_cert(cred, s.SOURCE_CERT)
    assert script.main(argv, clients=s.clients(cred)) == 0
    out = _out(capsys)
    assert out["status"] == "fail" and out["first_failure"] == "dms_certificates"
    assert "--target-certificate-arn" in out["message"] and out["to_confirm"] == ["source_cdc"]


@pytest.mark.parametrize("check_id", CHECK_ORDER)
def test_confirm_takes_every_check_id(tmp_path, capsys, check_id):
    _job(tmp_path)
    cred = StubbedCredMgr()
    s.queue_ready(cred)
    assert script.main(_full(tmp_path, "--confirm", check_id), clients=s.clients(cred)) == 0
    assert _out(capsys)["status"] != "error"


def test_confirm_repeats(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    s.queue_ready(cred)
    argv = _full(tmp_path, *[a for c in CHECK_ORDER for a in ("--confirm", c)])
    assert script.main(argv, clients=s.clients(cred)) == 0
    assert _out(capsys)["status"] == "pass"


def test_an_unmeasured_mysql_lob_table_is_the_first_failure_before_any_aws_call(tmp_path, capsys):
    # #2123: the plan lists customers.bio (LONGTEXT) unmeasured; nothing is queued, so an AWS
    # call would raise: lob_sizes fails first and the rest are not run.
    b = make_bundle()
    store = LocalArtifactStore(str(tmp_path))
    store.write_json("shop/job1/data-movement/step-1/plan.json", b.plan.model_dump(mode="json"))
    store.write_bytes(
        "shop/job1/transformation-contract/v2/step-1.json", canonical_json(b.contract)
    )
    assert script.main(_full(tmp_path), clients=s.clients(StubbedCredMgr())) == 0
    out = _out(capsys)
    assert out["status"] == "fail" and out["first_failure"] == "lob_sizes"
    assert "lob-sizes.sql" in out["message"] and "run_dms_step.py lob-sizes" in out["message"]
