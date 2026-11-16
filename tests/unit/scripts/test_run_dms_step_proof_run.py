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


def _mysql(tmp_path, cred, monkeypatch, secrets: FakeSecrets | None = None,
           **fake: Any) -> tuple[FakeDataApi, FakeSecrets]:  # fmt: skip
    """A MySQL job. The fake Data API rejects a bound CREATE USER password, as the real one
    does, and signs in with a runner's secret only when the user's hash matches it."""
    _job(tmp_path)
    _with_mysql_routine(tmp_path)
    _stack(cred)
    _resources(cred)
    _cluster(cred, DatabaseName="routine_proof", MasterUsername="proofadmin")
    secrets = secrets or FakeSecrets()
    client = FakeDataApi("mysql", secrets=secrets, **fake)
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
    """#2298: when the Data API cannot sign in as the new runner user, the proof never falls
    back to the master user: it stops, the user is dropped and the secret is deleted."""
    client, secrets = _mysql(tmp_path, cred, monkeypatch, sign_in_fails=True)
    code, out = _run(tmp_path, "proof-target-run", "--approve", capsys=capsys)
    assert code == 1 and "could not create that user or sign in as it" in out["message"]
    assert "its secret was deleted" in out["message"] and out["left_on_cluster"] == []
    assert out["sqlstate"] == "28000" and not client.users
    assert not secrets.secrets and secrets.deleted and secrets.deleted[0][1] is True
    assert not [s for s in client.sent if s.sql.startswith(("CREATE DATABASE", "CREATE DEFINER"))]


def test_the_refusal_says_when_the_runner_secret_could_not_be_deleted(
    tmp_path, capsys, cred, monkeypatch
):
    """Review round 2 of #2297: the refusal never claims a deletion that failed; what is
    left is named in ``left_on_cluster``."""
    _, secrets = _mysql(tmp_path, cred, monkeypatch, secrets=FakeSecrets(delete_fails=True),
                        sign_in_fails=True)  # fmt: skip
    code, out = _run(tmp_path, "proof-target-run", "--approve", capsys=capsys)
    assert code == 1 and "its secret was deleted" not in out["message"]
    assert "could not be deleted" in out["message"]
    assert out["left_on_cluster"] and all("secret" in x for x in out["left_on_cluster"])
    assert secrets.secrets  # it is still there, and the line says so


def test_a_killed_runs_runner_user_and_secret_are_swept(tmp_path, capsys, cred, monkeypatch):
    """Review round 2 of #2297: a run killed between the runner's creation and its drop
    leaves the user and its secret; the next run sweeps both by the job's prefixes, and
    nothing of another job."""
    from src.tools.routines import data_api_target as dat

    stale = dat.runner_prefix("job1") + "0f0f0f0f"
    other_job = dat.runner_prefix("job2") + "0f0f0f0f"
    secrets = FakeSecrets()
    secrets.create_secret(Name=dat.runner_secret_name("job1", stale), SecretString="{}")
    secrets.create_secret(Name=dat.runner_secret_name("job2", other_job), SecretString="{}")
    client, _ = _mysql(tmp_path, cred, monkeypatch, secrets=secrets, calls={"`f`(": [1]})
    client.users |= {stale, other_job}
    code, out = _run(tmp_path, "proof-target-run", "--approve", capsys=capsys)
    assert code == 0, out
    assert stale not in client.users and other_job in client.users
    assert [kw["Name"] for kw in secrets.secrets.values()] == [
        dat.runner_secret_name("job2", other_job)]  # fmt: skip
    assert out["left_on_cluster"] == []


def test_run_proof_runs_a_mysql_job_as_the_hash_created_runner(tmp_path):
    """#2298: the library no longer refuses a MySQL job: its runner is created from a hash,
    the password never in SQL text, and everything it made is removed."""
    from src.tools.routines.proof_run import run_proof

    _job(tmp_path)
    _with_mysql_routine(tmp_path)
    secrets = FakeSecrets()
    client = FakeDataApi("mysql", secrets=secrets, calls={"`f`(": [1]})
    cluster = {"engine": "aurora-mysql", "data_api": True, "arn": "arn:c",
               "master_user_secret_arn": "arn:s", "engine_version": "8.0.mysql_aurora.3.10.0"}  # fmt: skip  # pragma: allowlist secret
    out = run_proof(LocalArtifactStore(base_dir=str(tmp_path)), "shop", "job1", data_api=client,
                    cluster=cluster, secrets_client=secrets)  # fmt: skip
    assert out.get("reason_code") is None and out["status"] == "proved", out
    assert out["runner"] == "a restricted user granted the scratch database only"
    create = next(s for s in client.sent if s.sql.startswith("CREATE USER"))
    assert "IDENTIFIED WITH caching_sha2_password AS '$A$005$" in create.sql
    assert not create.parameters and "IDENTIFIED BY" not in create.sql
    assert not client.users and not client.databases and not secrets.secrets


def test_a_killed_runs_secret_the_sweep_cannot_delete_is_named(tmp_path, capsys, cred, monkeypatch):
    from src.tools.routines import data_api_target as dat

    stale = dat.runner_prefix("job1") + "0f0f0f0f"
    secrets = FakeSecrets(delete_fails=True)
    secrets.create_secret(Name=dat.runner_secret_name("job1", stale), SecretString="{}")
    _mysql(tmp_path, cred, monkeypatch, secrets=secrets, sign_in_fails=True)
    code, out = _run(tmp_path, "proof-target-run", "--approve", capsys=capsys)
    assert code == 1
    assert f"secret {dat.runner_secret_name('job1', stale)}" in out["left_on_cluster"]
