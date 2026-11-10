# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1939: the routine deliverables on whole runs of the PostgreSQL and MySQL routine
collections (#1934), ``run_assessment.py --all -y --llm-mode none``.

The carried routines to check roll up (maintainer decision 2 on #1938): the engineering
report lists one by one only the routines to migrate yourself, that would behave
differently or that have no code collected, and gives each rewrite and each check that
could not run once, with its routines. A function and a procedure of one name are two
routines.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.contracts.synthesis_output import SynthesisOutputContract
from src.report.renderers import render_engineering_report_md
from tests.e2e.deliverable_figures import md_plain
from tests.unit.report.test_routine_deliverables import _section, _table_rows
from tests.unit.scripts.test_synthesis_routine_risks import mysql_full, pg_full  # noqa: F401


def _report(job: Path) -> dict[str, Any]:
    report: dict[str, Any] = json.loads((job / "synthesis" / "v1" / "report.json").read_text())
    return report


@pytest.mark.parametrize("run", ["pg_full", "mysql_full"])
def test_only_the_listed_routines_get_a_row_and_the_rest_roll_up(run, request) -> None:
    report = _report(request.getfixturevalue(run))
    SynthesisOutputContract.model_validate(report)
    facts = report["facts"]["routines"]
    listed = [r for r in facts["routines"] if r["listed"]]
    low = [r for r in facts["routines"] if r["risk_classes"][0] == "carried_review"]
    assert low, "the sample has carried routines to check"
    assert all(not r["listed"] and r["why"] == [] for r in low)
    section = md_plain(_section(render_engineering_report_md(report)))
    assert len(_table_rows(section)) == len(listed)
    notes = section[section.index("### Rewrites and checks that could not run") :]
    # Each note's catalog text once, however many routines share it (a row's failed
    # check may give the same rule's reason in its why).
    for note in facts["check_notes"]:
        assert note["text"] and notes.count(note["text"]) == 1
        assert f"{note['label']} ({note['note_count']}): " in notes
    covered = {(r["kind"], r["routine_id"]) for n in facts["check_notes"] for r in n["routines"]}
    assert {(r["kind"], r["routine_id"]) for r in low} <= covered


def test_mysql_has_a_function_and_a_procedure_of_one_name(mysql_full) -> None:  # noqa: F811
    facts = _report(mysql_full)["facts"]["routines"]
    refs = [
        (r["kind"], r["routine_id"])
        for n in facts["check_notes"]
        if n["note"] == "rewrite"
        for r in n["routines"]
    ]
    assert ("function", "shop.place_order") in refs and ("procedure", "shop.place_order") in refs
    [definer] = [n for n in facts["check_notes"] if n["note"] == "rewrite"]
    assert definer["label"] == "DEFINER set to the target user"
