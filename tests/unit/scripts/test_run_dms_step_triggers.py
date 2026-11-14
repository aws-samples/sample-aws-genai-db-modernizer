# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""scripts/run_dms_step.py's triggers-enable commands (#1948, renamed in #2113):
``triggers-scripts`` writes the script the customer runs to turn on the triggers and events
step 1 left off, and ``triggers-check`` reads its check output; neither
makes an AWS call, runs a script or connects to a database."""

from __future__ import annotations

import json

import pytest

from scripts import run_dms_step as script
from scripts.run_dms_step import LEGACY_COMMANDS
from src.tools.data_movement.routine_scripts import CHECK_MAGIC
from src.tools.data_movement.triggers_scripts import LEGACY_CHECK, TRIGGERS_CHECK
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
    header = ["modernizer-triggers-enable-check", "1", "aurora_mysql", sha, "shop", "", ""]
    end = ["modernizer-triggers-enable-check-end", "", "", "", "", "", ""]
    return "\n".join("\t".join(x) for x in [header, *lines, end]) + "\n"


def test_triggers_scripts_write_the_files_with_no_aws_call_and_name_what_is_left_out(
    tmp_path, capsys
):
    _job(tmp_path, _rows())
    # No clients: a LOCAL command never builds one, so any AWS call would fail the run.
    code, line = base._run(tmp_path, "triggers-scripts", capsys=capsys)
    assert code == 0, line
    assert (line["status"], line["expected"], line["in_script"]) == ("ready", 1, 0)
    assert {(r["routine_id"], r["reason"]) for r in line["excluded_routines"]} == {
        ("shop.orders_au", "depends_on_not_carried"), ("shop.orders_bi", "source_missing")}  # fmt: skip
    assert "never runs it" in line["runs_it"]
    assert set(line["files"]) == {"triggers-enable.sql", "triggers-enable-check.sql", "triggers.md"}
    assert (tmp_path / STEP / "triggers.md").is_file()
    assert SENTINEL not in json.dumps(line)


def test_nothing_to_do_writes_no_file(tmp_path, capsys):
    _job(tmp_path)
    code, line = base._run(tmp_path, "triggers-scripts", capsys=capsys)
    assert (code, line["status"], line["expected"]) == (0, "nothing_to_do", 0)
    assert not (tmp_path / STEP / "triggers-enable.sql").exists()


def test_triggers_check_reads_the_output_and_writes_the_result_with_no_aws_call(tmp_path, capsys):
    sha = _job(tmp_path, _rows())
    code, line = base._run(tmp_path, "triggers-check", capsys=capsys)
    assert (code, line["status"]) == (0, "no_output")
    (tmp_path / STEP / "triggers-enable-check.tsv").write_text(
        _output(sha, [["trigger", "shop", "orders", "orders_bi", "", "", ""]]))  # fmt: skip
    code, line = base._run(tmp_path, "triggers-check", capsys=capsys)
    # orders_bi is there; orders_au is not carried, so the check fails, naming it.
    assert (code, line["status"], line["confirmed"], line["not_carried"]) == (0, "fail", 1, 1)
    assert line["not_carried_routines"][0]["routine_id"] == "shop.orders_au"
    written = json.loads((tmp_path / STEP / "triggers-enable-check.json").read_text())
    assert (written["status"], written["expected"]) == ("fail", 1)


def test_a_refused_output_removes_the_earlier_result(tmp_path, capsys):
    sha = _job(tmp_path, _rows())
    tsv = tmp_path / STEP / "triggers-enable-check.tsv"
    tsv.write_text(_output(sha, [["trigger", "shop", "orders", "orders_bi", "", "", ""]]))
    base._run(tmp_path, "triggers-check", capsys=capsys)
    assert (tmp_path / STEP / "triggers-enable-check.json").exists()
    tsv.write_text(_output(sha, [["trigger", "shop", "orders", "someone_elses", "", "", ""]]))
    code, line = base._run(tmp_path, "triggers-check", capsys=capsys)
    assert code == 1 and "refused" in line["message"] and "someone_elses" not in line["message"]
    assert not (tmp_path / STEP / "triggers-enable-check.json").exists()


def test_resume_needs_the_check_output_and_reads_it_strictly(tmp_path, capsys):
    sha = _job(tmp_path, _rows())
    code, line = base._run(tmp_path, "triggers-scripts", "--resume", capsys=capsys)
    assert code == 1 and "run triggers-enable-check.sql" in line["message"]
    tsv = tmp_path / STEP / "triggers-enable-check.tsv"
    tsv.write_text(_output(sha, [["trigger", "shop", "orders", f"x{SENTINEL}", "", "", ""]]))
    code, line = base._run(tmp_path, "triggers-scripts", "--resume", capsys=capsys)
    assert code == 1 and "refused" in line["message"] and SENTINEL not in line["message"]
    tsv.write_text(_output(sha, [["trigger", "shop", "orders", "orders_bi", "", "", ""]]))
    code, line = base._run(tmp_path, "triggers-scripts", "--resume", capsys=capsys)
    assert (code, line["status"], line["already_there"], line["in_script"]) == (0, "ready", 0, 0)
    assert list(line["files"]) == ["triggers-enable-resume.sql"]


def test_triggers_check_prints_and_writes_no_routine_text(tmp_path, capsys):
    sha = _job(tmp_path, _rows())
    (tmp_path / STEP / "triggers-enable-check.tsv").write_text(_output(sha, []))
    code, line = base._run(tmp_path, "triggers-check", capsys=capsys)
    assert code == 0 and line["mismatch_count"] == 1
    assert SENTINEL not in json.dumps(line)
    assert SENTINEL not in (tmp_path / STEP / "triggers-enable-check.json").read_text()


# --- The names #1948 shipped (#2113): hidden aliases, so saved instructions and old jobs work


def _legacy_output(sha: str, lines: list[list[str]]) -> str:
    """The check output a script generated before #2113 prints: its old header and end lines."""
    header = [LEGACY_CHECK.magic, "1", "aurora_mysql", sha, "shop", "", ""]
    end = [LEGACY_CHECK.end, "", "", "", "", "", ""]
    return "\n".join("\t".join(x) for x in [header, *lines, end]) + "\n"


