# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The executive report's routines sentence and the carried-routines risk's next step
(#2411, the maintainer's direction of 2026-11-17 on #2411): high level, counts from the
facts, naming the command; the engineering report and the /modernize summary keep the
detailed line. One source: ``facts.routines.executive_line`` (ADR-032)."""

from __future__ import annotations

from src.report.printed_summary import render_final_summary
from src.report.renderers import render_engineering_report_md
from src.shared.migration_risk_rules import routine_risks
from src.shared.report_facts import compute_report_facts
from src.shared.routine_facts import executive_line, routine_facts, with_routine_words
from tests.e2e.deliverable_figures import md_plain
from tests.executive_pdf import executive_text
from tests.unit.report.test_wave_readiness_deliverables import _report as _plain_report
from tests.unit.shared.test_routine_facts import _artifact, _row
from tests.unit.tools.data_movement.routine_fixtures import with_test_result

BEFORE = (
    "Routines: your routines can move to Aurora with the database: 4 of 5 for this database, "
    "3 as they are and 1 rewritten; 1 needs work before wave 1. Before they are deployed, "
    "test them on a throwaway Aurora with the /test-routines command. The engineering report "
    "lists each routine."
)
REWRITE = {"rule_id": "mysql-routine-definer-to-target-user", "rewrite": "definer"}


def _artifact5():
    return _artifact(
        _row("public.a()"), _row("public.b()"), _row("public.c()"),
        _row("public.d()", "rewritten", rewrites=[REWRITE]),
        _row("public.e()", "needs_attention", reasons=["pg-language"]),
        source="mysql", target="aurora_mysql",
    )  # fmt: skip


def _report(artifact, digest=None):
    report = _plain_report("MEDIUM")
    routines = routine_facts(artifact, 1, report["migration_waves"], sources_digest=digest)
    report["facts"] = compute_report_facts(report, routines=routines)
    return report


def test_before_a_test_the_executive_report_speaks_at_a_high_level_and_names_the_command():
    report = _report(_artifact5())
    pdf = " ".join(executive_text(report).split())
    assert BEFORE in pdf
    assert "Checked, not load-tested" not in pdf and "public." not in pdf
    # The engineering report and the /modernize summary keep the detailed line.
    detail = report["facts"]["routines"]["line"]
    assert detail.startswith("4 of 5 routines carry over: 3 as they are, 1 rewritten.")
    eng = " ".join(md_plain(render_engineering_report_md(report)).split())
    chat = " ".join(md_plain(render_final_summary(report, database_name="shop", job_id="j",
                                                  deliverables_dir="d/", deliverables=[])).split())  # fmt: skip
    for text in (eng, chat):
        assert detail in text and "can move to Aurora with the database" not in text


def test_after_a_test_it_gives_what_step_1_deploys_and_holds():
    artifact = with_test_result(_artifact5(), "d" * 64, {"public.d()": "needs_attention"})
    report = _report(artifact, "d" * 64)
    pdf = " ".join(executive_text(report).split())
    assert (
        "Routines: your routines can move to Aurora with the database: 4 of 5 for this database, "
        "3 as they are and 1 rewritten; 1 needs work before wave 1. Tested on a throwaway "
        "Aurora: step 1 deploys 3; 1 is held back. The engineering report lists each routine."
    ) in pdf
    assert "/test-routines command" not in pdf


def test_the_short_form_reads_well_for_one_routine_and_for_none_needing_work():
    assert executive_line({"as_is": 1}, 1, 1) == (
        "your routine can move to Aurora with the database: 1 of 1 for this database, 1 as it "
        "is. Before it is deployed, test it on a throwaway Aurora with the /test-routines "
        "command."
    )
    assert executive_line({"as_is": 2, "rewritten": 1}, 3, None).startswith(
        "your routines can move to Aurora with the database: 3 of 3 for this database, 2 as "
        "they are and 1 rewritten. Before"
    )
    assert executive_line({}, 2, 2) == (
        "none of your 2 routines can move to Aurora automatically; 2 need work before wave 2."
    )
    assert executive_line({"as_is": 2}, 2, 1, (2, 0)).endswith("step 1 deploys all 2.")
    assert executive_line({"as_is": 2}, 2, 1, (0, 2)).endswith(
        "step 1 deploys none of them; 2 are held back."
    )


def test_a_report_stored_before_the_short_form_gets_it_from_the_facts_layer():
    facts = routine_facts(_artifact5(), 1, [{"wave": 1, "engines": ["aurora_mysql"]}])
    stored = {k: v for k, v in facts.items() if k != "executive_line"}
    assert with_routine_words(stored)["executive_line"] == facts["executive_line"]


def test_the_carried_routines_risk_says_to_test_them_with_the_command():
    facts = routine_facts(_artifact5(), 1, [{"wave": 1, "engines": ["aurora_mysql"]}])
    [risk] = [r for r in routine_risks(facts, [{"wave": 1, "engines": ["aurora_mysql"]}])
              if r.get("rule") == "routines.carried"]  # fmt: skip
    assert risk["mitigation"].startswith(
        "Test the carried routines on a throwaway Aurora with /test-routines before step 1 "
        "deploys them, and check the target user's privileges cover what each rewritten one "
        "does."
    )
