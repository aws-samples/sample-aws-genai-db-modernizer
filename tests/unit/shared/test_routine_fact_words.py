# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1939: the words every deliverable prints for each routine, computed once in the facts.

``facts.routines`` (#1938) holds each routine behind a routine risk by its ids, rule ids
and reason codes. The deliverables print the routine's status and why in plain words, the
same in the engineering report and the UI, so the words are facts too (ADR-032): each
row's ``status_text``, ``why`` and ``listed``, the ``to_do_routines``, the
``check_notes`` the carried routines to check roll up into (maintainer decision 2 on
#1938: once per note, never one row each) and ``others_line``. A block stored before
them gets the same at render time.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from src.contracts.routine_migration import REASON_CODES
from src.contracts.synthesis_output import RoutineFacts
from src.report.facts import report_facts
from src.shared.prose_style import find_style_hits
from src.shared.routine_facts import NOTE_IDS_LISTED, PROOF_LABEL, routine_facts
from tests.unit.report.test_wave_readiness_deliverables import _report as _plain_report
from tests.unit.shared.test_routine_facts import MIXED, REWRITE, RULES, _artifact, _row

WAVES = [{"wave": 1, "engines": ["aurora_postgresql"], "moves_from": ["postgresql"]}]
FACTS = routine_facts(MIXED, 1, WAVES)

# Words a customer never reads: the artifact's statuses, reason codes, rule ids and
# category names.
RAW = (
    *REASON_CODES,
    *RULES,
    "needs_attention",
    "as_is",
    "model_repaired",
    "needs_repair",
    "not_exact",
    "not_readable",
    "opted_out",
    "migrate_yourself",
)


def _by_id(facts: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {r["routine_id"]: r for r in facts["routines"]}


def _unknown(rule_id: str = "pg-dependency") -> dict[str, Any]:
    return {"rule_id": rule_id, "outcome": "unknown", "findings": []}


def test_each_row_states_its_status_in_plain_words() -> None:
    rows = _by_id(FACTS)
    assert {rid: r["status_text"] for rid, r in rows.items()} == {
        "public.plain_unknown()": "Carried as it is",
        "public.owned()": "Carried with a rewrite",
        "public.py_fn()": "Not carried: needs repair",
        "public.f_a()": "Not carried: needs repair",
        "public.f_b()": "Not carried: needs repair",
        "public.public_path()": "Not carried: would behave differently",
        "public.secret()": "Not carried: its code could not be read",
        "public.opted_out()": "Not carried: no code collected",
        "public.orders.tg_audit": "Not carried: its code was rebuilt around a literal",
    }


def test_only_the_to_do_behaves_differently_and_opted_out_rows_are_listed() -> None:
    """The carried routines to check (LOW) are never listed one by one."""
    rows = _by_id(FACTS)
    assert {rid for rid, r in rows.items() if r["listed"]} == set(rows) - {
        "public.plain_unknown()",
        "public.owned()",
    }
    assert rows["public.owned()"]["why"] == [] and rows["public.plain_unknown()"]["why"] == []
    assert FACTS["to_do_routines"] == [
        {"kind": "function", "routine_id": "public.py_fn()"},
        {"kind": "function", "routine_id": "public.f_a()"},
        {"kind": "function", "routine_id": "public.f_b()"},
        {"kind": "function", "routine_id": "public.secret()"},
        {"kind": "trigger", "routine_id": "public.orders.tg_audit"},
    ]


def test_each_listed_row_says_why_without_an_id() -> None:
    rows = _by_id(FACTS)
    assert rows["public.py_fn()"]["why"] == [
        "A check failed: pg-language reason. Fix: pg-language fix."
    ]
    assert rows["public.public_path()"]["why"] == [
        "It would behave differently: pg-public-schema reason. Fix: pg-public-schema fix."
    ]
    # The routine it names is blocked_by, a structured id, never in the words.
    assert rows["public.f_a()"]["why"] == [
        "It is in a dependency cycle.",
        "It names a routine that is not translated.",
    ]
    assert rows["public.f_a()"]["blocked_by"] == [
        {"kind": "function", "routine_id": "public.f_b()"}
    ]
    assert rows["public.secret()"]["why"] == ["The collection user could not read its code."]
    assert rows["public.opted_out()"]["why"] == [
        "Routine body collection was turned off, so there is no code to check."
    ]
    assert rows["public.orders.tg_audit"]["why"] == [
        "Its code was rebuilt around a literal, so it is not exact; collect it again to "
        "have it checked and carried."
    ]
    for r in FACTS["routines"]:
        assert not any("public." in w for w in r["why"]), r


def test_what_routines_share_is_a_note_never_a_rows_why() -> None:
    """A rewrite or a check that could not run says the same on every routine: it is a
    note, once, for a routine that is not carried too; an unreadable routine has no code,
    so a check that could not run on it says nothing."""
    facts = routine_facts(
        _artifact(
            _row(
                "public.p()",
                "needs_attention",
                reasons=["pg-language"],
                checks=[_fail("pg-language"), _unknown()],
                rewrites=[REWRITE],
            ),
            _row("public.q()", checks=[_unknown()]),
            _row(
                "public.unread()",
                "needs_attention",
                source_text="not_collected",
                body_status="not_readable",
                reasons=["source_text_not_collected"],
                checks=[_unknown()],
            ),
        ),
        1,
        WAVES,
    )
    assert facts is not None
    rows = _by_id(facts)
    assert rows["public.p()"]["why"] == [
        "A check failed: pg-language reason. Fix: pg-language fix."
    ]
    assert rows["public.unread()"]["why"] == ["The collection user could not read its code."]
    assert not any("body" in w for r in rows.values() for w in r["why"])
    notes = {(n["note"], n["rule_id"]): n for n in facts["check_notes"]}
    assert [r["routine_id"] for r in notes[("rewrite", "pg-owner-to-target-user")]["routines"]] == [
        "public.p()"
    ]
    unknown = notes[("check_not_run", "pg-dependency")]
    assert (unknown["note_count"], [r["routine_id"] for r in unknown["routines"]]) == (
        2,
        ["public.p()", "public.q()"],
    )


def _fail(rule_id: str) -> dict[str, Any]:
    return {"rule_id": rule_id, "outcome": "fail", "findings": []}


def test_the_carried_routines_to_check_roll_up_once_per_note() -> None:
    assert FACTS["check_notes"] == [
        {
            "note": "rewrite",
            "rule_id": "pg-owner-to-target-user",
            "label": "Owner set to the target user",
            "text": "pg-owner-to-target-user reason.",
            "note_count": 1,
            "routines": [{"kind": "function", "routine_id": "public.owned()"}],
            "more_count": 0,
        },
        {
            "note": "check_not_run",
            "rule_id": "pg-dependency",
            "label": "A check could not run",
            "text": "pg-dependency reason.",
            "note_count": 1,
            "routines": [{"kind": "function", "routine_id": "public.plain_unknown()"}],
            "more_count": 0,
        },
        {
            "note": "check_not_run",
            "rule_id": "pg-superuser",
            "label": "A check could not run",
            "text": "pg-superuser reason.",
            "note_count": 1,
            "routines": [{"kind": "function", "routine_id": "public.owned()"}],
            "more_count": 0,
        },
    ]


def test_a_long_note_lists_the_first_ids_and_counts_the_rest() -> None:
    n = NOTE_IDS_LISTED + 3
    facts = routine_facts(
        _artifact(*[_row(f"public.f{i:02d}()", checks=[_unknown()]) for i in range(n)]), 1, WAVES
    )
    assert facts is not None
    [note] = facts["check_notes"]
    assert (note["note_count"], len(note["routines"]), note["more_count"]) == (
        n,
        NOTE_IDS_LISTED,
        3,
    )
    assert note["routines"][0]["routine_id"] == "public.f00()"
    assert not any(r["listed"] for r in facts["routines"]) and facts["others_line"] is None


def test_no_raw_code_or_style_hit_reaches_the_words() -> None:
    texts = (
        [FACTS["others_line"]]
        + [t for r in FACTS["routines"] for t in (r["status_text"], *r["why"])]
        + [n["label"] for n in FACTS["check_notes"]]
    )
    for text in texts:
        for code in RAW:
            # The fixture's catalog texts name their rule; the words never.
            plain = re.sub(r"[\w-]+ (?:reason|fix)\.", "", text)
            assert not re.search(rf"(?<![\w-]){re.escape(code)}(?![\w-])", plain), (code, text)
        assert not find_style_hits(text), text
        assert "proven" not in text.lower() and "cutover" not in text.lower()


def test_the_routines_with_no_row_are_stated_once() -> None:
    # MIXED has one routine with no risk: carried, every check ran and passed.
    assert FACTS["others_line"] == "The other routine is carried and every check on it passed."
    two = routine_facts(_artifact(_row("public.a()"), _row("public.b()")), 1, WAVES)
    assert two is not None and two["routines"] == []
    assert two["others_line"] == "Each of the 2 routines is carried and every check on it passed."
    three = routine_facts(
        _artifact(_row("public.a()"), _row("public.b()"), _row("public.c()", checks=[_unknown()])),
        1,
        WAVES,
    )
    assert three is not None and three["others_line"] == (
        "The other 2 routines are carried and every check on them passed."
    )
    listed = routine_facts(_artifact(_row("public.a()", checks=[_unknown()])), 1, WAVES)
    assert listed is not None and listed["others_line"] is None


def test_no_carried_routine_means_no_checked_label() -> None:
    """The headline says "checked, not load-tested" only for carried routines whose checks
    all ran: with none carried it says nothing about checks."""
    none_carried = routine_facts(
        _artifact(
            _row(
                "public.secret()",
                "needs_attention",
                source_text="not_collected",
                body_status="not_readable",
                reasons=["source_text_not_collected"],
            )
        ),
        1,
        WAVES,
    )
    assert none_carried is not None
    assert none_carried["line"] == (
        "0 of 1 routine carries over. 1 was not carried automatically: 1 was not readable."
    )
    assert PROOF_LABEL not in none_carried["line"].lower()


def test_the_facts_validate_with_the_words() -> None:
    model = RoutineFacts.model_validate(FACTS)
    assert model.model_dump(mode="json") == FACTS


def _stored_before(facts: dict[str, Any]) -> dict[str, Any]:
    """The block as #1938 wrote it, before the words."""
    old = copy.deepcopy(facts)
    for key in ("others_line", "to_do_routines", "check_notes"):
        old.pop(key)
    for r in old["routines"]:
        for key in ("status_text", "why", "listed"):
            r.pop(key)
    return old


def _generic_rewrite_label(facts: dict[str, Any]) -> dict[str, Any]:
    """FACTS as a block stored before them reads: the rewrite's kind is in the artifact
    only, so its note's label is the generic one."""
    out = copy.deepcopy(facts)
    for n in out["check_notes"]:
        if n["note"] == "rewrite":
            n["label"] = "Safe header rewrite"
    return out


def test_a_block_stored_before_the_words_gets_the_same_words_at_render_time() -> None:
    report = _plain_report("MEDIUM")
    report["facts"]["routines"] = _stored_before(FACTS)
    RoutineFacts.model_validate(report["facts"]["routines"])
    assert report_facts(report)["routines"] == _generic_rewrite_label(FACTS)
    # A block with the words is read as stored.
    report["facts"]["routines"] = {**FACTS, "others_line": "As stored."}
    assert report_facts(report)["routines"]["others_line"] == "As stored."


def test_a_block_emptied_by_a_model_round_trip_gets_the_words_too() -> None:
    """Through the 1.92 model a #1938 block gets None, False and [] for the new fields:
    they are filled in from the values, not only from missing keys."""
    dumped = RoutineFacts.model_validate(_stored_before(FACTS)).model_dump(mode="json")
    assert dumped["others_line"] is None and dumped["routines"][0]["status_text"] is None
    report = _plain_report("MEDIUM")
    report["facts"]["routines"] = dumped
    assert report_facts(report)["routines"] == _generic_rewrite_label(FACTS)


UI_FIXTURE = (
    Path(__file__).resolve().parents[3]
    / "src/ui/src/components/__tests__/fixtures/routineFacts.json"
)


def test_the_ui_fixture_is_the_facts_the_python_deliverables_print() -> None:
    """RoutineMigrationSection.test.js renders this file: the same facts as here."""
    assert json.loads(UI_FIXTURE.read_text(encoding="utf-8")) == FACTS


def test_every_reason_code_has_its_words() -> None:
    from src.shared.routine_facts import _WHY_CODE

    assert set(REASON_CODES) == set(_WHY_CODE)
