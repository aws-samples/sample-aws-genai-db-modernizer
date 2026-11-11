# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Tests for scripts/render_part_review_agent.py: the shared-prefix DynamoDB part-review
agent (#2120)."""

from __future__ import annotations

import json

import pytest

from scripts import render_part_review_agent as render


def test_the_committed_agent_file_is_current() -> None:
    assert render.AGENT.read_text(encoding="utf-8") == render.render()


def test_the_agent_prompt_carries_the_review_steps_and_the_skill_verbatim() -> None:
    text = render.render()
    command = render.COMMAND.read_text(encoding="utf-8")
    steps = command[command.index("## Steps") + len("## Steps") :].strip()
    assert steps in text  # the steps word for word, headings aside
    intro = command.split("\n", 1)[1].split("## Tool Use", 1)[0].strip()
    assert intro in text
    assert render.SKILL.read_text(encoding="utf-8").strip() in text
    assert text.index("### Steps") < text.index("<!-- skill start -->")
    # The command's own sections sit under its heading, not beside the skill's.
    assert "\n## Who runs it" not in text and "\n### Who runs it" in text


def test_the_agent_is_named_inherits_the_model_and_keeps_the_rules() -> None:
    head = render.render().split("---", 2)[1]
    assert "name: dynamodb-part-review" in head
    assert "model: inherit" in head  # a cache lever, not a model one
    body = render.render()
    assert "Do not dispatch subagents yourself." in body
    assert "Unattended: do not ask the user anything." in body
    # Nothing review-specific may be in the shared prompt.
    for placeholder in ("{GROUP}", "{REQUEST}", "{RESPONSE}", "{FINALIZE}"):
        assert placeholder not in body, placeholder


def test_the_agent_prompt_forbids_reloading_the_command_and_carries_the_tool_use_rule() -> None:
    """The task text starts "Follow /review-schema": the prompt says not to load it
    or Read the skill again, and backs up the tool-use rule."""
    from tests.unit.scripts.test_modernize_command import TOOL_USE_RULE

    head = " ".join(render.render().split("## /review-schema", 1)[0].split())
    assert (
        "Do not load `/review-schema` (or any other command or skill) with the Skill tool" in head
    )
    assert (
        "do not Read `.claude/commands/review-schema.md` or `src/skills/dynamodb-pe-review.md`"
        in head
    )
    assert "## Tool Use " + TOOL_USE_RULE in head


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


def test_lint_and_the_pre_commit_hook_check_it() -> None:
    lint = (render.REPO_ROOT / "ci" / "lint.sh").read_text(encoding="utf-8")
    assert "uv run python scripts/render_part_review_agent.py --check" in lint
    hooks = (render.REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    assert "entry: uv run python scripts/render_part_review_agent.py --check" in hooks
    for source in ("review-schema\\.md", "dynamodb-pe-review\\.md", "dynamodb-part-review\\.md"):
        assert source in hooks, source
