"""Guard rails for /modernize --auto: a headless run must never wait on a human.

This is a tripwire, not a simulation of a real run — the real proof is an
actual headless `claude -p` run against these commands.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMANDS_DIR = REPO_ROOT / ".claude" / "commands"

# Sub-commands /modernize dispatches as subagents during a pipeline run.
DISPATCHED_SUBCOMMANDS = [
    "reality-check.md",
    "design-schema-dynamodb.md",
    "design-schema-elasticache.md",
    "design-schema-opensearch.md",
    "design-schema-documentdb.md",
    "design-schema-aurora-mysql.md",
    "design-schema-aurora-postgresql.md",
    "synthesize.md",
]

# A line matches a "question" tripwire if it asks the user something or waits
# for them, UNLESS it is explicitly scoped to a non-headless/non---auto path.
QUESTION_PATTERN = re.compile(r"\bAsk\b.*\?")
WAIT_PATTERN = re.compile(r"Wait for the user")
AUTO_EXEMPTION_PATTERN = re.compile(r"--auto|headless|[Uu]nless `--auto`")


def _modernize_text() -> str:
    return (COMMANDS_DIR / "modernize.md").read_text()


def test_documents_mode_argument() -> None:
    text = _modernize_text()
    assert "--mode" in text
    assert "chat" in text and "ui" in text and "both" in text


def test_step_0_contains_skip_rule() -> None:
    text = _modernize_text()
    step_0 = text.split("## Step 0", 1)[1].split("## CRITICAL", 1)[0]
    assert "skip this question" in step_0.lower()


def test_modernize_result_line_documents_both_outcomes() -> None:
    text = _modernize_text()
    assert "MODERNIZE_RESULT: complete" in text
    assert "MODERNIZE_RESULT: failed" in text


def test_dispatched_subcommands_have_no_unexempted_user_prompts() -> None:
    for filename in DISPATCHED_SUBCOMMANDS:
        path = COMMANDS_DIR / filename
        assert path.exists(), f"missing dispatched sub-command: {filename}"
        text = path.read_text()

        for line in text.splitlines():
            if QUESTION_PATTERN.search(line) or WAIT_PATTERN.search(line):
                assert AUTO_EXEMPTION_PATTERN.search(line), (
                    f"{filename}: line asks/waits on a user without an "
                    f"--auto/headless exemption: {line!r}"
                )
