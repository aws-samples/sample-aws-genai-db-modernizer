# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1939: every deliverable shows the routine migration from ``facts.routines``.

Synthesis computes the routine facts once (#1938, ADR-034 Decision 7); the deliverables
only format them (ADR-032) and agree:

- the engineering report's Routines section: the headline, each routine behind a risk
  by id, kind and status with why in plain words, the routines no row lists, and the
  to-do: which routines to migrate yourself and before which wave;
- the executive report: the headline counts and the to-do, no routine or table name;
- the final chat summary: the same counts and to-do.

Never a routine's code, a raw code, "proven", or cutover advice beyond the one sentence.
A report with no ``facts.routines`` renders as before.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from src.contracts.routine_migration import REASON_CODES
from src.report.printed_summary import render_final_summary
from src.report.renderers import render_engineering_report_md
from src.report.routines import routines_md
from src.shared.report_facts import compute_report_facts
from src.shared.routine_facts import routine_facts
from tests.e2e.deliverable_figures import md_plain
from tests.executive_pdf import executive_text
from tests.unit.report.test_wave_readiness_deliverables import _report as _plain_report
from tests.unit.shared.test_routine_facts import FINDING, MIXED, RULES, _artifact, _row

TO_DO = "We could not translate 5 routines automatically; migrate them yourself before wave 1."
MIGRATE_YOURSELF = (
    "public.py_fn()",
    "public.f_a()",
    "public.f_b()",
    "public.secret()",
    "public.orders.tg_audit",
)


def _report() -> dict[str, Any]:
    """The readiness report (wave 1 on Aurora MySQL) with MIXED's routines on its target."""
    report = _plain_report("MEDIUM")
    artifact = MIXED.model_dump(mode="json")
    artifact.update(source_engine="mysql", target_engine="aurora_mysql")
    routines = routine_facts(artifact, 1, report["migration_waves"])
    report["facts"] = compute_report_facts(report, routines=routines)
    return report


REPORT = _report()
FACTS = REPORT["facts"]["routines"]


def _section(md: str) -> str:
    start = md.index("## Routines")
    nxt = md.find("\n## ", start + 1)
    return md[start : nxt if nxt != -1 else len(md)]


def _final(report: dict[str, Any]) -> str:
    return render_final_summary(
        report, database_name="shop", job_id="j", deliverables_dir="d/", deliverables=[]
    )


def _pdf(report: dict[str, Any]) -> str:
    return " ".join(executive_text(report).split())


def _without_ids(text: str) -> str:
    """The text without routine ids and the fixture's catalog texts, which name their rule."""
    return re.sub(r"public\.[\w.]+(\(\))?|[\w-]+ (?:reason|fix)\.", "", text)


def _no_raw_code(text: str) -> None:
    plain = _without_ids(text)
    for code in (*REASON_CODES, *RULES, "needs_attention", "as_is", "migrate_yourself"):
        assert not re.search(rf"(?<![\w-]){re.escape(code)}(?![\w-])", plain), code


def _table_rows(section: str) -> list[str]:
    table = section[section.index("### Each routine") :]
    return [ln for ln in table.splitlines() if ln.startswith("| `")]


def test_the_engineering_report_lists_the_listed_routines_with_status_and_why() -> None:
    section = md_plain(_section(render_engineering_report_md(REPORT)))
    assert FACTS["line"] in section
    listed = [r for r in FACTS["routines"] if r["listed"]]
    rows = _table_rows(section)
    assert [ln.split("`")[1] for ln in rows] == [r["routine_id"] for r in listed]
    for row, line in zip(listed, rows, strict=True):
        assert row["kind"] in line and row["status_text"] in line
        for why in row["why"]:
            assert why in line
    f_a = next(ln for ln in rows if ln.startswith("| `public.f_a()` |"))
    assert "| function `public.f_b()` |" in f_a


def test_the_carried_routines_to_check_are_one_line_per_note() -> None:
    """Maintainer decision 2 on #1938: the LOW routines roll up, never a row each."""
    section = md_plain(_section(render_engineering_report_md(REPORT)))
    for rid in ("public.plain_unknown()", "public.owned()"):
        assert not any(rid in ln for ln in _table_rows(section))
    notes = section[section.index("### Rewrites and checks that could not run") :]
    assert (
        "- Owner set to the target user (1): `public.owned()`. pg-owner-to-target-user reason."
        in notes
    )
    assert "- A check could not run (1): `public.plain_unknown()`. pg-dependency reason." in notes
    assert notes.count("pg-dependency reason.") == 1
    assert FACTS["others_line"] in notes


def test_a_long_note_lists_its_first_ids_and_how_many_more() -> None:
    report = _plain_report("MEDIUM")
    rows = [_row(f"shop.f{i:02d}", checks=[_unknown()]) for i in range(13)]
    artifact = _artifact(*rows, source="mysql", target="aurora_mysql")
    report["facts"] = compute_report_facts(
        report, routines=routine_facts(artifact, 1, report["migration_waves"])
    )
    section = md_plain(_section(render_engineering_report_md(report)))
    assert "- A check could not run (13): `shop.f00`, " in section
    assert "`shop.f09`, … and 3 more. pg-dependency reason." in section
    assert "shop.f10" not in section and "### Each routine" not in section


def test_the_engineering_report_lists_the_to_do_with_its_wave() -> None:
    section = md_plain(_section(render_engineering_report_md(REPORT)))
    to_do = section[section.index("### To do") : section.index("### Each routine")]
    assert to_do.startswith(f"### To do\n\n{TO_DO}\n")
    assert to_do.rstrip().endswith(
        "Migrate these routines yourself before wave 1, then check them again."
    )
    listed = re.findall(r"^- (\w+) `([^`]+)`", to_do, re.M)
    assert [rid for _, rid in listed] == list(MIGRATE_YOURSELF)
    assert ("trigger", "public.orders.tg_audit") in listed


def test_the_engineering_section_shows_no_code_raw_code_or_claim() -> None:
    section = md_plain(_section(render_engineering_report_md(REPORT)))
    assert FINDING not in section
    _no_raw_code(section)
    assert "proven" not in section.lower()
    assert "cutover" not in section.lower()
    assert "For 2 carried routines not every check could run" in section


def test_an_id_with_markdown_in_it_stays_an_id() -> None:
    """Ids are structured fields the renderer sets as code spans, never free text."""
    odd = 'shop."c|d`*e*<x>"'
    report = _plain_report("MEDIUM")
    artifact = _artifact(
        _row(odd, "needs_attention", reasons=["pg-language"], checks=[_fail("pg-language")]),
        _row(
            "shop.caller",
            "needs_attention",
            reasons=["depends_on_needs_attention"],
            blocked_by=[{"kind": "function", "routine_id": odd}],
        ),
        source="mysql",
        target="aurora_mysql",
    )
    report["facts"] = compute_report_facts(
        report, routines=routine_facts(artifact, 1, report["migration_waves"])
    )
    section = _section(render_engineering_report_md(report))
    span = '`shop."c\\|d*e*<x>"`'  # the backtick dropped, the pipe escaped, nothing else
    rows = _table_rows(section)
    assert rows[0].startswith(f"| {span} |")
    assert f"| function {span} |" in rows[1]
    assert "&lt;" not in section
    # Outside the spans no stray emphasis or code.
    outside = re.sub(r"`[^`]*`", "", "\n".join(rows))
    assert "*" not in outside and "<" not in outside


def test_the_executive_report_states_the_counts_once_and_names_nothing() -> None:
    pdf = _pdf(REPORT)
    assert f"Routines: {FACTS['line']} The engineering report lists each routine." in pdf
    # The to-do once: in its wave's readiness line, not again under the numbers.
    assert f"To do: {TO_DO}" not in pdf
    assert f"To do: {TO_DO[0].lower()}{TO_DO[1:]}" in pdf
    for rid in (r["routine_id"] for r in FACTS["routines"]):
        assert rid not in pdf
    assert "tg_audit" not in pdf and "public.orders" not in pdf
    assert FINDING not in pdf


def test_the_executive_report_states_the_to_do_when_no_wave_carries_it() -> None:
    report = copy.deepcopy(REPORT)
    report["facts"]["routines"]["wave"] = None
    assert f"To do: {TO_DO}" in _pdf(report)


def test_the_final_summary_states_the_counts_and_the_to_do_once() -> None:
    text = md_plain(_final(REPORT))
    glance = text[text.index("### At a glance") : text.index("### Engines")]
    assert f"- Routines: {FACTS['line']}" in glance
    assert glance.count("could not translate 5 routines") == 1
    assert f"- To do: {TO_DO}" in glance
    assert "public.f_a()" not in text
    # With wave 1 not suggested, its line carries the to-do and nothing repeats it.
    held = copy.deepcopy(REPORT)
    held["facts"]["wave_readiness_notes"] = [{"wave": 1, "line": f"Held. To do: {TO_DO}"}]
    glance = md_plain(_final(held))
    glance = glance[glance.index("### At a glance") : glance.index("### Engines")]
    assert glance.count("could not translate 5 routines") == 1
    assert "- To do:" not in glance


def test_the_deliverables_agree_on_the_counts_and_the_to_do() -> None:
    md = md_plain(render_engineering_report_md(REPORT))
    pdf = _pdf(REPORT).lower()
    chat = md_plain(_final(REPORT))
    for text in (md, chat):
        assert FACTS["line"] in text and TO_DO in text
    assert FACTS["line"].lower() in pdf and TO_DO.lower() in pdf


def test_no_to_do_without_a_routine_to_migrate_yourself() -> None:
    report = copy.deepcopy(REPORT)
    facts = report["facts"]["routines"]
    facts.update(
        migrate_yourself_line=None,
        migrate_yourself_step=None,
        migrate_yourself_count=0,
        to_do_routines=[],
    )
    facts["routines"] = [
        r for r in facts["routines"] if "migrate_yourself" not in r["risk_classes"]
    ]
    md = md_plain(render_engineering_report_md(report))
    assert "### To do" not in _section(md)
    assert "To do: We could not" not in _pdf(report)
    assert "- To do:" not in _final(report)


@pytest.mark.passes_on_main  # #1938 already renders nothing new without the facts
@pytest.mark.parametrize("routines", ["absent", None])
def test_a_report_without_routine_facts_renders_as_before(routines: Any) -> None:
    """A report written before #1938, or a job with no routines: no section, no line."""
    old = _plain_report("MEDIUM")
    if routines is None:
        old["facts"]["routines"] = None
    assert routines_md(old) == []
    md = render_engineering_report_md(old)
    assert md == render_engineering_report_md(_plain_report("MEDIUM"))
    assert "## Routines" not in md and "routine" not in md.lower()
    pdf = executive_text(old)
    assert "Routines:" not in pdf and pdf == executive_text(_plain_report("MEDIUM"))
    chat = _final(old)
    assert "Routines" not in chat and chat == _final(_plain_report("MEDIUM"))


def test_with_no_routine_behind_a_risk_the_section_states_the_headline_only() -> None:
    report = _plain_report("MEDIUM")
    artifact = _artifact(
        _row("public.a()"), _row("public.b()"), source="mysql", target="aurora_mysql"
    )
    routines = routine_facts(artifact, 1, report["migration_waves"])
    report["facts"] = compute_report_facts(report, routines=routines)
    section = md_plain(_section(render_engineering_report_md(report)))
    assert section.strip().startswith(
        "## Routines\n\n2 of 2 routines carry over: 2 as they are. Checked, not load-tested. "
        "Each of the 2 routines is carried and every check on it passed."
    )
    assert "### To do" not in section and "### Each routine" not in section
    pdf = _pdf(report)
    assert "To do:" not in pdf
    # Nothing listed, so no pointer to a list.
    assert "Routines: 2 of 2 routines carry over: 2 as they are. Checked, not load-tested." in pdf
    assert "The engineering report lists each routine." not in pdf


OLD_JOB = (
    Path(__file__).resolve().parents[3]
    / "tests/fixtures/old_jobs/v0.4.0-beta.2/wordpress/aa4e840a/synthesis/v2/report.json"
)


@pytest.mark.passes_on_main  # a saved job has no facts.routines since before #1938
def test_a_saved_old_job_renders_no_routines() -> None:
    old = json.loads(OLD_JOB.read_text(encoding="utf-8"))
    assert routines_md(old) == []
    assert "## Routines" not in render_engineering_report_md(old)
    assert "Routines:" not in _final(old)


def _fail(rule_id: str) -> dict[str, Any]:
    return {"rule_id": rule_id, "outcome": "fail", "findings": []}


def _unknown(rule_id: str = "pg-dependency") -> dict[str, Any]:
    return {"rule_id": rule_id, "outcome": "unknown", "findings": []}
