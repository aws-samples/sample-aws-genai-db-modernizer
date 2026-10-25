"""The Claude Code commands and skills carry the customer's engine scope (#1574).

A tripwire on the command text, in the style of ``test_modernize_command.py``: the real
proof is a scoped headless run. Each test names the item of #1574 it guards.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit.scripts.test_modernize_command import (
    COMMANDS_WITH_TOOL_USE_RULE,
    SEARCH_AS_IS,
    TOOL_USE_RULE,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMANDS = REPO_ROOT / ".claude" / "commands"
SKILLS = REPO_ROOT / "src" / "skills"


def _text(name: str) -> str:
    return " ".join((COMMANDS / name).read_text().split())


def test_collect_and_triage_take_the_scope_and_never_ask_for_it() -> None:
    # Item 6: the standalone path gives the scope to triage, which records it.
    collect = _text("collect.md")
    assert "`--target-databases <ids>`" in collect and "Never ask for it" in collect
    assert "`/triage --target-databases <ids>`" in collect
    assert TOOL_USE_RULE in collect
    triage = _text("triage.md")
    assert "`--target-databases <ids>`" in triage and "Never ask for it" in triage
    assert "add `--target-databases <ids>` to the command" in triage
    source = (REPO_ROOT / "scripts" / "run_triage.py").read_text()
    assert '"--target-databases"' in source


def test_triage_shows_the_scope_and_its_skips_and_its_gate_refuses_outside_engines() -> None:
    triage = _text("triage.md")
    assert "`engine_scope`" in triage and "(customer's choice)" in triage
    assert "`skipped_reasons`" in triage
    assert "Not in the customer's engine scope" in triage
    gate = triage.split("Decision gate", 1)[1]
    assert "Refuse any engine outside `engine_scope`" in gate
    assert "never add it to the selection" in gate


def test_no_command_says_all_selected_engines_or_all_engines() -> None:
    # Item 7: with a scope, "all engines" reads as every engine, not the selection.
    for name in ("analyze.md", "assign.md", "design-schema.md"):
        text = _text(name)
        assert "all selected engines" not in text, name
        assert "all engines" not in text, name


def test_assign_shows_the_queries_with_no_engine() -> None:
    assign = _text("assign.md")
    assert "`unservable_queries`" in assign
    assert "no engine in the customer's engine scope can serve them" in assign


def test_design_schema_designs_the_schema_design_engines() -> None:
    text = _text("design-schema.md")
    assert "exactly the engines in `schema_design_engines`" in text
    assert "never re-derived from `after_distribution`" in text
    modernize = _text("modernize.md")
    assert "Design exactly the engines in `schema_design_engines`" in modernize


def test_modernize_quotes_the_unservable_number_rather_than_stating_it() -> None:
    # Item 4: the argument's rule no longer asks for a figure the gate rule forbids.
    modernize = _text("modernize.md")
    args = modernize.split("## Arguments", 1)[1].split("## Step 0", 1)[0]
    assert "Say the number once" not in args
    assert "The gate block and the final summary state the scope and that number" in args
    assert "never state the number yourself" in args
    gate = modernize.split("### Decision Gate: Assignment Approval", 1)[1].split("###", 1)[0]
    assert "add no table or figure of your own" in gate
    assert "the customer's engine scope and how many queries have no engine in it" in gate


def test_modernize_describes_the_hottest_cache_candidates() -> None:
    # Item 8.
    gate = _text("modernize.md").split("### Decision Gate: Assignment Approval", 1)[1]
    assert "`Hottest cache candidates:`" in gate.split("###", 1)[0]


def test_modernize_writes_no_waiting_lines_under_auto() -> None:
    # Item 8: an --auto run printed about 40 "Part N is done. Still waiting ..." lines.
    modernize = _text("modernize.md")
    assert "Under `--auto` or `--mode ui`, skip the progress lines" in modernize
    assert "write no other text while subagents run" in modernize
    assert '"still waiting on ..."' in modernize
    # And it waits on the status lines, never on its own tally (the stack run).
    assert "is never your own tally of reports: the status lines say it" in modernize


def test_the_dynamodb_group_input_lists_the_effective_architecture() -> None:
    # Item 9: the group input carries it; the command now says so.
    text = _text("design-schema-dynamodb.md")
    step = text.split("Read your group input", 1)[1].split("2. Read the domain expertise", 1)[0]
    assert "`effective_architecture`" in step


@pytest.mark.parametrize("skill", sorted(p.name for p in SKILLS.glob("*-pe-review.md")))
def test_every_pe_review_skill_forbids_quoting_scores(skill: str) -> None:
    # Item 9: the rule the designer skills got in 3d917c6b.
    text = " ".join((SKILLS / skill).read_text().split())
    assert "Never quote the analysis's own scores" in text
    assert '"NN% confidence"' in text


@pytest.mark.passes_on_main  # guards the parametrized list above: six PE review skills
def test_the_pe_review_skills_are_all_covered() -> None:
    assert len(list(SKILLS.glob("*-pe-review.md"))) == 6


def test_every_command_keeps_the_tool_use_rule_word_for_word() -> None:
    # Every command that carries the rule, the ones this change edits among them
    # (/analyze, /assign and /triage gained it here).
    for name in COMMANDS_WITH_TOOL_USE_RULE:
        assert TOOL_USE_RULE + SEARCH_AS_IS in (COMMANDS / name).read_text(), name
    for name in ("analyze.md", "assign.md", "triage.md", "collect.md", "design-schema.md"):
        assert name in COMMANDS_WITH_TOOL_USE_RULE, name