def test_the_old_subcommand_names_run_the_renamed_commands(tmp_path, capsys):
    assert LEGACY_COMMANDS == {"cutover-scripts": "triggers-scripts",  # legacy alias
                               "cutover-check": "triggers-check"}  # legacy alias  # fmt: skip
    sha = _job(tmp_path, _rows())
    for old, new in LEGACY_COMMANDS.items():
        assert base._run(tmp_path, old, capsys=capsys) == base._run(tmp_path, new, capsys=capsys)
    (tmp_path / STEP / "triggers-enable-check.tsv").write_text(
        _output(sha, [["trigger", "shop", "orders", "orders_bi", "", "", ""]]))  # fmt: skip
    code, line = base._run(tmp_path, "triggers-check", capsys=capsys)
    assert (code, line["status"], line["confirmed"]) == (0, "fail", 1)


def test_the_old_subcommand_names_are_not_listed_in_the_help(capsys):
    with pytest.raises(SystemExit):
        script.main(["--help"])
    shown = capsys.readouterr().out
    assert "triggers-scripts" in shown and "triggers-check" in shown
    assert not any(old in shown for old in LEGACY_COMMANDS)


def test_an_output_an_old_script_printed_under_the_old_name_is_still_read(tmp_path, capsys):
    """A job from before #2113: the customer ran the old check script and saved its output
    under the old name, as the old instructions said."""
    sha = _job(tmp_path, _rows())
    old = tmp_path / STEP / LEGACY_CHECK.output
    old.write_text(_legacy_output(sha, [["trigger", "shop", "orders", "orders_bi", "", "", ""]]))
    code, line = base._run(tmp_path, "triggers-check", capsys=capsys)
    assert (code, line["status"], line["confirmed"], line["not_carried"]) == (0, "fail", 1, 1)
    assert (tmp_path / STEP / "triggers-enable-check.json").is_file()
    code, line = base._run(tmp_path, "triggers-scripts", "--resume", capsys=capsys)
    assert (code, line["status"], line["already_there"]) == (0, "ready", 0)
    # Once the new output is there, it is the one read; the old name is never read again.
    (tmp_path / STEP / "triggers-enable-check.tsv").write_text(
        _output(sha, [["trigger", "shop", "orders", "someone_elses", "", "", ""]]))  # fmt: skip
    code, line = base._run(tmp_path, "triggers-check", capsys=capsys)
    assert code == 1 and "triggers-enable-check.tsv: refused" in line["message"]


