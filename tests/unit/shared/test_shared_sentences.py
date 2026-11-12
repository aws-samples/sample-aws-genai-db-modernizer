# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Sibling design entries that share a sentence are grouped once in report.json (#2165).

Two real runs printed a long sentence twice: an ElastiCache design gave two unsupported
patterns the same closing sentence, and an Aurora MySQL design's model-written zero-date
note restated the sentence its deterministic column-default notes carry. Synthesis now
groups them (``src.shared.shared_sentences``), the deliverables read a report through the
same pass, and the engineering report states the sentence once. The shapes are built
here with synthetic text.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.contracts.synthesis_output import SynthesisOutputContract
from src.report.engineering_report import render_engineering_report_md
from src.shared.design_text import design_prose_report, unsupported_pattern_detail
from src.shared.shared_sentences import GROUPED_FROM, grouped_report, merged_text
from src.tools.schema.aurora_common.constraint_translator import translate_default
from src.tools.schema.aurora_common.ddl_generator import excerpt
from tests.report_concision import repeated_paragraphs, repeated_statements

REPO = Path(__file__).resolve().parents[3]
SAVED_REPORTS = (
    # A job an older release wrote (v0.4.0-beta.2, contract 1.69).
    REPO / "tests/fixtures/old_jobs/v0.4.0-beta.2/wordpress/aa4e840a/synthesis/v2/report.json",
    REPO / "tests/fixtures/real_runs/wordpress-both/report.json",
)

STALE = (
    "A cached answer that is out of date makes the application write a duplicate row or "
    "update one that is gone."
)
STAYS = "The query stays on Aurora MySQL, uncached."
Q1, Q2, Q3 = ("a" * 64, "b" * 64, "c" * 64)


def _pattern(qid: str, own: str, workaround: str = STAYS) -> dict[str, Any]:
    return {
        "source_query_ids": [qid],
        "reason": f"Query `{qid[:8]}` {own} {STALE}",
        "workaround": workaround,
    }


def _cache_report() -> dict[str, Any]:
    """The ElastiCache shape: two unsupported patterns end with the same sentence, a
    third shares nothing, and the cache risk lists each one in its details."""
    patterns = [
        _pattern(
            Q1,
            "checks whether a post's metadata row exists before a write.",
            (f"{STAYS} Its key lookup uses the post index."),
        ),
        _pattern(Q2, "checks whether an order item's metadata row exists before a write."),
        {
            "source_query_ids": [Q3],
            "reason": f"Query `{Q3[:8]}` pages through posts with an offset limit.",
            "workaround": STAYS,
        },
    ]
    return {
        "schema_designs": {
            "elasticache": {"status": "completed", "unsupported_patterns": patterns}
        },
        "risk_assessment": {
            "risks": [
                {
                    "rule": "cache.unsupported",
                    "description": "[elasticache] The cache design marks 3 patterns unsupported.",
                    "details": [unsupported_pattern_detail(p) for p in patterns],
                },
                {"rule": "other", "description": "[elasticache] Other.", "details": ["x"]},
            ]
        },
    }


def test_two_unsupported_patterns_sharing_a_sentence_become_one_entry() -> None:
    report = _cache_report()
    before = copy.deepcopy(report)
    out = grouped_report(report)
    assert report == before  # the input is not changed
    entries = out["schema_designs"]["elasticache"]["unsupported_patterns"]
    assert len(entries) == 2
    grouped, alone = entries
    assert grouped["source_query_ids"] == [Q1, Q2]
    # Each entry's own sentence kept, the shared one stated once, after them.
    assert grouped["reason"] == (
        f"Query `{Q1[:8]}` checks whether a post's metadata row exists before a write. "
        f"Query `{Q2[:8]}` checks whether an order item's metadata row exists before a "
        f"write. {STALE}"
    )
    assert grouped["workaround"] == f"{STAYS} Its key lookup uses the post index."
    assert (
        grouped[GROUPED_FROM] == before["schema_designs"]["elasticache"]["unsupported_patterns"][:2]
    )
    assert alone == before["schema_designs"]["elasticache"]["unsupported_patterns"][2]
    # The cache risk's details list the grouped entry once, as the design summary does.
    risk, other = out["risk_assessment"]["risks"]
    assert risk["details"] == [unsupported_pattern_detail(e) for e in entries]
    assert other == before["risk_assessment"]["risks"][1]


def test_entries_with_no_shared_sentence_are_unchanged() -> None:
    report = _cache_report()
    patterns = report["schema_designs"]["elasticache"]["unsupported_patterns"]
    for i, p in enumerate(patterns):
        p["reason"] = p["reason"].replace(STALE, f"Its answer changes on write number {i}.")
    report["risk_assessment"]["risks"][0]["details"] = [
        unsupported_pattern_detail(p) for p in patterns
    ]
    assert grouped_report(report) is report
    # A shared short sentence (the workaround) is no reason to group.
    assert all(p["workaround"].startswith(STAYS) for p in patterns)
    assert design_prose_report(report)["schema_designs"] == report["schema_designs"]


