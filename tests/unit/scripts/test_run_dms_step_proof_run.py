# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""scripts/run_dms_step.py proof-target-run: the routine proof on the test cluster (#1945).

CloudFormation and RDS go to botocore Stubbers and the Data API to a fake client: nothing is
sent."""

# ruff: noqa: F811 - cred is the shared fixture, imported, then requested by name
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from scripts import run_dms_step as script
from src.storage.local_store import LocalArtifactStore
from tests.unit.scripts.test_run_dms_step_proof_target import (  # noqa: F401 - the fixture
    REGION,
    REPO_ROOT,
    STACK,
    _cluster,
    _job,
    _resources,
    _restore_aws_profile,
    _run,
    _stack,
    cred,
)
from tests.unit.tools.routines.fake_data_api import FakeDataApi, FakeSecrets

FN = "CREATE OR REPLACE FUNCTION public.f(p integer)\n RETURNS integer\n LANGUAGE sql\nAS $$ SELECT 1 $$"


def _with_routine(tmp_path: Path) -> None:
    """The job gets one routine carried as it is: its artifact and its exact CREATE."""
    from src.agents.collector.routine_sources import write_routine_sources
    from src.contracts.routine_sources import RoutineSource

    store = LocalArtifactStore(base_dir=str(tmp_path))
    collector = store.read_json("shop/job1/collector/output.json")
    source = RoutineSource(kind="function", routine_id="f", schema_name="public", specific_name="f_1",
                           create_statement=FN, origin="pg_get_functiondef")  # fmt: skip
    write_routine_sources(store, "shop", "job1", collector, [source])
    row = {"routine_id": "public.f(integer)", "kind": "function", "schema_name": "public",
           "name": "f", "specific_name": "f_1", "signature": "integer", "returns": "integer",
           "source_text": "exact", "status": "as_is", "deploy_order": 1,
           "dependencies": [{"kind": "table", "name": "public.orders", "found": True}],
           "dependencies_complete": True, "rules_passed": 3}  # fmt: skip
    artifact = {"job_id": "job1", "database_name": "shop", "assignment_version": 2,
                "source_engine": "postgresql", "source_version": "16.4",
                "target_engine": "aurora_postgresql", "target_line": "16", "target_user": "postgres",
                "routine_sources": True, "total": 1,
                "counts": {"as_is": 1, "rewritten": 0, "model_repaired": 0, "needs_attention": 0},
                "routines": [row]}  # fmt: skip
    store.write_json("shop/job1/routines/v2/routine_migration.json", artifact)


def _pg_cluster(cred: Any) -> None:
    _cluster(cred, Engine="aurora-postgresql", EngineVersion="16.15", DatabaseName="routine_proof",
             MasterUsername="proofadmin")  # fmt: skip


def test_proof_target_run_is_refused_under_the_ci_sandbox(capsys, monkeypatch, cred):
    monkeypatch.setenv("MODERNIZER_CI_SANDBOX", "1")
    code, out = _run(REPO_ROOT / "artifacts", "proof-target-run", "--approve", capsys=capsys)
    assert code == 1 and "refused under MODERNIZER_CI_SANDBOX=1" in out.get("message", "")
    assert cred.asked_for == []


def test_proof_target_run_is_refused_under_auto(tmp_path, capsys, cred):
    _job(tmp_path, "postgresql")
    code, out = _run(tmp_path, "proof-target-run", "--approve", "--auto", capsys=capsys)
    assert code == 1 and "never runs under --auto" in out.get("message", "")
    assert cred.asked_for == []


def test_proof_target_run_needs_approve_and_the_question_names_the_stack_and_region(
    tmp_path, capsys, cred
):
    _job(tmp_path, "postgresql")
    code, out = _run(tmp_path, "proof-target-run", capsys=capsys)
    assert code == 1 and "pass --approve only after the customer approved it" in out["message"]
    assert (
        STACK in out["message"] and REGION in out["message"] and "routine proof" in out["message"]
    )
    assert cred.asked_for == []


def test_proof_target_run_proves_the_routines_and_writes_them_into_the_artifact(
    tmp_path, capsys, cred, monkeypatch
):
    _job(tmp_path, "postgresql")
    _with_routine(tmp_path)
    _stack(cred)
    _resources(cred)
    _pg_cluster(cred)
    client = FakeDataApi(calls={'"public"."f"': [1]})
    monkeypatch.setattr(script, "_data_api", lambda _cred: client, raising=False)
    monkeypatch.setattr(script, "_secrets", lambda _cred: FakeSecrets(), raising=False)
    code, out = _run(tmp_path, "proof-target-run", "--approve", capsys=capsys)
    assert code == 0, out
    assert out["cluster_changes"] == {} and out["left_on_cluster"] == []
    assert out["counts"] == {"proved": 1, "needs_attention": 0, "not_proved": 0}
    assert (
        out["routines"][0]["mode"] == "load_and_run" and out["routines"][0]["outcome"] == "proved"
    )
    assert (
        out["target"] == "an Aurora PostgreSQL 16.15 test cluster"
        and out["strict_check"] == "plpgsql_extra_errors"
    )
    assert os.environ["AWS_PROFILE"] == "team-Admin"  # the proof writes to the cluster
    saved = json.loads((tmp_path / "shop/job1/routines/v2/routine_migration.json").read_text())
    assert (
        saved["routines"][0]["proof"]["outcome"] == "proved"
        and saved["proof_run"]["counts"]["proved"] == 1
    )
    assert "SELECT 1" not in json.dumps(saved) and "SELECT 1" not in json.dumps(out)
    assert not client.databases  # every scratch database is gone
    routine = [s for s in client.sent if s.sql.startswith("CREATE OR REPLACE FUNCTION public.f")]
    assert routine and all(s.database.startswith("mdz_proof_") for s in routine)


def test_proof_target_run_refuses_a_cluster_that_is_not_available(
    tmp_path, capsys, cred, monkeypatch
):
    _job(tmp_path, "postgresql")
    _with_routine(tmp_path)
    _stack(cred)
    _resources(cred)
    _cluster(cred, Engine="aurora-postgresql", Status="creating")
    client = FakeDataApi()
    monkeypatch.setattr(script, "_data_api", lambda _cred: client, raising=False)
    code, out = _run(tmp_path, "proof-target-run", "--approve", capsys=capsys)
    assert code == 1 and "deployed and available" in out["message"] and client.sent == []


@pytest.mark.parametrize("engine", ["mysql"])
def test_proof_target_run_refuses_a_cluster_of_another_engine(
    tmp_path, capsys, cred, monkeypatch, engine
):
    _job(tmp_path, "postgresql")
    _with_routine(tmp_path)
    _stack(cred)
    _resources(cred)
    _cluster(cred)  # aurora-mysql
    client, secrets = FakeDataApi(), FakeSecrets()
    monkeypatch.setattr(script, "_data_api", lambda _cred: client, raising=False)
    monkeypatch.setattr(script, "_secrets", lambda _cred: secrets, raising=False)
    code, out = _run(tmp_path, "proof-target-run", "--approve", capsys=capsys)
    assert code == 1 and "aurora-mysql" in out["message"] and client.sent == []
    assert not secrets.secrets


MY_FN = "CREATE DEFINER=`admin`@`%` FUNCTION `f`(`p` int) RETURNS int DETERMINISTIC RETURN p"


def _with_mysql_routine(tmp_path: Path) -> None:
    from src.agents.collector.routine_sources import write_routine_sources
    from src.contracts.routine_sources import RoutineSource

    store = LocalArtifactStore(base_dir=str(tmp_path))
    collector = store.read_json("shop/job1/collector/output.json")
    source = RoutineSource(kind="function", routine_id="f", schema_name="shop", specific_name="f",
                           create_statement=MY_FN, origin="show_create")  # fmt: skip
    write_routine_sources(store, "shop", "job1", collector, [source])
    row = {"routine_id": "shop.f", "kind": "function", "schema_name": "shop", "name": "f",
           "specific_name": "f", "signature": "int", "returns": "int", "source_text": "exact",
           "status": "as_is", "deploy_order": 1, "dependencies_complete": True, "rules_passed": 3}  # fmt: skip
    artifact = {"job_id": "job1", "database_name": "shop", "assignment_version": 2,
                "source_engine": "mysql", "source_version": "8.0.36",
                "target_engine": "aurora_mysql", "target_line": "8.0", "target_user": "admin",
                "routine_sources": True, "total": 1,
                "counts": {"as_is": 1, "rewritten": 0, "model_repaired": 0, "needs_attention": 0},
                "routines": [row]}  # fmt: skip
    store.write_json("shop/job1/routines/v2/routine_migration.json", artifact)


def _mysql(tmp_path, cred, monkeypatch, **fake: Any) -> tuple[FakeDataApi, FakeSecrets]:
    _job(tmp_path)
    _with_mysql_routine(tmp_path)
    _stack(cred)
    _resources(cred)
    _cluster(cred, DatabaseName="routine_proof", MasterUsername="proofadmin")
    client, secrets = FakeDataApi("mysql", **fake), FakeSecrets()
    monkeypatch.setattr(script, "_data_api", lambda _cred: client, raising=False)
    monkeypatch.setattr(script, "_secrets", lambda _cred: secrets, raising=False)
    return client, secrets


def test_the_mysql_proof_runs_as_a_restricted_user_and_removes_it_and_its_secret(
    tmp_path, capsys, cred, monkeypatch
):
    """Review of #2297: the MySQL calls run as a per-run user granted the scratch database
    only, through its own secret tagged with the job id; the user is dropped and the secret
    deleted with no recovery window in the same cleanup as the scratch databases."""
    client, secrets = _mysql(tmp_path, cred, monkeypatch, calls={"`f`(": [1]})
    code, out = _run(tmp_path, "proof-target-run", "--approve", capsys=capsys)
    assert code == 0, out
    assert out["runner"] == "a restricted user granted the scratch database only"
    assert out["counts"]["proved"] == 1 and out["left_on_cluster"] == []
    assert not client.users and not client.databases and not secrets.secrets
    assert len(secrets.deleted) == 1 and secrets.deleted[0][1] is True
    created = next(s for s in client.sent if s.sql.startswith("CREATE DEFINER="))
    assert created.secret.endswith("/mdzr_" + created.secret.rsplit("/mdzr_", 1)[1])
    assert "`mdzr_" in created.sql  # the routine's DEFINER is the restricted user
    assert out["cluster_changes"] == {}


def test_the_mysql_proof_is_refused_while_the_event_scheduler_is_on(
    tmp_path, capsys, cred, monkeypatch
):
    client, secrets = _mysql(tmp_path, cred, monkeypatch, scheduler="ON")
    code, out = _run(tmp_path, "proof-target-run", "--approve", capsys=capsys)
    assert code == 1 and "event_scheduler is ON" in out["message"]
    assert [s.sql for s in client.sent] == ["SELECT @@GLOBAL.event_scheduler"]
    assert not secrets.secrets


def test_a_change_the_run_left_outside_the_scratch_databases_is_reported(
    tmp_path, capsys, cred, monkeypatch
):
    client, _ = _mysql(tmp_path, cred, monkeypatch, calls={"`f`(": [1]})
    real = client.execute_statement

    def leaves_a_database(**kw: Any) -> dict[str, Any]:
        if kw["sql"].startswith("SELECT `f`("):
            client.databases.add("evil")
        got: dict[str, Any] = real(**kw)
        return got

    monkeypatch.setattr(client, "execute_statement", leaves_a_database)
    code, out = _run(tmp_path, "proof-target-run", "--approve", capsys=capsys)
    assert code == 0 and out["status"] == "done"
    assert out["cluster_changes"] == {"databases": {"added": ["evil"], "removed": []}}


def test_the_mysql_proof_stops_when_its_runner_user_cannot_be_made(
    tmp_path, capsys, cred, monkeypatch
):
    """Found on the real cluster: the Data API takes no bound password in CREATE USER. The
    proof never falls back to the master user: it stops, and the secret is deleted."""
    client, secrets = _mysql(tmp_path, cred, monkeypatch, fail_on={"CREATE USER": "42000"})
    code, out = _run(tmp_path, "proof-target-run", "--approve", capsys=capsys)
    assert code == 1 and "could not be created through the RDS Data API" in out["message"]
    assert not secrets.secrets and secrets.deleted and secrets.deleted[0][1] is True
    assert not [s for s in client.sent if s.sql.startswith(("CREATE DATABASE", "CREATE DEFINER"))]
