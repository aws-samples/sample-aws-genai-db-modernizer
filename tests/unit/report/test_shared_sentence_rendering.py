# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A sentence sibling design entries share is printed once, and each entry keeps its own
text (#2165).

Two real runs printed a long sentence twice: an ElastiCache design gave two unsupported
patterns the same closing sentence, and an Aurora MySQL design's model-written zero-date
note restated the sentence its column-default notes carry. The shapes are rebuilt here
with synthetic text on a saved report. These tests read only what main already has (the
deliverables' read path and the renderer), so they fail on main for what they check.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

import pytest

from src.agents.referee.migration_risks import build_migration_risks
from src.report.engineering_report import _app_layer_notes_md, render_engineering_report_md
from src.shared.design_text import design_prose_report, unsupported_pattern_detail
from tests.report_concision import md_sections, repeated_paragraphs, repeated_statements

REPO = Path(__file__).resolve().parents[3]
SAVED = REPO / "tests/fixtures/real_runs/wordpress-both/report.json"

STALE = (
    "A cached answer that is out of date makes the application write a duplicate row or "
    "update one that is gone."
)
STAYS = "The query stays on Aurora MySQL, uncached."
POST_INDEX = "Its key lookup uses the post index."
Q = ["1" * 64, "2" * 64, "3" * 64]


def _patterns() -> list[dict[str, Any]]:
    """Job 22613a50's shape: two patterns end with the same sentence and only the first
    workaround names an index; a third pattern shares nothing."""
    return [
        {
            "source_query_ids": [Q[0]],
            "reason": f"Query `{Q[0][:8]}` checks whether a post's metadata row exists. {STALE}",
            "workaround": f"{STAYS} {POST_INDEX}",
        },
        {
            "source_query_ids": [Q[1]],
            "reason": f"Query `{Q[1][:8]}` checks whether an order item's row exists. {STALE}",
            "workaround": STAYS,
        },
        {
            "source_query_ids": [Q[2]],
            "reason": f"Query `{Q[2][:8]}` pages through posts with an offset limit.",
            "workaround": STAYS,
        },
    ]


def _cache_report() -> dict[str, Any]:
    report: dict[str, Any] = json.loads(SAVED.read_text())
    patterns = _patterns()
    report["schema_designs"]["elasticache"]["unsupported_patterns"] = patterns
    risks = report["risk_assessment"]["risks"]
    [cache] = [r for r in risks if r.get("rule") == "cache.unsupported"]
    # The risk as synthesis builds it from the same design.
    [built] = [
        r
        for r in build_migration_risks(
            migration_waves=[],
            query_assignments=[],
            source_queries=[],
            schema_designs={"elasticache": {"unsupported_patterns": patterns}},
            cache_only_engines=["elasticache"],
            source_engine="mysql",
        )
        if r["rule"] == "cache.unsupported"
    ]
    cache.update(description=built["description"], details=built["details"])
    return report


def _cache_list(md: str) -> list[str]:
    target = next(b for h, b in md_sections(md, 3) if h.startswith("ElastiCache"))
    body = target.split("**Unsupported patterns", 1)[1].split("\n\n", 2)[1]
    return body.splitlines()


def test_a_shared_sentence_is_printed_once_and_each_query_keeps_its_own_workaround() -> None:
    md = render_engineering_report_md(design_prose_report(_cache_report()))
    lines = _cache_list(md)
    assert sum(STALE in ln for ln in md.splitlines()) == 1
    for repeated, _ in repeated_statements(md) + repeated_paragraphs(md):
        assert "out of date" not in repeated
    first = next(ln for ln in lines if f"`{Q[0][:8]}`" in ln)
    second = next(ln for ln in lines if f"`{Q[1][:8]}`" in ln)
    # Each query's line is its own: the index sentence stays with the query it is about.
    index = POST_INDEX.rstrip(".")
    assert index in first and index not in second
    assert f"`{Q[1][:8]}`" not in first and f"`{Q[0][:8]}`" not in second
    assert "post's metadata row" in first and "order item's row" in second
    assert first.startswith("  - ") and second.startswith("  - ")
    lead = lines[lines.index(first) - 1]
    assert lead == f"- For each of these: {STALE}"


@pytest.mark.passes_on_main  # guards that grouping never merges entries (review of #2168)
def test_the_cache_risk_count_is_the_number_of_listed_patterns() -> None:
    report = design_prose_report(_cache_report())
    md = render_engineering_report_md(report)
    [risk] = [r for r in report["risk_assessment"]["risks"] if r["rule"] == "cache.unsupported"]
    count = int(re.search(r"marks (\d+) patterns? unsupported", risk["description"]).group(1))
    entries = [ln for ln in _cache_list(md) if not ln.startswith("- For each of these:")]
    patterns = report["schema_designs"]["elasticache"]["unsupported_patterns"]
    assert count == len(entries) == len(patterns) == 3
    assert f"**Unsupported patterns ({count}):**" in md
    # The details still name every pattern in full, as the list holds them.
    assert risk["details"] == [unsupported_pattern_detail(p) for p in _patterns()]
    # Once the sentence is grouped, the list is the only place it is printed.
    assert STALE not in md.split("## Risk register", 1)[1]


def _default_note(column: str, literal: str) -> dict[str, Any]:
    # As the DDL generator writes one note per column (``ddl_generator``, #156).
    return {
        "feature": "column_default",
        "source_object": column,
        "recommendation": (
            f"DEFAULT '{literal}' is a MySQL zero date, which Aurora MySQL does not accept, so "
            "the target DDL uses DEFAULT NULL instead. Rows holding a zero date need a real "
            "date or NULL when the data is migrated."
        ),
    }


ROWS = "Rows holding a zero date need a real date or NULL when the data is migrated."


def test_a_date_and_a_datetime_column_keep_their_own_default_and_state_the_rest_once() -> None:
    design = {
        "status": "completed",
        "app_layer_notes": [
            _default_note("t.day", "0000-00-00"),
            _default_note("t.at", "0000-00-00 00:00:00"),
            _default_note("t.seen", "0000-00-00 00:00:00"),
        ],
    }
    before = "\n".join(_app_layer_notes_md(copy.deepcopy(design)))
    report = design_prose_report({"schema_designs": {"aurora_mysql": design}})
    after_design = report["schema_designs"]["aurora_mysql"]
    assert after_design["app_layer_notes"] == design["app_layer_notes"]  # entries untouched
    after = "\n".join(_app_layer_notes_md(after_design))
    assert after.count(ROWS) == 1
    day = next(ln for ln in after.splitlines() if "'0000-00-00'" in ln)
    at = next(ln for ln in after.splitlines() if "'0000-00-00 00:00:00'" in ln)
    assert day.endswith("Columns: `t.day`") or day.startswith("  - `t.day`:")
    assert "`t.at`, `t.seen`" in at and "`t.day`" not in at
    assert len(after) <= len(before)


def test_a_model_note_restating_a_column_default_sentence_states_it_once() -> None:
    model = {
        "feature": "zero_date_default",
        "source_object": "wp_posts, wp_comments and 2 other tables",
        "recommendation": (
            "Aurora MySQL rejects the zero date default. For NOT NULL date columns the target "
            f"DDL drops the default, so the application must set these dates on insert. {ROWS}"
        ),
    }
    design = {
        "status": "completed",
        "app_layer_notes": [model, _default_note("a.at", "0000-00-00 00:00:00")],
    }
    report = design_prose_report({"schema_designs": {"aurora_mysql": design}})
    md = "\n".join(_app_layer_notes_md(report["schema_designs"]["aurora_mysql"]))
    assert md.count(ROWS) == 1
    assert "set these dates on insert" in md
