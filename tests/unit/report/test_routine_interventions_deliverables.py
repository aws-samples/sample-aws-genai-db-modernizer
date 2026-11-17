# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The deliverables say a routine needed an intervention, and what came of it (#1941).

``facts.routines.interventions`` holds, per routine the repair loop worked on, the line
the deliverables print (ADR-032: computed once, the renderers only format it): a
``model_repaired`` routine's line says the proof accepted it side by side and what
changed, in the model's change note; a pending candidate's never presents its note as
what changed; a routine that still needs attention lists every attempt and its error,
in the engineering report and in its risk. The results page renders the same facts
(``RoutineMigrationSection.test.js`` reads ``fixtures/routineInterventions.json``, which
is these facts).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.contracts.synthesis_output import RoutineFacts
from src.report.routines import routines_md
from src.shared.migration_risk_rules import routine_risks
from src.shared.routine_facts import routine_facts
from tests.unit.agents.routine_repair import repair_fixtures as fx
from tests.unit.report.test_wave_readiness_deliverables import _report as _plain_report

REPAIRED_NOTE = "Calls pg_current_wal_lsn, the name PostgreSQL 10 gave the function."
PENDING_NOTE = "Pending note that must not be printed as what changed."
ERRORS = (
    "the candidate creates another name, kind or trigger table",
    "the rebuilt exact text fails postgresql-routine-v10-renamed-functions",
    "the side-by-side proof found 2 difference(s) (result) on the call(s) baseline",
)


def _attempt(n: int, stage: str, outcome: str, code: str | None, error: str | None) -> dict:
    return {"attempt": n, "stage": stage, "outcome": outcome, "code": code, "error": error}


def repair() -> dict[str, Any]:
    """A repair result with one routine in each intervention state, plus a refusal."""
    rows: list[dict[str, Any]] = [
        {
            "kind": "function", "routine_id": fx.WAL, "state": "model_repaired",
            "reasons": ["postgresql-routine-v10-renamed-functions"],
            "repaired_create": fx.WAL_REPAIRED, "change_note": REPAIRED_NOTE,
            "diff": "-x\n+y", "proof_source": "a disposable postgresql 16 engine",
            "attempts": [_attempt(1, "proof", "proved", None, "no differences found")],
        },
        {
            "kind": "function", "routine_id": "public.ok(integer)",
            "state": "repaired_pending_proof", "reasons": ["target_error"],
            "repaired_create": "CREATE ...", "change_note": PENDING_NOTE, "diff": "-a\n+b",
            "attempts": [_attempt(1, "proof", "pending", "no_proof_target", "no proof target")],
        },
        {
            "kind": "function", "routine_id": fx.HIDDEN, "state": "needs_attention",
            "reasons": ["postgresql-routine-v10-renamed-functions"],
            "attention": "attempts_used",
            "attention_detail": "every one of 3 attempts failed",
            "attempts": [
                _attempt(1, "screen", "failed", "renamed", ERRORS[0]),
                _attempt(2, "recheck", "failed", "check_failed", ERRORS[1]),
                _attempt(3, "proof", "failed", "different", ERRORS[2]),
            ],
        },
        {
            "kind": "function", "routine_id": "public.orphan(integer)",
            "state": "needs_attention", "reasons": ["postgresql-routine-dependency-not-moved"],
            "attention": "not_rebuildable",
            "attention_detail": "a literal of the routine reads '?' itself",
        },
    ]  # fmt: skip
    counts = dict.fromkeys(
        ("repaired_pending_proof", "refused", "no_answer", "not_repaired", "model_repaired",
         "needs_attention"), 0,
    )  # fmt: skip
    for r in rows:
        counts[r["state"]] += 1
    return {
        "job_id": fx.JOB, "database_name": fx.DB, "assignment_version": 1, "llm_mode": "bedrock",
        "source_engine": "postgresql", "target_engine": "aurora_postgresql", "proof": "local",
        "rounds": 3, "counts": counts, "routines": rows,
    }  # fmt: skip


FACTS = routine_facts(fx.migration(), 1, [{"wave": 1, "engines": ["aurora_postgresql"]}], repair())


