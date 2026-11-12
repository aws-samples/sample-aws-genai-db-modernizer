# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Which sibling design entries share a sentence, recorded once in report.json (#2165).

Synthesis records a group of entries that share a long sentence next to the entries,
which stay as the design wrote them (``shared_sentence_groups``); the renderers state
the sentence once and print each entry with its own text. Synthetic text throughout.
The rendering itself is checked in ``tests/unit/report/test_shared_sentence_rendering``.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.contracts.synthesis_output import SynthesisOutputContract
from src.shared.design_text import design_prose_report
from src.shared.shared_sentences import (
    GROUPS_KEY,
    grouped_report,
    sentence_groups,
    shared_layout,
)

REPO = Path(__file__).resolve().parents[3]
SAVED_REPORTS = (
    # A job an older release wrote (v0.4.0-beta.2, contract 1.69).
    REPO / "tests/fixtures/old_jobs/v0.4.0-beta.2/wordpress/aa4e840a/synthesis/v2/report.json",
    REPO / "tests/fixtures/real_runs/wordpress-both/report.json",
)
# 15 and 14 words.
LONG = "A cached answer that is out of date makes the application write a duplicate twice."
SHORT = "A cached answer that is out of date makes the application write a duplicate."
assert (len(LONG.split()), len(SHORT.split())) == (15, 14)
TEXT = ("recommendation",)


def _e(text: str, kind: str = "") -> dict[str, Any]:
    return {"recommendation": text, "pattern_type": kind}


def _groups(entries: list[dict[str, Any]], by_text: bool = False) -> list[dict[str, Any]]:
    return sentence_groups(entries, TEXT, lambda e: str(e.get("pattern_type")), by_text)


def test_a_15_word_sentence_is_shared_and_a_14_word_one_is_not() -> None:
    assert _groups([_e(f"A. {LONG}"), _e(f"B. {LONG}")]) == [
        {"sentences": [LONG], "indexes": [0, 1]}
    ]
    assert _groups([_e(f"A. {SHORT}"), _e(f"B. {SHORT}")]) == []


@pytest.mark.parametrize(
    "other",
    [
        LONG.replace("duplicate", "second"),  # one word different
        LONG.replace("twice", "2 times"),  # a number
        LONG.replace("duplicate", "`wp_posts` duplicate"),  # an id or a table name
    ],
)
def test_a_near_duplicate_sentence_is_not_shared(other: str) -> None:
    assert _groups([_e(f"A. {LONG}"), _e(f"B. {other}")]) == []


def test_case_bold_and_the_closing_stop_are_folded() -> None:
    other = f"**{LONG.upper().rstrip('.')}**"
    assert _groups([_e(f"A. {LONG}"), _e(f"B. {other}")])[0]["indexes"] == [0, 1]


def test_entries_of_another_kind_are_never_grouped() -> None:
    assert _groups([_e(f"A. {LONG}", "aggregation"), _e(f"B. {LONG}", "full_text")]) == []


@pytest.mark.passes_on_main  # main never grouped app-layer notes of any feature
def test_a_trigger_note_and_an_event_note_are_never_grouped() -> None:
    notes = [
        {"feature": "trigger", "source_object": "trg_orders_audit", "recommendation": LONG},
        {"feature": "event", "source_object": "ev_purge_sessions", "recommendation": LONG},
    ]
    report = {"schema_designs": {"aurora_mysql": {"app_layer_notes": notes}}}
    assert grouped_report(report) is report


def test_identical_notes_are_one_line_already_and_form_no_group() -> None:
    notes = [
        {"feature": "column_default", "source_object": c, "recommendation": LONG} for c in "ab"
    ]
    assert sentence_groups(notes, TEXT, lambda e: e["feature"], by_text=True) == []


def test_an_entry_is_in_one_group_and_two_shared_sentences_make_one_group() -> None:
    other = "Every write to a post changes the page and the total row count of this list."
    both = [_e(f"{LONG} {other}"), _e(f"{LONG} {other} Own."), _e("C.")]
    assert _groups(both) == [{"sentences": [LONG, other], "indexes": [0, 1]}]
    # A sentence the group's entries share with a third entry stays in their text: no
    # entry is in two groups, and no lead is stated for an entry it does not cover.
    overlap = [_e(f"{LONG} {other}"), _e(f"{LONG} {other} Own."), _e(f"{other} C.")]
    assert _groups(overlap) == [{"sentences": [LONG], "indexes": [0, 1]}]


def test_the_layout_keeps_every_entry_and_its_own_text() -> None:
    entries = [
        {"source_query_ids": ["q1"], "reason": f"Q1 reads. {LONG}", "workaround": "W1."},
        {"source_query_ids": ["q2"], "reason": "Q2 stands alone.", "workaround": "W2."},
        {"source_query_ids": ["q3"], "reason": f"Q3 reads. {LONG}", "workaround": "W3."},
    ]
    groups = [{"list": "unsupported_patterns", "sentences": [LONG], "indexes": [0, 2]}]
    blocks = shared_layout(entries, groups, "unsupported_patterns")
    assert blocks == [
        ([LONG], [{**entries[0], "reason": "Q1 reads."}, {**entries[2], "reason": "Q3 reads."}]),
        ([], [entries[1]]),
    ]
    # A record for another list, or one out of range, changes nothing.
    bad = [{"list": "migration_notes", "sentences": [LONG], "indexes": [0, 2]}]
    bad.append({"list": "unsupported_patterns", "sentences": [LONG], "indexes": [0, 9]})
    assert shared_layout(entries, bad, "unsupported_patterns") == [([], [e]) for e in entries]


