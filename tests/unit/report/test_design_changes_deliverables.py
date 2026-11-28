# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2289: every deliverable states what an Aurora design changes from the source, from one fact.

Synthesis compares each design table with its collected table and stores the rows on the
design summary; ``facts.design_changes`` reads them once: each table's kinds of change, the
counts by kind and the executive report's count line. The engineering report lists each
change, the executive report prints the count line only (it names no table) and the
results page shows the badges (Jest). A report written before the fact gets it at read
time; a summary written before the comparison has no rows, so no deliverable changes.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from src.contracts.synthesis_output import ReportFacts
from src.report.engineering_report import render_engineering_report_md
from src.report.executive_report import build_story
from src.report.facts import report_facts
from src.shared.design_changes import design_change_facts
from src.shared.report_facts import compute_report_facts

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_report.json"
UNSAFE = "<img src=x onerror=alert(1)>|"


def _column(name: str, change: str, source: str | None = "int", aurora: str | None = "INT"):
    return {"name": name, "aurora_type": aurora, "source_type": source, "nullable": True,
            "default": None, "identity": False, "change": change}  # fmt: skip


def _index(name: str, change: str, **extra: Any) -> dict[str, Any]:
    return {"name": name, "columns": ["a"], "unique": False, "change": change,
            "source_name": None, "differences": [], "source_columns": None,
            "source_unique": None, "reason": None, **extra}  # fmt: skip


SAME = {"source_table": "wordpress.x", "columns": [_column("id", "same")], "indexes": []}
CHANGED = {
    "source_table": "wordpress.wp_posts",
    "columns": [
        _column("id", "same"),
        _column(UNSAFE, "type_changed", "varchar(20)", "TEXT"),
        _column("gone", "skipped", aurora=None),
    ],
    "indexes": [
        _index("k", "same"),
        _index("extra", "added"),
        _index("new_name", "renamed", source_name="old_name"),
        _index("dropped", "skipped", reason="the table already has an index named dropped"),
        _index("uq", "changed", differences=["unique"], source_columns=["a"], source_unique=True),
        _index("lower_a", "step1", columns=[]),
    ],
}


def _plain() -> dict:
    report: dict = json.loads(FIXTURE.read_text())
    assert report.get("facts") is None
    return report


def _compared() -> dict:
    report = _plain()
    design = report["schema_designs"]["aurora_mysql"]
    design["source_compared"] = True
    for table in design["tables"]:
        changes = CHANGED if table["table_name"] == "wp_posts" else SAME
        table["source_changes"] = copy.deepcopy(changes)
    design["tables"].append(
        {"table_name": "audit", "source_changes": {"source_table": None,
         "columns": [_column("id", "added", source=None)], "indexes": []}}
    )  # fmt: skip
    design["tables"].append(
        {"table_name": "orders", "source_changes": {"uncompared": "ambiguous_source"}}
    )
    return report


def _story_text(report: dict) -> str:
    return " ".join(getattr(f, "text", "") or "" for f in build_story(report))


BRIEF = (
    "Compared with the source, the Aurora MySQL design changes 2 of 10 tables: 1 table "
    "added, 1 column not created, 1 column type changed, 1 index added, 1 index renamed, "
    "1 index changed and 1 index not created. Step 1 recreates 1 expression index the "
    "design script does not create, when it moves its table. 1 table of the design is "
    "not compared. The engineering "
    "report and the results page name each one."
)


def test_synthesis_states_each_tables_flag_kinds_and_the_counts_once() -> None:
    facts = compute_report_facts(_compared())
    (entry,) = facts["design_changes"]
    assert entry["engine"] == "aurora_mysql"
    assert (entry["table_count"], entry["changed_table_count"]) == (10, 2)
    assert entry["change_counts"] == {
        "table_added": 1, "column_skipped": 1, "type_changed": 1, "index_added": 1,
        "index_renamed": 1, "index_changed": 1, "index_skipped": 1, "index_step1": 1,
    }  # fmt: skip
    posts = next(t for t in entry["table_changes"] if t["table_name"] == "wp_posts")
    assert posts["changed"] is True
    assert posts["change_kinds"] == [
        "column_skipped", "type_changed", "index_added", "index_renamed", "index_changed",
        "index_skipped", "index_step1",
    ]  # fmt: skip
    assert (
        next(t for t in entry["table_changes"] if t["table_name"] == "wp_users")["changed"] is False
    )
    assert entry["uncompared_tables"] == [
        {"table_name": "orders", "schema_name": None, "reason_code": "ambiguous_source",
         "reason": "two source schemas hold a table of its name and the design names none"}
    ]  # fmt: skip
    assert entry["uncompared_table_count"] == 1
    assert entry["brief_line"] == BRIEF
    # The rows are stored once, on the summary: the fact holds the flag, kinds and counts.
    assert "columns" not in posts and "indexes" not in posts
    ReportFacts.model_validate(facts)


