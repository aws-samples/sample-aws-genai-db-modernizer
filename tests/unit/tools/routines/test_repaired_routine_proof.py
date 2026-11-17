# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The test cluster runs a model-repaired routine side by side (#2384): the original on the
disposable source engine, the repair's exact text, rebuilt in memory from the routines file
and ``routine_repair.json``, on the cluster; and the repair loop proves its rounds there with
``--proof test-cluster``. Every Data API call goes to :class:`FakeDataApi`: nothing is sent."""

# ruff: noqa: F811 - cred is the shared fixture, imported, then requested by name
from __future__ import annotations

import json
import sys
from typing import Any

import pytest

from src.contracts.routine_repair import RoutineRepairContract
from src.shared.routine_facts import repair_sha256
from src.storage.local_store import LocalArtifactStore
from src.tools.data_movement.routine_test_gate import held_routines
from src.tools.routines.migration import read_routine_migration
from tests.unit.agents.routine_repair import repair_fixtures as fx
from tests.unit.scripts import test_run_dms_step_proof_target as _proof_target
from tests.unit.tools.routines import repaired_fixtures as rf
from tests.unit.tools.routines.fake_data_api import FakeDataApi, FakeSecrets, runner_for

cred = _proof_target.cred  # the shared fixture

ARNS: dict[str, Any] = {"cluster_arn": "arn:aws:rds:us-east-1:111122223333:cluster:c",
        "secret_arn": "arn:aws:secretsmanager:us-east-1:111122223333:secret:s"}  # fmt: skip  # pragma: allowlist secret
CLUSTER = {"engine": "aurora-postgresql", "data_api": True, "arn": ARNS["cluster_arn"],
           "master_user_secret_arn": ARNS["secret_arn"], "engine_version": "16.15",
           "database_name": "routine_proof"}  # fmt: skip
CALLS = {'"public"."wal_pos"': [1], '"public"."ok"': [1]}
REPAIRED_SQL = "pg_current_wal_lsn()::text || 'LiteralMark1940'"


def _source_side(api: FakeDataApi) -> Any:
    """The disposable source engine's side, as a factory (a fake here)."""
    from src.tools.routines import data_api_target as dat

    runner = runner_for(api, **ARNS)
    return dat.data_api_target(api, **ARNS, engine="postgresql", version="9.6", job_id=rf.JOB,
                               admin_database="routine_proof", runner=runner)  # fmt: skip


def _sent(api: FakeDataApi) -> str:
    return "\n".join(s.sql for s in api.sent)


def _prove(store, cluster_api: FakeDataApi, source_api: FakeDataApi) -> dict[str, Any]:
    from src.tools.routines.proof_run import run_proof

    return run_proof(store, rf.DB, rf.JOB, data_api=cluster_api, cluster=CLUSTER,
                     secrets_client=cluster_api.secrets_store,
                     source_factory=lambda _e, _v: _source_side(source_api))  # fmt: skip


def test_the_test_runs_the_rebuilt_repair_on_the_cluster_and_the_original_on_the_source(tmp_path):
    store = rf.job(tmp_path)
    cluster_api = FakeDataApi(secrets=FakeSecrets(), calls=CALLS)
    source_api = FakeDataApi(calls=CALLS)
    out = _prove(store, cluster_api, source_api)
    assert out["status"] == "proved", out
    (wal,) = [r for r in out["routines"] if r["routine_id"] == rf.WAL]
    assert (wal["status"], wal["mode"], wal["outcome"]) == (
        "model_repaired",
        "side_by_side",
        "proved",
    )
    assert REPAIRED_SQL in _sent(cluster_api) and "pg_current_xlog_location" not in _sent(
        cluster_api
    )
    assert "pg_current_xlog_location()::text || 'LiteralMark1940'" in _sent(source_api)
    artifact = read_routine_migration(store, rf.DB, rf.JOB, 1)
    assert artifact is not None
    (row,) = [r for r in artifact.routines if r.routine_id == rf.WAL]
    assert row.status == "needs_attention"  # the artifact's own verdict is never rewritten
    assert row.proof is not None and row.proof.repair_sha256 == repair_sha256(fx.WAL_REPAIRED)
    # The rebuilt text is in no artifact: only a digest of the redacted candidate.
    assert REPAIRED_SQL not in json.dumps(artifact.model_dump(mode="json"))
    assert held_routines(store, rf.DB, rf.JOB, artifact) == {}  # step 1 deploys both


def test_a_repair_that_does_not_rebuild_is_not_tested_and_stays_held(tmp_path):
    store = rf.job(tmp_path)
    rf.write_repair(store, fx.WAL_REDACTED.replace("SELECT ", "SELECT  "))  # fails its checks
    cluster_api = FakeDataApi(secrets=FakeSecrets(), calls=CALLS)
    out = _prove(store, cluster_api, FakeDataApi(calls=CALLS))
    (wal,) = [r for r in out["routines"] if r["routine_id"] == rf.WAL]
    assert wal["outcome"] == "not_proved" and "does not rebuild" in wal["reason"]
    assert "wal_pos" not in _sent(cluster_api)
    artifact = read_routine_migration(store, rf.DB, rf.JOB, 1)
    assert artifact is not None
    assert held_routines(store, rf.DB, rf.JOB, artifact) == {rf.WAL_KEY: "not_tested"}