def test_the_report_records_the_group_and_leaves_the_entries_as_written() -> None:
    patterns = [
        {"source_query_ids": ["q1"], "reason": f"Q1. {LONG}", "workaround": "W1."},
        {"source_query_ids": ["q2"], "reason": f"Q2. {LONG}", "workaround": "W2."},
    ]
    report = {"schema_designs": {"elasticache": {"unsupported_patterns": patterns}}}
    before = copy.deepcopy(report)
    out = grouped_report(report)
    assert report == before
    design = out["schema_designs"]["elasticache"]
    assert design["unsupported_patterns"] == patterns
    assert design[GROUPS_KEY] == [
        {"list": "unsupported_patterns", "sentences": [LONG], "indexes": [0, 1]}
    ]
    assert grouped_report(out) is out  # idempotent


def test_entries_with_no_shared_sentence_are_unchanged() -> None:
    report = {
        "schema_designs": {
            "elasticache": {
                "unsupported_patterns": [
                    {"source_query_ids": ["q1"], "reason": "Q1 changes on each write."},
                    {"source_query_ids": ["q2"], "reason": "Q2 returns no rows."},
                ]
            }
        }
    }
    assert grouped_report(report) is report
    assert design_prose_report(report)["schema_designs"] == report["schema_designs"]


def test_a_restating_note_keeps_its_text_when_nothing_of_its_own_would_be_left() -> None:
    notes = [
        {"feature": "zero_date", "source_object": "t", "recommendation": LONG},
        {"feature": "column_default", "source_object": "t.day", "recommendation": f"D. {LONG}"},
    ]
    report = {"schema_designs": {"aurora_mysql": {"app_layer_notes": notes}}}
    assert grouped_report(report) is report


@pytest.mark.parametrize("path", SAVED_REPORTS, ids=["v0.4.0-beta.2", "wordpress-both"])
def test_a_report_written_before_the_groups_loads(path: Path) -> None:
    report = json.loads(path.read_text())
    assert GROUPS_KEY not in json.dumps(report)
    SynthesisOutputContract.model_validate(report)
    design = report["schema_designs"].setdefault("elasticache", {"status": "completed"})
    design["unsupported_patterns"] = [
        {"source_query_ids": ["q1"], "reason": f"Q1. {LONG}"},
        {"source_query_ids": ["q2"], "reason": f"Q2. {LONG}"},
    ]
    out = design_prose_report(report)
    assert out["schema_designs"]["elasticache"][GROUPS_KEY][0]["indexes"] == [0, 1]
    SynthesisOutputContract.model_validate(out)


def test_synthesis_writes_the_group_next_to_the_entries() -> None:
    from src.agents.referee.synthesis_handler import (
        _write_synthesis_report,
        run_synthesis_deterministic,
    )
    from tests.unit.agents.referee.test_synthesis_llm_seam import _SCHEMA_DDB, _mock_store

    schema = copy.deepcopy(_SCHEMA_DDB)
    schema["unsupported_patterns"] = [
        {"pattern_type": "aggregation", "query_ids": ["DDB-AP-1"], "recommendation": f"A. {LONG}"},
        {"pattern_type": "aggregation", "query_ids": ["DDB-AP-2"], "recommendation": f"B. {LONG}"},
    ]
    store = _mock_store()
    read = store.read_json.side_effect

    def read_json(p: str) -> dict:
        if "schema-dynamodb/" in p and p.endswith("schema_output.json"):
            return schema
        found: dict = read(p)
        return found

    store.read_json.side_effect = read_json
    result = run_synthesis_deterministic("job-1", "mydb", store, assignment_version=0)
    _write_synthesis_report(store, result, assignment_version=0)
    [report] = [v for k, v in store._written.items() if k.endswith("report.json")]
    design = report["schema_designs"]["dynamodb"]
    assert [e["recommendation"] for e in design["unsupported_patterns"]] == [
        f"A. {LONG}",
        f"B. {LONG}",
    ]
    assert design[GROUPS_KEY] == [
        {"list": "unsupported_patterns", "sentences": [LONG], "indexes": [0, 1]}
    ]
    SynthesisOutputContract.model_validate(report)


def test_two_triggers_with_the_same_note_form_a_group() -> None:
    # Discourse e2e-llm run (#2181): two triggers got the same note word for word. Only
    # column-default notes with one text share a line, so these two printed it twice.
    notes = [
        {"feature": "trigger", "source_object": t, "recommendation": f"Carried over. {LONG}"}
        for t in ("trg_a", "trg_b")
    ]
    design = grouped_report({"schema_designs": {"aurora_postgresql": {"app_layer_notes": notes}}})
    groups = design["schema_designs"]["aurora_postgresql"][GROUPS_KEY]
    assert groups == [{"list": "app_layer_notes", "sentences": [LONG], "indexes": [0, 1]}]


@pytest.mark.passes_on_main  # guards that #2181 keeps the column-default rule
def test_column_default_notes_with_one_text_still_form_no_group() -> None:
    # The column-default line already names every column whose note reads the same.
    notes = [
        {"feature": "column_default", "source_object": c, "recommendation": LONG} for c in "ab"
    ]
    report = {"schema_designs": {"aurora_mysql": {"app_layer_notes": notes}}}
    assert grouped_report(report) is report
