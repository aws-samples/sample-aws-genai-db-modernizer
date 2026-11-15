# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""scripts/run_dms_step.py's views commands (#2082): ``views`` writes the scripts the
customer runs, only after the full load; ``views-check`` reads their check output, with no
AWS call. Every AWS call goes to a botocore Stubber: nothing is sent."""

from __future__ import annotations

import json
from pathlib import Path

from src.agents.collector.routine_sources import write_routine_sources
from src.storage.local_store import LocalArtifactStore
from src.tools.transformation_contract.hashing import canonical_json, contract_sha256
from tests.unit.scripts import test_run_dms_step as base
from tests.unit.scripts.test_run_dms_step_routines import _loaded
from tests.unit.tools.aws.aws_stubs import StubbedCredMgr
from tests.unit.tools.data_movement.dms_step_fixtures import make_plan
from tests.unit.tools.data_movement.view_fixtures import (
    SENTINEL,
    contract_with_views,
    mysql_view_source,
    view,
)

STEP = Path("shop") / "job1" / "data-movement" / "step-1"
_TEXT = ("CREATE OR REPLACE SQL SECURITY DEFINER VIEW `shop`.`v_orders` (`id`) AS\n"
         f"select `shop`.`orders`.`id` AS `id` from `shop`.`orders` where 'x' <> '{SENTINEL}';")  # fmt: skip


def _job(tmp_path: Path, rows=None, *, sources=True) -> str:
    rows = rows if rows is not None else [
        view("v_orders", engine="aurora_mysql"),
        view("v_far", engine="aurora_mysql", tables=["shop.gone"]),
    ]  # fmt: skip
    contract = contract_with_views(rows, "aurora_mysql")
    store = LocalArtifactStore(base_dir=str(tmp_path))
    if sources:
        write_routine_sources(store, "shop", "job1", {"metadata": {"source_database": {"engine": "mysql"}}},
                              [mysql_view_source("v_orders", _TEXT)])  # fmt: skip
    store.write_bytes("shop/job1/transformation-contract/v2/step-1.json", canonical_json(contract))
    store.write_json(f"{STEP}/plan.json", make_plan(contract).model_dump(mode="json"))
    (tmp_path / STEP / "inputs.json").write_text(json.dumps(base.INPUTS))
    return contract_sha256(contract)


def test_views_are_refused_before_the_full_load_is_done(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _loaded(cred, 60)
    code, line = base._run(tmp_path, "views", clients=base._clients(cred), capsys=capsys)
    assert code == 1 and "after the full load" in line["message"] and "60%" in line["message"]
    assert not (tmp_path / STEP / "views-deploy.sql").exists()


def test_views_after_the_full_load_write_the_scripts_and_print_no_view_text(tmp_path, capsys):
    _job(tmp_path)
    cred = StubbedCredMgr()
    _loaded(cred, 100)
    code, line = base._run(tmp_path, "views", clients=base._clients(cred), capsys=capsys)
    assert code == 0, line
    assert (line["status"], line["carried"], line["in_script"], line["not_carried"]) == (
        "ready",
        1,
        1,
        1,
    )
    (far,) = line["not_carried_views"]
    assert (far["view_id"], far["reasons"]) == ("v_far", ["depends_on_table_not_in_step"])
    assert "create the view yourself" in far["why"]
    assert "before routines-deploy.sql" in line["runs_it"]
    assert SENTINEL in (tmp_path / STEP / "views-deploy.sql").read_text()
    assert SENTINEL not in json.dumps(line)


def test_a_view_refused_from_the_script_says_why(tmp_path, capsys):
    _job(tmp_path, sources=False)
    cred = StubbedCredMgr()
    _loaded(cred, 100)
    code, line = base._run(tmp_path, "views", clients=base._clients(cred), capsys=capsys)
    assert code == 0, line
    assert line["refused_views"] == [{"view_id": "v_orders", "reason": "source_missing",
                                      "why": line["refused_views"][0]["why"]}]  # fmt: skip
    assert "collect again" in line["refused_views"][0]["why"]


def test_nothing_to_deploy_makes_no_aws_call(tmp_path, capsys):
    _job(tmp_path, [view("v_far", engine="aurora_mysql", tables=["shop.gone"])])
    code, line = base._run(
        tmp_path, "views", clients=base._clients(StubbedCredMgr()), capsys=capsys
    )
    assert (code, line["status"], line["not_carried_views"][0]["view_id"]) == (
        0, "nothing_to_deploy", "v_far")  # fmt: skip


def _output(sha: str, lines: list[list[str]]) -> str:
    header = ["modernizer-views-check", "1", "aurora_mysql", sha, "", "", ""]
    end = ["modernizer-views-check-end", "", "", "", "", "", ""]
    return "\n".join("\t".join(x) for x in [header, *lines, end]) + "\n"


def test_views_check_reads_the_output_and_writes_the_result_with_no_aws_call(tmp_path, capsys):
    sha = _job(tmp_path)
    code, line = base._run(tmp_path, "views-check", capsys=capsys)
    assert (code, line["status"]) == (0, "no_output")
    (tmp_path / STEP / "views-check.tsv").write_text(
        _output(sha, [["view", "shop", "v_orders", "1", "id", "bigint", "VIEW"]]))  # fmt: skip
    code, line = base._run(tmp_path, "views-check", capsys=capsys)
    assert code == 0, line
    assert (line["status"], line["confirmed"], line["carried"], line["not_carried"]) == (
        "pass",
        1,
        1,
        1,
    )
    saved = json.loads((tmp_path / STEP / "views-check.json").read_text())
    assert saved["status"] == "pass"
    # A refused file removes the earlier result and never repeats the file's content.
    (tmp_path / STEP / "views-check.tsv").write_text(
        _output(sha, [["view", "shop", "hidden_secret", "1", "id", "bigint", "VIEW"]]))  # fmt: skip
    code, line = base._run(tmp_path, "views-check", capsys=capsys)
    assert code == 1 and "refused" in line["message"] and "hidden_secret" not in line["message"]
    assert not (tmp_path / STEP / "views-check.json").exists()