def test_an_old_header_under_the_new_name_is_read_and_a_foreign_header_is_refused(tmp_path, capsys):
    sha = _job(tmp_path, _rows())
    tsv = tmp_path / STEP / "triggers-enable-check.tsv"
    tsv.write_text(_legacy_output(sha, [["trigger", "shop", "orders", "orders_bi", "", "", ""]]))
    code, line = base._run(tmp_path, "triggers-check", capsys=capsys)
    assert (code, line["confirmed"]) == (0, 1)
    routines = _output(sha, []).replace(TRIGGERS_CHECK.magic, CHECK_MAGIC)
    tsv.write_text(routines)
    code, line = base._run(tmp_path, "triggers-check", capsys=capsys)
    assert code == 1 and "refused" in line["message"]


def test_no_status_line_or_result_says_cutover(tmp_path, capsys):
    """#2113: what the routine and triggers-enable commands print and write names no code
    with "cutover" in it; a MySQL trigger already on the target is
    ``present_before_triggers_enable``."""
    from tests.unit.scripts.test_run_dms_step_routines import _output as routines_output
    from tests.unit.tools.aws.aws_stubs import StubbedCredMgr

    sha = _job(tmp_path, _rows())
    cred = StubbedCredMgr()
    base._stack(cred)
    base._connections(cred)
    base._task(cred, "running", 100)
    base._stats(cred)
    present = [["procedure", "shop", "", "place_order", "IN int", "", ""],
               ["trigger", "shop", "orders", "orders_bi", "", "", ""]]  # fmt: skip
    (tmp_path / STEP / "routines-check.tsv").write_text(routines_output(sha, present))
    (tmp_path / STEP / "triggers-enable-check.tsv").write_text(_output(sha, present[1:]))
    printed = []
    for command, clients in (("routines", base._clients(cred)), ("routines-check", None),
                             ("triggers-scripts", None), ("triggers-check", None)):  # fmt: skip
        code, line = base._run(tmp_path, command, clients=clients, capsys=capsys)
        assert code == 0, (command, line)
        printed.append(json.dumps(line))
    routines = json.loads(printed[1])
    assert {(m["routine_id"], m["problem"]) for m in routines["mismatches"]} == {
        ("shop.orders_bi", "present_before_triggers_enable")}  # fmt: skip
    written = [(tmp_path / STEP / name).read_text()
               for name in ("routines-check.json", "triggers-enable-check.json")]  # fmt: skip
    for text in [*printed, *written]:
        assert "cutover" not in text.lower(), text


def test_a_case_only_database_clash_says_why_in_the_triggers_scripts_output(tmp_path, capsys):
    # The event's database `Shop` differs from the moved tables' `shop` only by case; the
    # check that reports it missing is the triggers one, not routines-check.sql.
    from src.tools.data_movement.routine_scripts import CASE_DIFFERS_TRIGGERS_WHY
    from tests.unit.tools.data_movement.routine_fixtures import mysql_source

    rows = [row("Shop.sweep", "event", schema="Shop", status="rewritten", specific="sweep")]
    _job(tmp_path, rows, schema="Shop", target_database=None,
         extra=[mysql_source("sweep", "CREATE DEFINER=`app`@`%` EVENT `sweep` ON SCHEDULE EVERY 1 "
                             "DAY STARTS '2036-01-01 00:00:00' DISABLE DO SELECT 1", kind="event",
                             schema="Shop")])  # fmt: skip
    code, line = base._run(tmp_path, "triggers-scripts", capsys=capsys)
    assert code == 0, line
    (left_out,) = line["excluded_routines"]
    assert (left_out["routine_id"], left_out["reason"]) == ("Shop.sweep", "database_case_differs")
    assert left_out["why"] == CASE_DIFFERS_TRIGGERS_WHY
    assert (
        "triggers-enable-check.sql" in left_out["why"] and "routines-check" not in left_out["why"]
    )
    text = (
        (tmp_path / STEP / "triggers.md").read_text()
        if (tmp_path / STEP / "triggers.md").exists()
        else ""
    )
    assert "routines-check.sql" not in text


@pytest.mark.passes_on_main
def test_an_excluded_trigger_with_no_case_clash_has_no_why(tmp_path, capsys):
    _job(tmp_path, _rows())
    code, line = base._run(tmp_path, "triggers-scripts", capsys=capsys)
    assert code == 0 and all("why" not in r for r in line["excluded_routines"])
