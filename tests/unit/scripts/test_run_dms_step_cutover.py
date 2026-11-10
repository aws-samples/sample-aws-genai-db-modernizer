# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""scripts/run_dms_step.py's cutover commands (#1948): ``cutover-scripts`` writes the script
the customer runs at their own cutover, and ``cutover-check`` reads its check output; neither
makes an AWS call, runs a script or connects to a database."""

from __future__ import annotations

import json

from tests.unit.scripts import test_run_dms_step as base
from tests.unit.scripts.test_run_dms_step_routines import STEP, _job
from tests.unit.tools.data_movement.routine_fixtures import SENTINEL, row


def _rows():
    return [
        row("shop.place_order", "procedure", schema="shop", status="rewritten", signature="IN int",
            specific="place_order"),
        row("shop.orders_bi", "trigger", schema="shop", table="shop.orders", specific="orders.orders_bi"),
        row("shop.py", "procedure", schema="shop", status="needs_attention", reasons=["x"]),
        row("shop.orders_au", "trigger", schema="shop", table="shop.orders", specific="orders.orders_au",
            dependencies=[{"kind": "routine", "name": "py", "routine_kind": "procedure", "found": True,
                           "routines": [{"kind": "procedure", "routine_id": "shop.py"}]}]),
    ]  # fmt: skip


def _output(sha: str, lines: list[list[str]]) -> str:
    header = ["modernizer-cutover-check", "1", "aurora_mysql", sha, "shop", "", ""]
    end = ["modernizer-cutover-check-end", "", "", "", "", "", ""]
    return "\n".join("\t".join(x) for x in [header, *lines, end]) + "\n"


def test_cutover_scripts_write_the_files_with_no_aws_call_and_name_what_is_left_out(
    tmp_path, capsys
):
    _job(tmp_path, _rows())
    # No clients: a LOCAL command never builds one, so any AWS call would fail the run.
    code, line = base._run(tmp_path, "cutover-scripts", capsys=capsys)
    assert code == 0, line
    assert (line["status"], line["expected"], line["in_script"]) == ("ready", 1, 0)
    assert {(r["routine_id"], r["reason"]) for r in line["excluded_routines"]} == {
        ("shop.orders_au", "depends_on_not_carried"), ("shop.orders_bi", "source_missing")}  # fmt: skip
    assert "never runs it" in line["runs_it"]
    assert set(line["files"]) == {"cutover-routines.sql", "cutover-check.sql", "cutover.md"}
    assert (tmp_path / STEP / "cutover.md").is_file()
    assert SENTINEL not in json.dumps(line)


def test_nothing_to_do_writes_no_file(tmp_path, capsys):
    _job(tmp_path)
    code, line = base._run(tmp_path, "cutover-scripts", capsys=capsys)
    assert (code, line["status"], line["expected"]) == (0, "nothing_to_do", 0)
    assert not (tmp_path / STEP / "cutover-routines.sql").exists()


def test_cutover_check_reads_the_output_and_writes_the_result_with_no_aws_call(tmp_path, capsys):
    sha = _job(tmp_path, _rows())
    code, line = base._run(tmp_path, "cutover-check", capsys=capsys)
    assert (code, line["status"]) == (0, "no_output")
    (tmp_path / STEP / "cutover-check.tsv").write_text(
        _output(sha, [["trigger", "shop", "orders", "orders_bi", "", "", ""]]))  # fmt: skip
    code, line = base._run(tmp_path, "cutover-check", capsys=capsys)
    # orders_bi is there; orders_au is not carried, so the check fails, naming it.
    assert (code, line["status"], line["confirmed"], line["not_carried"]) == (0, "fail", 1, 1)
    assert line["not_carried_routines"][0]["routine_id"] == "shop.orders_au"
    written = json.loads((tmp_path / STEP / "cutover-check.json").read_text())
    assert (written["status"], written["expected"]) == ("fail", 1)


def test_a_refused_output_removes_the_earlier_result(tmp_path, capsys):
    sha = _job(tmp_path, _rows())
    tsv = tmp_path / STEP / "cutover-check.tsv"
    tsv.write_text(_output(sha, [["trigger", "shop", "orders", "orders_bi", "", "", ""]]))
    base._run(tmp_path, "cutover-check", capsys=capsys)
    assert (tmp_path / STEP / "cutover-check.json").exists()
    tsv.write_text(_output(sha, [["trigger", "shop", "orders", "someone_elses", "", "", ""]]))
    code, line = base._run(tmp_path, "cutover-check", capsys=capsys)
    assert code == 1 and "refused" in line["message"] and "someone_elses" not in line["message"]
    assert not (tmp_path / STEP / "cutover-check.json").exists()


def test_resume_needs_the_check_output_and_reads_it_strictly(tmp_path, capsys):
    sha = _job(tmp_path, _rows())
    code, line = base._run(tmp_path, "cutover-scripts", "--resume", capsys=capsys)
    assert code == 1 and "run cutover-check.sql" in line["message"]
    tsv = tmp_path / STEP / "cutover-check.tsv"
    tsv.write_text(_output(sha, [["trigger", "shop", "orders", f"x{SENTINEL}", "", "", ""]]))
    code, line = base._run(tmp_path, "cutover-scripts", "--resume", capsys=capsys)
    assert code == 1 and "refused" in line["message"] and SENTINEL not in line["message"]
    tsv.write_text(_output(sha, [["trigger", "shop", "orders", "orders_bi", "", "", ""]]))
    code, line = base._run(tmp_path, "cutover-scripts", "--resume", capsys=capsys)
    assert (code, line["status"], line["already_there"], line["in_script"]) == (0, "ready", 0, 0)
    assert list(line["files"]) == ["cutover-resume.sql"]


def test_cutover_check_prints_and_writes_no_routine_text(tmp_path, capsys):
    sha = _job(tmp_path, _rows())
    (tmp_path / STEP / "cutover-check.tsv").write_text(_output(sha, []))
    code, line = base._run(tmp_path, "cutover-check", capsys=capsys)
    assert code == 0 and line["mismatch_count"] == 1
    assert SENTINEL not in json.dumps(line)
    assert SENTINEL not in (tmp_path / STEP / "cutover-check.json").read_text()