def _by_id() -> dict[str, dict[str, Any]]:
    assert FACTS is not None
    return {i["routine_id"]: i for i in FACTS["interventions"]}


def test_each_intervention_state_has_its_words() -> None:
    RoutineFacts.model_validate(FACTS)
    items = _by_id()
    repaired = items[fx.WAL]["line"]
    # #2385: compared on a local engine of the source, never more than checked on Aurora.
    assert "compared with the original on a disposable postgresql 16 engine" in repaired
    assert "a local engine of the source" in repaired
    assert "it is checked, not tested yet on a test Aurora" in repaired
    assert "every call" not in repaired and "proof accepted" not in repaired
    assert f"What changed: {REPAIRED_NOTE}" in repaired
    assert "does not deploy a repaired routine yet" not in repaired  # #2384: it can now
    assert "Test it with /test-routines before it is deployed." in repaired
    pending = items["public.ok(integer)"]["line"]
    assert "not carried" in pending and PENDING_NOTE not in pending
    assert "What changed" not in pending
    used = items[fx.HIDDEN]
    assert "tried 3 times and none was accepted" in used["line"]
    assert used["attempt_lines"] == [
        f"Attempt 1: refused by the repair screen: {ERRORS[0]}",
        f"Attempt 2: failed the routine checks on the exact text: {ERRORS[1]}",
        f"Attempt 3: the side-by-side proof: {ERRORS[2]}",
    ]
    blocked = items["public.orphan(integer)"]["line"]
    assert blocked.startswith("It was not sent for a model repair: A literal of the routine")
    # #2384: a carried repair is carried, repaired; the other states move no figure.
    without = routine_facts(fx.migration(), 1, [{"wave": 1, "engines": ["aurora_postgresql"]}])
    assert without is not None
    for key, moved in (("carried_count", 1), ("repaired_count", 1), ("needs_repair_count", -1)):
        assert FACTS[key] == without[key] + moved, key


def test_the_engineering_report_prints_each_intervention_as_the_facts_say() -> None:
    report = _plain_report("MEDIUM")
    report["facts"]["routines"] = FACTS
    text = "\n".join(routines_md(report))
    assert "### Interventions" in text
    from src.report.escaping import md_text

    for item in _by_id().values():
        assert md_text(item["line"]) in text
        for line in item["attempt_lines"]:
            assert f"  - {md_text(line)}" in text
    assert PENDING_NOTE not in text
    assert "$function$" not in text and "-x" not in text  # no routine text, no diff


def test_the_risk_lists_every_attempt_of_a_routine_that_needs_attention() -> None:
    assert FACTS is not None
    risks = routine_risks(FACTS)
    details = [d for r in risks for d in r.get("details") or []]
    hidden = next(d for d in details if fx.HIDDEN in d)
    assert "tried 3 times" in hidden
    for error in ERRORS:
        assert error in hidden


def test_a_report_stored_before_the_words_gets_them() -> None:
    from src.report.facts import report_facts

    assert FACTS is not None
    report = _plain_report("MEDIUM")
    stored = json.loads(json.dumps(FACTS))
    for item in stored["interventions"]:
        item.pop("line")
        item.pop("attempt_lines")
    report["facts"]["routines"] = stored
    assert report_facts(report)["routines"]["interventions"] == FACTS["interventions"]


UI_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "src/ui/src/components/__tests__/fixtures/routineInterventions.json"
)


def test_the_ui_fixture_is_the_facts_the_python_deliverables_print() -> None:
    """RoutineMigrationSection.test.js renders this file: the same facts as here."""
    assert json.loads(UI_FIXTURE.read_text(encoding="utf-8")) == FACTS


def test_a_pending_row_from_the_seam_alone_does_not_claim_the_loops_checks() -> None:
    """A #1940 result (1.0, no attempts) never ran the checks on the exact text: its line
    says only that a model proposed a repair (#2387 review)."""
    from src.shared.routine_facts import intervention_line

    seam_only = {"state": "repaired_pending_proof", "change_note": PENDING_NOTE, "attempts": []}
    line = intervention_line(seam_only)
    assert "exact text" not in line and "not carried" in line and PENDING_NOTE not in line
    looped = {**seam_only, "attempts": [_attempt(1, "proof", "pending", "no_proof_target", "x")]}
    assert "passed every check again on the exact text" in intervention_line(looped)


