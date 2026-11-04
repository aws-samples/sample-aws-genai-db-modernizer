# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for scripts/render_group_draft_agent.py: the shared-prefix group-draft agent (#763)."""

from __future__ import annotations

import json

import pytest

from scripts import render_group_draft_agent as render


def test_the_committed_agent_file_is_current() -> None:
    assert render.AGENT.read_text(encoding="utf-8") == render.render()


def test_the_agent_prompt_carries_the_task_skill_and_contract_verbatim() -> None:
    text = render.render()
    command = render.COMMAND.read_text(encoding="utf-8")
    task = command[command.index("## Group draft task") : command.index("\n## Merge fix task")]
    assert task.strip() in text
    assert render.SKILL.read_text(encoding="utf-8").strip() in text
    assert render.CONTRACT.read_text(encoding="utf-8").rstrip() in text
    # The order matters only for reading; the whole prompt is the shared prefix.
    assert text.index("## Group draft task") < text.index("<!-- skill start -->")
    assert text.index("<!-- skill end -->") < text.index("````python")


def test_the_agent_is_named_inherits_the_model_and_keeps_the_rules() -> None:
    head = render.render().split("---", 2)[1]
    assert "name: dynamodb-group-draft" in head
    assert "model: inherit" in head  # the same model as before: a cache lever, not a model one
    body = render.render()
    assert "Do not dispatch subagents yourself." in body
    assert "Unattended: do not ask the user anything." in body
    # Nothing group-specific may be in the shared prompt.
    for placeholder in ("{G}", "{INPUT_FILE}", "{DRAFT}", "{OTHER_GROUPS}"):
        assert placeholder not in body.split("## Group draft task", 1)[0], placeholder


def test_check_reports_a_stale_file(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    stale = tmp_path / "agent.md"
    stale.write_text("old\n")
    monkeypatch.setattr(render, "AGENT", stale)
    monkeypatch.setattr(render, "REPO_ROOT", tmp_path)
    assert render.main(["--check"]) == 1
    assert "--write" in capsys.readouterr().out
    assert render.main(["--write"]) == 0
    assert json.loads(capsys.readouterr().out) == {"wrote": ["agent.md"]}
    assert render.main(["--check"]) == 0


def test_the_agent_prompt_forbids_reloading_the_command_and_carries_the_tool_use_rule() -> None:
    """Review of #1828: the task text starts "Follow /design-schema-dynamodb", so the
    prompt says not to load it with the Skill tool, and backs up the tool-use rule."""
    from tests.unit.scripts.test_modernize_command import TOOL_USE_RULE

    head = " ".join(render.render().split("## Group draft task", 1)[0].split())
    assert (
        "Do not load `/design-schema-dynamodb` (or any other command or skill) with the Skill tool"
        in head
    )
    assert "## Tool Use " + TOOL_USE_RULE in head