def test_a_translated_only_table_is_no_change_and_counts_on_its_own_line() -> None:
    report = _plain()
    design = report["schema_designs"]["aurora_mysql"]
    design["source_compared"] = True
    for table in design["tables"]:
        table["source_changes"] = {
            "source_table": "x", "indexes": [],
            "columns": [_column("a", "translated"), _column("b", "translated")],
        }  # fmt: skip
    (entry,) = design_change_facts(report)
    assert entry["changed_table_count"] == 0
    assert {t["changed"] for t in entry["table_changes"]} == {False}
    assert entry["table_changes"][0]["change_kinds"] == ["translated"]
    assert entry["brief_line"] == (
        "Compared with the source, the Aurora MySQL design keeps all 9 tables as collected. "
        "It translates 18 column types as the deterministic translation to Aurora MySQL "
        "writes them. The engineering report and the results page name each one."
    )


def test_a_design_with_no_change_says_so() -> None:
    report = _compared()
    tables = report["schema_designs"]["aurora_mysql"]["tables"]
    tables[:] = [t for t in tables if t["table_name"] != "orders"]
    for table in tables:
        table["source_changes"] = copy.deepcopy(SAME)
    (entry,) = design_change_facts(report)
    assert entry["brief_line"] == (
        "Compared with the source, the Aurora MySQL design keeps all 10 tables as collected."
    )


def test_a_design_with_no_compared_table_states_only_what_is_not_compared() -> None:
    report = _plain()
    design = report["schema_designs"]["aurora_mysql"]
    design["source_compared"] = True
    for table in design["tables"]:
        table["source_changes"] = {"uncompared": "ambiguous_source"}
    (entry,) = design_change_facts(report)
    assert entry["table_count"] == 0
    assert entry["brief_line"] == (
        "9 tables of the design are not compared. The engineering report and the results "
        "page name each one."
    )


def test_a_summary_written_before_the_comparison_states_nothing() -> None:
    assert design_change_facts(_plain()) == []
    assert compute_report_facts(_plain())["design_changes"] == []
    plain = _plain()
    plain["facts"] = compute_report_facts(plain)
    assert "Changes from the source" not in render_engineering_report_md(plain)
    assert "Compared with the source" not in _story_text(plain)


def test_a_block_written_before_2289_gains_the_fact_at_read_time() -> None:
    report = _compared()
    stored = compute_report_facts(report)
    expected = stored.pop("design_changes")
    stored["engine_count"] = 99  # a stored figure is never recomputed
    for block in (stored, {**stored, "design_changes": None}):
        report["facts"] = block
        facts = report_facts(report)
        assert facts["design_changes"] == expected
        assert facts["engine_count"] == 99
    report["facts"] = {**stored, "design_changes": []}
    assert report_facts(report)["design_changes"] == []


def test_the_engineering_report_lists_each_change_with_its_reason_escaped() -> None:
    report = _compared()
    report["facts"] = compute_report_facts(report)
    md = render_engineering_report_md(report)
    assert f"**Changes from the source:** {BRIEF}" in md
    assert "| Table | Object | Change | Source | Design | Why |" in md
    assert "| `audit` | table | Added | - | - | - |" in md
    assert "| `wp_posts` | index `extra` | Added | - | `a` | - |" in md
    assert "| `wp_posts` | index `new_name` | Renamed | `old_name` | `a` | - |" in md
    assert (
        "| `wp_posts` | index `dropped` | Not created | `a` | - | the table already has an "
        "index named dropped |"
    ) in md
    assert "| `wp_posts` | index `uq` | Changed (uniqueness) | `a` (unique) | `a` | - |" in md
    assert (
        "| `wp_posts` | index `lower_a` | Recreated by step 1 when it moves the table | - | - | - |"
        in md
    )
    assert "| `wp_posts` | column `gone` | Not created | `int` | - | - |" in md
    assert (
        "| `orders` | table | Not compared | - | - | two source schemas hold a table of its "
        "name and the design names none |"
    ) in md
    # Untrusted names stay in their cell, as text.
    assert "<img" not in md
    assert "`&lt;img src=x onerror=alert(1)&gt;\\|` | Type changed | `varchar(20)` | `TEXT` |" in md
    # Only changes are listed; a same row is not.
    assert "index `k`" not in md


def test_the_executive_report_prints_the_count_line_and_names_no_table() -> None:
    report = _compared()
    report["facts"] = compute_report_facts(report)
    text = _story_text(report)
    assert BRIEF in text
    assert "wp_posts" not in text and "audit" not in text and "new_name" not in text


def test_the_summary_stores_the_comparison_with_its_flag() -> None:
    from src.agents.referee.synthesis_handler import _build_aurora_schema_summary

    design = {
        "table_definitions": [{"table_name": "t", "columns": [{"name": "id", "aurora_type": "INT"}],
                               "primary_key": ["id"], "indexes": [], "foreign_keys": []}],
        "migration_strategy": "carry_over",
    }  # fmt: skip
    collector = {
        "metadata": {"source_database": {"engine": "mysql", "database_name": "db"}},
        "database_schema": {"tables": [{"table_id": "db.t", "table_name": "t",
                            "schema_name": "db", "columns": [{"column_name": "id",
                            "data_type": "int", "nullable": False}]}]},
    }  # fmt: skip
    summary = _build_aurora_schema_summary("aurora_mysql", design, collector)
    assert summary["source_compared"] is True
    changes = summary["tables"][0]["source_changes"]
    assert changes["source_table"] == "db.t"
    assert [c["change"] for c in changes["columns"]] == ["same"]
    without = _build_aurora_schema_summary("aurora_mysql", design)
    assert without["source_compared"] is False
    assert "source_changes" not in without["tables"][0]