def test_entries_of_another_pattern_type_or_a_short_shared_sentence_stay_apart() -> None:
    short = "Writes change it."
    entries = [
        {"pattern_type": "aggregation", "query_ids": [Q1], "recommendation": f"A. {STALE}"},
        {"pattern_type": "full_text", "query_ids": [Q2], "recommendation": f"B. {STALE}"},
        {"pattern_type": "aggregation", "query_ids": [Q3], "recommendation": f"C. {short}"},
        {"pattern_type": "aggregation", "query_ids": ["d" * 64], "recommendation": f"D. {short}"},
    ]
    report = {"schema_designs": {"dynamodb": {"unsupported_patterns": entries}}}
    assert grouped_report(report) is report


def test_a_group_through_a_middle_entry_lists_every_query_once() -> None:
    other = "Every write to a post changes the page and the total row count of the list."
    entries = [
        {"query_ids": [Q1], "recommendation": f"A. {STALE}"},
        {"query_ids": [Q2, Q1], "recommendation": f"B. {STALE} {other}"},
        {"query_ids": [Q3], "recommendation": f"{other} C."},
    ]
    out = grouped_report({"schema_designs": {"opensearch": {"unsupported_patterns": entries}}})
    [entry] = out["schema_designs"]["opensearch"]["unsupported_patterns"]
    assert entry["query_ids"] == [Q1, Q2, Q3]
    assert entry["recommendation"] == f"A. B. C. {STALE} {other}"
    assert grouped_report(out) is out  # idempotent


def test_migration_notes_sharing_a_sentence_become_one_note() -> None:
    logic = (
        "Move the counter update into the application code that writes the row, in the "
        "same transaction."
    )
    notes = [
        {"object_type": "trigger", "object_name": "t_one", "application_logic_required": logic},
        {"object_type": "trigger", "object_name": "t_two", "application_logic_required": logic},
        {"object_type": "procedure", "object_name": "p", "application_logic_required": logic},
    ]
    out = grouped_report({"schema_designs": {"dynamodb": {"migration_notes": notes}}})
    triggers, procedure = out["schema_designs"]["dynamodb"]["migration_notes"]
    assert triggers["object_name"] == "t_one, t_two"
    assert triggers["application_logic_required"] == logic
    assert procedure == notes[2]


def _default_note(literal: str, aurora_type: str, nullable: bool) -> str:
    """A column-default note's text as the DDL generator writes it (one per column)."""
    reason = translate_default(
        literal,
        source_engine="mysql",
        target="aurora_mysql",
        aurora_type=aurora_type,
        nullable=nullable,
    ).reason
    assert reason
    return f"DEFAULT {excerpt(literal)} {reason}"


# The zero-date shape: the sentence is deterministic (``constraint_translator``, one note
# per column), and a model-written note restated it.
def _zero_date_notes() -> list[dict[str, Any]]:
    nullable = _default_note("0000-00-00 00:00:00", "DATETIME", True)
    not_null = _default_note("0000-00-00 00:00:00", "DATETIME", False)
    rows = "Rows holding a zero date need a real date or NULL when the data is migrated."
    assert rows in nullable and rows not in not_null
    model = {
        "feature": "zero_date_default",
        "source_object": "wp_posts, wp_comments and 2 other tables",
        "recommendation": (
            "Aurora MySQL rejects the zero date default. For NOT NULL date columns the target "
            f"DDL drops the default, so the application must set these dates on insert. {rows}"
        ),
    }
    notes = [model]
    for column in ("a.created", "a.updated"):
        notes.append(
            {"feature": "column_default", "source_object": column, "recommendation": nullable}
        )
    for column in ("b.created", "b.updated"):
        notes.append(
            {"feature": "column_default", "source_object": column, "recommendation": not_null}
        )
    return notes


def test_a_model_note_restating_a_column_default_sentence_drops_it() -> None:
    notes = _zero_date_notes()
    before = copy.deepcopy(notes)
    out = grouped_report({"schema_designs": {"aurora_mysql": {"app_layer_notes": notes}}})
    after = out["schema_designs"]["aurora_mysql"]["app_layer_notes"]
    assert notes == before
    # One entry per column still: the risk and the report count columns.
    assert len(after) == len(before)
    assert after[1:] == before[1:]
    assert after[0]["recommendation"] == (
        "Aurora MySQL rejects the zero date default. For NOT NULL date columns the target "
        "DDL drops the default, so the application must set these dates on insert."
    )
    md = "\n".join(_render_designs(out))
    assert md.count("Rows holding a zero date need a real date or NULL") == 1


