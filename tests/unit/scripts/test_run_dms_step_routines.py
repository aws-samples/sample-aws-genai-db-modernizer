# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""scripts/run_dms_step.py's routines commands (#1947): ``routines`` writes the scripts the
customer runs, only after the full load; ``routines-check`` reads their check output, with
no AWS call. Every AWS call goes to a botocore Stubber: nothing is sent."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agents.collector.routine_sources import write_routine_sources
from src.storage.local_store import LocalArtifactStore
from src.tools.routines.migration import routine_migration_path
from src.tools.transformation_contract.hashing import canonical_json, contract_sha256
from tests.unit.scripts import test_run_dms_step as base
from tests.unit.tools.aws.aws_stubs import StubbedCredMgr
from tests.unit.tools.data_movement.dms_step_fixtures import make_plan
from tests.unit.tools.data_movement.routine_fixtures import (
    SENTINEL,
    contract_with_routines,
    dep_routine,
    migration,
    mysql_source,
    row,
)

STEP = Path("shop") / "job1" / "data-movement" / "step-1"


def _job(
    tmp_path: Path,
    rows=None,
    *,
    schema: str = "shop",
    target_database: str | None = "shop",
    extra=(),
) -> str:
    rows = rows if rows is not None else [
        row("shop.place_order", "procedure", schema="shop", status="rewritten", signature="IN int",
            specific="place_order")]  # fmt: skip
    artifact = migration(rows, "mysql")
    contract = contract_with_routines(artifact)
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(routine_migration_path("shop", "job1", 2), artifact.model_dump(mode="json"))
    write_routine_sources(store, "shop", "job1", {"metadata": {"source_database": {"engine": "mysql"}}},
                          [mysql_source("place_order", "CREATE DEFINER=`app`@`%` PROCEDURE "
                                        f"`place_order`(IN p INT)\nBEGIN SELECT '{SENTINEL}'; END",
                                        schema=schema), *extra])  # fmt: skip
    store.write_bytes("shop/job1/transformation-contract/v2/step-1.json", canonical_json(contract))
    store.write_json(f"{STEP}/plan.json", make_plan(contract).model_dump(mode="json"))
    inputs = {k: v for k, v in base.INPUTS.items() if k != "target_database_name"}
    if target_database:
        inputs["target_database_name"] = target_database
    (tmp_path / STEP / "inputs.json").write_text(json.dumps(inputs))
    return contract_sha256(contract)


def _loaded(cred, percent):
    base._stack(cred)
    base._connections(cred)
    base._task(cred, "running", percent)
    base._stats(cred)


