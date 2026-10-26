"""The PostToolUse reminder to paste print_summary.py's block into the reply (#1605)."""

from __future__ import annotations

import json

import pytest

from scripts.hooks import remind_quote_printed_summary as hook

GATE = "uv run python scripts/print_summary.py --job-id j1 --db wordpress --gate --mode both"
FINAL = "uv run python scripts/print_summary.py --job-id j1 --db wordpress --final --mode chat"
GATE_BLOCK = "## Assignment for approval: wordpress (job j1)\n\n| Engine | Queries before |\n"
FINAL_BLOCK = "## Assessment: wordpress (job j1)\n\n### At a glance\n"


def _event(command: str, stdout: str, **extra: object) -> dict:
    return {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {"stdout": stdout, "stderr": "", "interrupted": False},
        "tool_use_id": "tu-1",
        **extra,
    }


def _context(result) -> str | None:
    assert result.code == 0 and result.stderr == ""
    if not result.stdout.strip():
        return None
    out = json.loads(result.stdout)
    assert out["hookSpecificOutput"]["hookEventName"] == "PostToolUse"
    return str(out["hookSpecificOutput"]["additionalContext"])


def test_a_gate_block_gets_the_paste_it_verbatim_reminder(run_hook) -> None:
    context = _context(run_hook("remind_quote_printed_summary.py", _event(GATE, GATE_BLOCK)))
    assert context is not None
    assert "next chat message must contain this stdout verbatim" in context
    assert "approval question" in context and '"the block above"' in context


def test_a_final_summary_gets_its_reminder(run_hook) -> None:
    context = _context(run_hook("remind_quote_printed_summary.py", _event(FINAL, FINAL_BLOCK)))
    assert context is not None
    assert "Paste this stdout verbatim" in context and "MODERNIZE_RESULT" in context


@pytest.mark.parametrize(
    "event",
    [
        _event(GATE, '{"status": "error", "message": "no reality-check output"}'),
        _event(GATE, ""),
        _event(GATE, GATE_BLOCK, agent_id="a1"),
        _event(f"{GATE} | head -5", GATE_BLOCK),
        _event("uv run python scripts/print_summary.py --help --gate", GATE_BLOCK),
        _event("uv run python scripts/run_assessment.py --job-id j1 --gate", GATE_BLOCK),
        _event(f"echo '{GATE}'", GATE_BLOCK),
        {**_event(GATE, GATE_BLOCK), "tool_name": "Read"},
    ],
    ids=[
        "error-line",
        "empty",
        "subagent",
        "pipe",
        "help",
        "other-script",
        "quoted-text",
        "other-tool",
    ],
)
def test_no_reminder_when_there_is_no_block_to_show(run_hook, event: dict) -> None:
    assert _context(run_hook("remind_quote_printed_summary.py", event)) is None


@pytest.mark.parametrize("payload", ["", "not json", "[]", '{"tool_input": 3}'])
def test_a_bad_event_prints_nothing_and_never_blocks(run_hook, payload: str) -> None:
    assert _context(run_hook("remind_quote_printed_summary.py", payload)) is None


def test_the_reminder_names_the_rule_modernize_states() -> None:
    from tests.unit.hooks.conftest import REPO_ROOT

    text = (REPO_ROOT / ".claude" / "commands" / "modernize.md").read_text()
    assert "scripts/hooks/remind_quote_printed_summary.py" in text
    assert hook.printed_kind(GATE) == "gate" and hook.printed_kind(FINAL) == "final"