# --------------------------------------------------------------------------- the repair loop


@pytest.fixture
def model(monkeypatch):
    from src.agents.routine_repair import handler
    from src.contracts.routine_repair import RoutineRepairResponse

    answer = {"repairs": [{"kind": "function", "routine_id": fx.WAL,
                           "repaired_create": fx.WAL_REPAIRED, "change_note": rf.NOTE}]}  # fmt: skip
    monkeypatch.setattr(handler, "_ask_bedrock",
                        lambda _req: RoutineRepairResponse.model_validate(answer))  # fmt: skip


def _cluster_options(cluster_api: FakeDataApi, source_api: FakeDataApi) -> dict[str, Any]:
    from src.tools.routines import data_api_target as dat

    runner = runner_for(cluster_api, **ARNS, job_id=rf.JOB)
    return {
        "target_factory": dat.data_api_target(cluster_api, **ARNS, engine="postgresql",
                                              version="16.15", job_id=rf.JOB,
                                              admin_database="routine_proof", runner=runner),
        "target_words": "an Aurora PostgreSQL 16.15 test cluster",
        "engine_factory": lambda _e, _m: _source_side(source_api),
        "engine_available": lambda _e, _m: True,
    }  # fmt: skip


def test_the_repair_loop_proves_its_rounds_on_the_test_cluster(tmp_path, model):
    from src.agents.routine_repair.handler import run_routine_repair

    store = rf.job(tmp_path, repaired=False)
    cluster_api = FakeDataApi(secrets=FakeSecrets(), calls=CALLS)
    source_api = FakeDataApi(calls=CALLS)
    status = run_routine_repair(store, rf.DB, rf.JOB, llm_mode="bedrock", proof="test-cluster",
                                proof_options=_cluster_options(cluster_api, source_api))  # fmt: skip
    result = RoutineRepairContract.model_validate(store.read_json(status["artifact"]))
    (row,) = [r for r in result.routines if r.routine_id == fx.WAL]
    assert (row.state, result.proof) == ("model_repaired", "test-cluster")
    assert row.proof_source == (
        "a disposable postgresql 9 engine and an Aurora PostgreSQL 16.15 test cluster"
    )
    assert REPAIRED_SQL in _sent(cluster_api) and REPAIRED_SQL not in _sent(source_api)
    assert "LiteralMark1940" not in json.dumps(result.model_dump(mode="json"))


def test_without_an_open_test_cluster_the_repair_waits(tmp_path, model):
    from src.agents.routine_repair.handler import run_routine_repair

    store = rf.job(tmp_path, repaired=False)
    status = run_routine_repair(store, rf.DB, rf.JOB, llm_mode="bedrock", proof="test-cluster")
    result = RoutineRepairContract.model_validate(store.read_json(status["artifact"]))
    (row,) = [r for r in result.routines if r.routine_id == fx.WAL]
    assert row.state == "repaired_pending_proof" and row.attempts[-1].code == "no_test_cluster"


def test_a_finalize_keeps_the_test_cluster_only_when_it_names_it(tmp_path, monkeypatch):
    from src.agents.routine_repair import loop

    seen: list[str] = []
    real = loop._verifier
    monkeypatch.setattr(loop, "_verifier",
                        lambda store, m, refs, proof, options: seen.append(proof)
                        or real(store, m, refs, proof, options))  # fmt: skip
    store = rf.job(tmp_path, repaired=False)
    status = loop.run_repair_loop(store, rf.DB, rf.JOB, llm_mode="external", proof="test-cluster")
    answer = {"repairs": [{"kind": "function", "routine_id": fx.WAL,
                           "repaired_create": fx.WAL_REPAIRED, "change_note": rf.NOTE}]}  # fmt: skip
    store.write_json(status["llm_response"], answer)
    done = loop.finalize_repair_loop(store, rf.DB, rf.JOB, proof="local")  # another target
    assert seen == ["test-cluster", "none"] and done["proof"] == "none"


# --------------------------------------------------------------------------- the cluster session


def test_the_cluster_session_runs_as_a_runner_and_leaves_nothing_behind(tmp_path):
    from src.tools.routines.proof_run import cluster_session

    store = rf.job(tmp_path, repaired=False)
    migration = read_routine_migration(store, rf.DB, rf.JOB, 1)
    secrets = FakeSecrets()
    api = FakeDataApi(secrets=secrets, calls=CALLS)
    with cluster_session(migration, data_api=api, cluster=CLUSTER, secrets_client=secrets,
                         job_id=rf.JOB) as opened:  # fmt: skip
        assert opened["status"] == "open", opened
        options = opened["options"]
        assert options["target_words"] == "an Aurora PostgreSQL 16.15 test cluster"
        assert api.roles  # the session's runner
        target = options["target_factory"]()
        target.close()
    assert not api.roles and not api.databases and not secrets.secrets
    wrong = {**CLUSTER, "engine": "aurora-mysql"}
    with cluster_session(migration, data_api=api, cluster=wrong, secrets_client=secrets,
                         job_id=rf.JOB) as refused:  # fmt: skip
        assert refused["status"] == "error" and "aurora-mysql" in refused["message"]