def test_column_default_notes_with_other_texts_that_share_a_sentence_read_as_one_line() -> None:
    date = _default_note("0000-00-00", "DATE", True)
    stamp = _default_note("0000-00-00 00:00:00", "DATETIME", True)
    assert date != stamp
    notes = [
        {"feature": "column_default", "source_object": "t.day", "recommendation": date},
        {"feature": "column_default", "source_object": "t.at", "recommendation": stamp},
    ]
    out = grouped_report({"schema_designs": {"aurora_mysql": {"app_layer_notes": notes}}})
    after = out["schema_designs"]["aurora_mysql"]["app_layer_notes"]
    assert [n["source_object"] for n in after] == ["t.day", "t.at"]
    assert after[0]["recommendation"] == after[1]["recommendation"]
    lines = [ln for ln in _render_designs(out) if ln.startswith("- ")]
    assert len(lines) == 1 and "Columns: `t.day`, `t.at`." in lines[0]
    assert lines[0].count("Rows holding a zero date") == 1


def test_a_note_with_nothing_of_its_own_left_is_dropped() -> None:
    reason = _default_note("0000-00-00", "DATE", True)
    notes = [
        {"feature": "zero_date", "source_object": "t", "recommendation": reason},
        {"feature": "column_default", "source_object": "t.day", "recommendation": reason},
    ]
    out = grouped_report({"schema_designs": {"aurora_mysql": {"app_layer_notes": notes}}})
    assert out["schema_designs"]["aurora_mysql"]["app_layer_notes"] == notes[1:]


def test_merged_text_keeps_a_shared_opening_first() -> None:
    assert merged_text([f"{STAYS} Its own part.", STAYS]) == f"{STAYS} Its own part."
    assert merged_text(["One.", "One."]) == "One."


def _render_designs(report: dict[str, Any]) -> list[str]:
    from src.report.engineering_report import _app_layer_notes_md

    out: list[str] = []
    for design in report["schema_designs"].values():
        out += _app_layer_notes_md(design)
    return out


def _load(path: Path) -> dict[str, Any]:
    report: dict[str, Any] = json.loads(path.read_text())
    return report


def _with_shared_cache_sentence(report: dict[str, Any]) -> dict[str, Any]:
    """``report`` with its cache design's unsupported patterns given the shared shape."""
    report = copy.deepcopy(report)
    design = report["schema_designs"].setdefault("elasticache", {"status": "completed"})
    patterns = _cache_report()["schema_designs"]["elasticache"]["unsupported_patterns"]
    design["status"] = "completed"
    design["unsupported_patterns"] = patterns
    risks = report["risk_assessment"]["risks"]
    for r in risks:
        if r.get("rule") == "cache.unsupported":
            r["details"] = [unsupported_pattern_detail(p) for p in patterns]
    return report


@pytest.mark.parametrize("path", SAVED_REPORTS, ids=["v0.4.0-beta.2", "wordpress-both"])
def test_a_report_written_before_the_grouping_loads_and_renders_the_sentence_once(
    path: Path,
) -> None:
    report = _with_shared_cache_sentence(_load(path))
    assert "grouped_from" not in json.dumps(report)
    SynthesisOutputContract.model_validate(report)  # an old report still loads
    assert repeated_statements(render_engineering_report_md(report))  # as main renders it
    # The deliverables read a report through the same pass synthesis applies.
    out = design_prose_report(report)
    SynthesisOutputContract.model_validate(out)  # the grouped form loads too
    md = render_engineering_report_md(out)
    assert md.count(STALE.rstrip(".")) == 1
    for repeated, _ in repeated_statements(md) + repeated_paragraphs(md):
        assert STALE.rstrip(".") not in repeated
    # Every query of the group is still named.
    assert f"`{Q1[:8]}`" in md and f"`{Q2[:8]}`" in md


def test_synthesis_writes_the_grouped_entries() -> None:
    from src.agents.referee.synthesis_handler import (
        _write_synthesis_report,
        run_synthesis_deterministic,
    )
    from tests.unit.agents.referee.test_synthesis_llm_seam import _SCHEMA_DDB, _mock_store

    schema = copy.deepcopy(_SCHEMA_DDB)
    schema["unsupported_patterns"] = [
        {"pattern_type": "aggregation", "query_ids": ["DDB-AP-1"], "recommendation": f"A. {STALE}"},
        {"pattern_type": "aggregation", "query_ids": ["DDB-AP-2"], "recommendation": f"B. {STALE}"},
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
    [entry] = report["schema_designs"]["dynamodb"]["unsupported_patterns"]
    assert entry["query_ids"] == ["DDB-AP-1", "DDB-AP-2"]
    assert entry["recommendation"] == f"A. B. {STALE}"
    assert len(entry[GROUPED_FROM]) == 2
    SynthesisOutputContract.model_validate(report)