# --------------------------------------------------------------------------- #2385


def _repaired_line(facts: dict[str, Any]) -> str:
    return str(next(i for i in facts["interventions"] if i["state"] == "model_repaired")["line"])


def test_a_model_repair_reads_as_compared_locally_and_not_tested_on_aurora() -> None:
    """#2385, the #1925 decision of 2026-11-17: a repair the local comparison accepted is
    compared on an engine of the source, never tested on Aurora, in #2411's words."""
    from src.shared.routine_facts import NOT_TESTED_STATE, repaired_not_tested_sentence

    assert FACTS is not None
    line = _repaired_line(FACTS)
    assert repaired_not_tested_sentence("a disposable postgresql 16 engine") in line
    assert f"it is {NOT_TESTED_STATE} on a test Aurora" in line
    assert NOT_TESTED_STATE == "checked, not tested yet"
    for overclaim in ("tested on a test Aurora:", "proved", "every call", "ready to deploy"):
        assert overclaim not in line, overclaim
    # No proof source on the row: the words still name a local engine of the source.
    bare = repaired_not_tested_sentence(None)
    assert "a disposable engine of the source, a local engine of the source" in bare


def test_the_engineering_report_prints_the_repair_as_not_tested_on_aurora() -> None:
    from src.report.escaping import md_text

    assert FACTS is not None
    report = _plain_report("MEDIUM")
    report["facts"]["routines"] = FACTS
    text = "\n".join(routines_md(report))
    assert md_text(_repaired_line(FACTS)) in text
    assert "not tested yet on a test Aurora" in text


def test_the_ui_fixture_shows_the_repair_as_not_tested_on_aurora() -> None:
    """RoutineMigrationSection.test.js prints each intervention's ``line`` from this file."""
    fixture = json.loads(UI_FIXTURE.read_text(encoding="utf-8"))
    line = _repaired_line(fixture)
    assert "a local engine of the source" in line
    assert "it is checked, not tested yet on a test Aurora" in line


def test_the_executive_line_counts_model_repairs_as_checked_not_tested() -> None:
    from src.report.routines import routine_summary_lines
    from src.shared.routine_facts import executive_line

    assert FACTS is not None
    clause = (
        "1 of the carried routines was repaired by a model and compared on a local engine "
        "of the source: it is "
        "checked, not tested yet on a test Aurora."
    )
    assert FACTS["executive_line"].endswith(clause)
    report = _plain_report("MEDIUM")
    report["facts"]["routines"] = FACTS
    (label, text), *_ = routine_summary_lines(report, executive=True)
    assert label == "Routines" and text == FACTS["executive_line"]
    two = executive_line({"as_is": 1}, 3, 1, None, repaired_by_model=2)
    assert "2 of the carried routines were repaired by a model" in two
    assert "they are checked, not tested yet on a test Aurora" in two
    assert "not deploy" not in two  # #2384: the deploy gate decides, after a test
    # No model repair: the line is #2411's, unchanged.
    assert executive_line({"as_is": 1}, 3, 1, None) == executive_line(
        {"as_is": 1}, 3, 1, None, repaired_by_model=0
    )
    assert "model" not in executive_line({"as_is": 1}, 3, 1, None)
    # A pending or failed repair is no model repair the comparison accepted.
    pending_only = routine_facts(
        fx.migration(),
        1,
        [{"wave": 1, "engines": ["aurora_postgresql"]}],
        {
            **repair(),
            "routines": [r for r in repair()["routines"] if r["state"] != "model_repaired"],
            "counts": {},
        },
    )
    assert pending_only is not None and "model repaired" not in pending_only["executive_line"]


def test_a_stored_block_without_an_executive_line_counts_its_model_repairs() -> None:
    from src.report.facts import report_facts

    assert FACTS is not None
    stored = json.loads(json.dumps(FACTS))
    stored.pop("executive_line")
    report = _plain_report("MEDIUM")
    report["facts"]["routines"] = stored
    assert (
        "1 of the carried routines was repaired by a model"
        in report_facts(report)["routines"]["executive_line"]
    )