# --------------------------------------------------------------------------- the script


def _main(monkeypatch, capsys, tmp_path, *args: str) -> tuple[int, dict[str, Any]]:
    from scripts import run_routine_repair as script

    monkeypatch.setattr(sys, "argv", ["run_routine_repair.py", "--job-id", rf.JOB, "--db", rf.DB,
                                      "--artifact-root", str(tmp_path), *args])  # fmt: skip
    code = 0
    try:
        script.main()
    except SystemExit as exc:
        code = int(exc.code or 0)
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_the_script_refuses_the_test_cluster_in_a_ci_sandbox(tmp_path, monkeypatch):
    """Under the CI sandbox the test cluster is never opened, whatever --approve says."""
    import argparse

    from scripts import run_dms_step, run_routine_repair

    store = rf.job(tmp_path, repaired=False)
    called: list[bool] = []
    monkeypatch.setattr(run_dms_step, "run_on_test_cluster", lambda **_k: called.append(True))
    monkeypatch.setenv("MODERNIZER_CI_SANDBOX", "1")
    args = argparse.Namespace(llm_mode="external", finalize=False, approve=True, inputs=None,
                              artifact_root=str(tmp_path), db=rf.DB, job_id=rf.JOB,
                              assignment_version=None)  # fmt: skip
    line = run_routine_repair._on_test_cluster(args, store, lambda *_a: {"status": "ran"})
    assert line["status"] == "error" and "refused under MODERNIZER_CI_SANDBOX=1" in line["message"]
    assert not called


def test_the_script_needs_approve_and_a_deployed_cluster(tmp_path, monkeypatch, capsys):
    """Without --approve it is refused before any AWS call, with the repair question."""
    from src.tools.data_movement import proof_target as pt

    rf.job(tmp_path, repaired=False)
    inputs = tmp_path / rf.DB / rf.JOB / pt.INPUTS_DIR / "inputs.json"
    inputs.parent.mkdir(parents=True, exist_ok=True)
    inputs.write_text(json.dumps({"region": "us-east-1", "vpc_id": "vpc-0123456789abcdef0",
                                  "subnet_ids": ["subnet-0123456789abcdef0",
                                                 "subnet-0123456789abcdef1"]}))  # fmt: skip
    monkeypatch.delenv("MODERNIZER_CI_SANDBOX", raising=False)
    code, line = _main(monkeypatch, capsys, tmp_path, "--llm-mode", "external",
                       "--proof", "test-cluster")  # fmt: skip
    assert code == 1 and "pass --approve only after the customer approved it" in line["message"]
    assert "Run the routine repair rounds against the test cluster" in line["message"]


def test_the_script_runs_the_loop_on_the_cluster_the_status_reads(tmp_path, monkeypatch, capsys):
    """The test cluster's refusals passed (run_on_test_cluster is the proof-target gate): the
    loop runs inside the cluster session, which cleans up when the run ends."""
    from scripts import run_dms_step

    rf.job(tmp_path, repaired=False)
    secrets = FakeSecrets()
    api = FakeDataApi(secrets=secrets, calls=CALLS)

    def gate(**kw: Any) -> dict[str, Any]:
        assert kw["approve"] is True
        out: dict[str, Any] = kw["on_cluster"](
            cluster=CLUSTER, data_api=api, secrets_client=secrets
        )
        return out

    monkeypatch.setattr(run_dms_step, "run_on_test_cluster", gate)
    monkeypatch.delenv("MODERNIZER_CI_SANDBOX", raising=False)
    code, line = _main(monkeypatch, capsys, tmp_path, "--llm-mode", "external",
                       "--proof", "test-cluster", "--approve")  # fmt: skip
    assert code == 0 and line["status"] == "awaiting_llm" and line["proof"] == "test-cluster"
    assert not api.roles and not api.databases and not secrets.secrets
    assert LocalArtifactStore(base_dir=str(tmp_path)).exists(line["llm_request"])


def test_the_plan_names_the_routines_to_repair_and_the_repair_question(tmp_path, capsys, cred):
    """``proof-target-plan`` (read-only) gives /test-routines what to offer before the test:
    how many routines may get a repair, and the question to ask before the rounds."""
    pt = _proof_target
    REGION, STACK, _job, _run, _versions = pt.REGION, pt.STACK, pt._job, pt._run, pt._versions

    _job(tmp_path)
    _versions(cred)
    code, out = _run(tmp_path, "proof-target-plan", capsys=capsys)
    assert code == 0, out
    assert out["repairable"] == 0  # this job has no routine-migration artifact
    question = out["repair_question"]
    assert question.startswith("Run the routine repair rounds against the test cluster")
    assert STACK in question and REGION in question and cred.asked_for == ["rds"]
    store = rf.job(tmp_path / "repairs", repaired=False)
    from src.agents.routine_repair.handler import repairable_routines

    assert repairable_routines(store, rf.DB, rf.JOB) >= 1  # wal_pos at least