def test_routines_are_refused_before_the_full_load_is_done(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _loaded(cred, 60)
    code, line = base._run(tmp_path, "routines", clients=base._clients(cred), capsys=capsys)
    assert code == 1 and "after the full load" in line["message"] and "60%" in line["message"]
    assert not (tmp_path / STEP / "routines-deploy.sql").exists()


def test_routines_after_the_full_load_write_the_scripts_and_print_no_routine_text(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _loaded(cred, 100)
    code, line = base._run(tmp_path, "routines", clients=base._clients(cred), capsys=capsys)
    assert code == 0, line
    assert (line["status"], line["carried"], line["in_script"], line["label"]) == (
        "ready", 1, 1, "checked, not load-tested")  # fmt: skip
    assert SENTINEL in (tmp_path / STEP / "routines-deploy.sql").read_text()
    assert SENTINEL not in json.dumps(line)


def test_nothing_to_deploy_makes_no_aws_call(tmp_path, capsys):
    _job(
        tmp_path,
        [row("shop.q", "procedure", schema="shop", status="needs_attention", reasons=["x"])],
    )
    code, line = base._run(
        tmp_path, "routines", clients=base._clients(StubbedCredMgr()), capsys=capsys
    )
    assert (code, line["status"], line["not_carried_routines"][0]["routine_id"]) == (
        0, "nothing_to_deploy", "shop.q")  # fmt: skip


def _output(sha: str, lines: list[list[str]]) -> str:
    header = ["modernizer-routines-check", "1", "aurora_mysql", sha, "shop", "", ""]
    end = ["modernizer-routines-check-end", "", "", "", "", "", ""]
    return "\n".join("\t".join(x) for x in [header, *lines, end]) + "\n"


def test_routines_check_reads_the_output_and_writes_the_result_with_no_aws_call(tmp_path, capsys):
    sha = _job(tmp_path)
    code, line = base._run(tmp_path, "routines-check", capsys=capsys)
    assert (code, line["status"]) == (0, "no_output")
    (tmp_path / STEP / "routines-check.tsv").write_text(
        _output(sha, [["procedure", "shop", "", "place_order", "IN int", "", ""]]))  # fmt: skip
    code, line = base._run(tmp_path, "routines-check", capsys=capsys)
    assert (code, line["status"], line["confirmed"]) == (0, "pass", 1)
    assert json.loads((tmp_path / STEP / "routines-check.json").read_text())["status"] == "pass"


def test_a_refused_output_removes_the_earlier_result(tmp_path, capsys):
    sha = _job(tmp_path)
    tsv = tmp_path / STEP / "routines-check.tsv"
    tsv.write_text(_output(sha, [["procedure", "shop", "", "place_order", "IN int", "", ""]]))
    base._run(tmp_path, "routines-check", capsys=capsys)
    tsv.write_text(_output(sha, [["procedure", "shop", "", "someone_elses", "", "", ""]]))
    code, line = base._run(tmp_path, "routines-check", capsys=capsys)
    assert code == 1 and "refused" in line["message"] and "someone_elses" not in line["message"]
    assert not (tmp_path / STEP / "routines-check.json").exists()


def test_a_case_only_database_clash_says_why_in_the_routines_and_check_output(tmp_path, capsys):
    # The routine's database `Shop` differs from the moved tables' `shop` only by case.
    rows = [row("Shop.place_order", "procedure", schema="Shop", status="rewritten",
                signature="IN int", specific="place_order")]  # fmt: skip
    sha = _job(tmp_path, rows, schema="Shop", target_database=None)
    cred = StubbedCredMgr()
    _loaded(cred, 100)
    code, line = base._run(tmp_path, "routines", clients=base._clients(cred), capsys=capsys)
    assert code == 0, line
    (refused,) = line["refused_routines"]
    assert (refused["routine_id"], refused["reason"]) == (
        "Shop.place_order",
        "database_case_differs",
    )
    assert "only by case" in refused["why"] and "create these routines by hand" in refused["why"]
    header = ["modernizer-routines-check", "1", "aurora_mysql", sha, "", "", ""]
    end = ["modernizer-routines-check-end", "", "", "", "", "", ""]
    (tmp_path / STEP / "routines-check.tsv").write_text(
        "\n".join("\t".join(x) for x in [header, end]) + "\n")  # fmt: skip
    code, line = base._run(tmp_path, "routines-check", capsys=capsys)
    assert (code, line["status"]) == (0, "fail")
    (missing,) = line["mismatches"]
    assert (missing["routine_id"], missing["problem"]) == ("Shop.place_order", "missing")
    assert missing["why"] == refused["why"]


@pytest.mark.passes_on_main
def test_a_missing_routine_with_no_case_clash_has_no_why(tmp_path, capsys):
    sha = _job(tmp_path)
    (tmp_path / STEP / "routines-check.tsv").write_text(_output(sha, []))
    code, line = base._run(tmp_path, "routines-check", capsys=capsys)
    assert (code, line["status"], line["mismatches"][0]["problem"]) == (0, "fail", "missing")
    assert "why" not in line["mismatches"][0]


def test_a_routine_left_out_for_depending_on_a_case_clash_says_why_too(tmp_path, capsys):
    # shop.total is in the tables' database but calls Shop.place_order, left out for the clash:
    # it clears with the same fix, so its lines say so.
    rows = [row("Shop.place_order", "procedure", schema="Shop", status="rewritten",
                signature="IN int", specific="place_order"),
            row("shop.total", "procedure", schema="shop", status="rewritten", signature="IN int",
                specific="total", dependencies=[dep_routine("procedure", "Shop.place_order")])]  # fmt: skip
    sha = _job(tmp_path, rows, schema="Shop", target_database=None,
               extra=[mysql_source("total", "CREATE DEFINER=`app`@`%` PROCEDURE `total`(IN p INT)\n"
                                   "BEGIN CALL `Shop`.place_order(p); END")])  # fmt: skip
    cred = StubbedCredMgr()
    _loaded(cred, 100)
    code, line = base._run(tmp_path, "routines", clients=base._clients(cred), capsys=capsys)
    assert code == 0, line
    why = {r["routine_id"]: (r["reason"], r.get("why")) for r in line["refused_routines"]}
    assert why["shop.total"] == ("depends_on_refused", why["Shop.place_order"][1])
    assert why["shop.total"][1]
    header = ["modernizer-routines-check", "1", "aurora_mysql", sha, "", "", ""]
    end = ["modernizer-routines-check-end", "", "", "", "", "", ""]
    (tmp_path / STEP / "routines-check.tsv").write_text(
        "\n".join("\t".join(x) for x in [header, end]) + "\n")  # fmt: skip
    code, line = base._run(tmp_path, "routines-check", capsys=capsys)
    got = {m["routine_id"]: (m["problem"], m.get("why")) for m in line["mismatches"]}
    assert got["shop.total"] == ("missing", why["Shop.place_order"][1])
