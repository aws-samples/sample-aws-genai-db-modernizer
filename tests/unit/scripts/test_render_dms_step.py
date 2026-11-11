# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""scripts/render_dms_step.py: renders into the job, one JSON status line (#1798)."""

from __future__ import annotations

import json

import pytest

from scripts import render_dms_step as script
from src.tools.transformation_contract.hashing import canonical_json
from tests.unit.tools.data_movement.dms_step_fixtures import make_bundle, write_job, write_pg_job


def _args(tmp_path, *extra):
    return ["--job-id", "job1", "--db", "shop", "--artifact-root", str(tmp_path), *extra]


def test_render_writes_the_three_files(tmp_path, capsys):
    store = write_job(tmp_path)
    assert script.main(_args(tmp_path)) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "complete" and out["replica_identity_recommended"] == 0
    for name in ("aurora-target.yaml", "step-1.ddl.sql"):
        assert store.exists(f"shop/job1/data-movement/step-1/{name}")
    # customers.bio (LONGTEXT) is unmeasured (#2123): the LOB size script, and no stack yet.
    assert out["lob_sizes_needed"] == 1 and store.exists(
        "shop/job1/data-movement/step-1/lob-sizes.sql"
    )
    assert not store.exists("shop/job1/data-movement/step-1/dms-step-1.yaml")


def test_a_missing_plan_is_an_error_line(tmp_path, capsys):
    assert script.main(_args(tmp_path)) == 1
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "error" and "run_data_movement_plan" in out["message"]


def test_a_bad_job_id_is_refused(tmp_path, capsys):
    assert script.main(["--job-id", "../x", "--db", "shop", "--artifact-root", str(tmp_path)]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_missing_ids_are_an_error_line(capsys):
    assert script.main([]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_bundle_writes_the_inputs(tmp_path, capsys):
    write_job(tmp_path, dates=True)
    target = tmp_path / "input.json"
    assert script.main(_args(tmp_path, "--bundle", str(target))) == 0
    data = json.loads(target.read_text())
    assert set(data) == {"plan", "contract", "ddl", "zero_date_defaults", "identity_columns",
                         "source_objects", "extensions"}  # fmt: skip
    assert data["zero_date_defaults"] == ["shop.orders.created"]
    assert data["source_objects"] == {"procedures": 0, "functions": 0, "triggers": 0, "views": 1}


def test_a_postgresql_bundle_round_trips_its_sequences_and_extensions(tmp_path, capsys):
    """The bundle carries the identity columns, the collection's counts and its extensions,
    so an example renders the same four files as the job (#1905, #1906)."""
    store = write_pg_job(tmp_path, extensions=[{"name": "citext", "version": "1.6"}])
    target = tmp_path / "input.json"
    assert script.main(_args(tmp_path, "--bundle", str(target))) == 0
    data = json.loads(target.read_text())
    assert data["identity_columns"] == [["public.orders", "id"], ["public.tags", "we\"ird'id"]]
    assert data["extensions"] == ["citext"]
    assert script.main(_args(tmp_path)) == 0
    bundle_dir = tmp_path / "ex"
    bundle_dir.mkdir()
    (bundle_dir / "input.json").write_text(target.read_text())
    rendered = script.render_example(bundle_dir)
    assert "step-1.sequences.sql" in rendered
    for name, text in rendered.items():
        assert store.read_bytes(f"shop/job1/data-movement/step-1/{name}").decode() == text, name


def test_the_bundle_round_trips_to_the_same_files(tmp_path, capsys):
    store = write_job(tmp_path, dates=True)
    target = tmp_path / "input.json"
    assert script.main(_args(tmp_path, "--bundle", str(target))) == 0
    assert script.main(_args(tmp_path)) == 0
    bundle_dir = tmp_path / "ex"
    bundle_dir.mkdir()
    (bundle_dir / "input.json").write_text(target.read_text())
    for name, text in script.render_example(bundle_dir).items():
        written = store.read_bytes(f"shop/job1/data-movement/step-1/{name}").decode()
        assert written == text, name


def test_a_changed_contract_is_an_error_line(tmp_path, capsys):
    store = write_job(tmp_path)
    key = "shop/job1/transformation-contract/v2/step-1.json"
    contract = make_bundle().contract.model_copy(update={"wave": 9})
    store.write_bytes(key, canonical_json(contract))
    assert script.main(_args(tmp_path)) == 1
    assert "changed since the plan" in json.loads(capsys.readouterr().out)["message"]


def test_check_without_examples_is_an_error_line(capsys):
    assert script.main(["--check"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_examples_with_a_job_is_an_error_line(capsys):
    assert script.main(["--examples", "--job-id", "job1", "--db", "shop"]) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "error"


def test_examples_rewrite_is_refused_under_the_ci_sandbox(capsys, monkeypatch):
    """#1886: /move's allow rule covers this script; a headless run never rewrites tracked files."""
    monkeypatch.setenv("MODERNIZER_CI_SANDBOX", "1")
    monkeypatch.setattr(script, "_examples", lambda check: pytest.fail("must not render"))
    assert script.main(["--examples"]) == 1
    assert "MODERNIZER_CI_SANDBOX=1" in json.loads(capsys.readouterr().out)["message"]


def test_an_unexpected_failure_is_one_typed_error_line(tmp_path, capsys, monkeypatch):
    write_job(tmp_path)

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(script, "write_step1_delivery", boom)
    assert script.main(_args(tmp_path)) == 1
    assert json.loads(capsys.readouterr().out) == {
        "status": "error",
        "message": "RuntimeError: boom",
    }
