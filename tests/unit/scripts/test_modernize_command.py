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
CONFIRM_PATTERN = re.compile(r"\bconfirm", re.IGNORECASE)
AUTO_EXEMPTION_PATTERN = re.compile(r"--auto|headless|[Uu]nless `--auto`")


def _modernize_text() -> str:
    return (COMMANDS_DIR / "modernize.md").read_text()


def _paragraphs(text: str) -> list[str]:
    """Split markdown into blank-line-delimited paragraphs -- the unit an
    --auto exemption must appear within. A trigger word and its exemption
    can be on different lines of the same paragraph (e.g. a bullet list with
    no blank lines between items), but not in two paragraphs separated by a
    blank line -- a reader (human or headless agent) skimming one paragraph
    at a time should never see a question/wait/confirm with no escape hatch
    in view."""
    return [p for p in re.split(r"\n\s*\n", text) if p.strip()]


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


def test_modernize_md_paragraphs_with_user_prompts_are_auto_exempt() -> None:
    text = _modernize_text()
    for paragraph in _paragraphs(text):
        triggered = (
            QUESTION_PATTERN.search(paragraph)
            or WAIT_PATTERN.search(paragraph)
            or CONFIRM_PATTERN.search(paragraph)
        )
        if triggered:
            assert AUTO_EXEMPTION_PATTERN.search(paragraph), (
                "modernize.md: a paragraph asks/waits/confirms with the user "
                f"without an --auto exemption in that same paragraph:\n{paragraph!r}"
            )


def test_setup_failure_also_ends_with_modernize_result_failed() -> None:
    # The local-UI setup step (scripts/start_local_ui.py "error") must end a
    # headless run the same way any other phase failure does.
    step_0 = _modernize_text().split("## Step 0", 1)[1].split("## CRITICAL", 1)[0]
    assert "MODERNIZE_RESULT: failed phase=setup" in step_0


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


def test_reality_check_finalize_is_owned_by_modernize_only() -> None:
    # /modernize dispatches /reality-check as a subagent and then runs
    # --resume-reality-check itself (the orchestration step owns finalize and
    # the state update). The subagent only writes the response; finalizing in
    # both places is redundant work (finalize is a no-op the second time) and
    # an extra Bash call the subagent's context doesn't need.
    # Run on its own (not from /modernize), /reality-check must still finalize,
    # so it may mention --resume-reality-check, but only inside the paragraph
    # that scopes it to standalone use.
    assert "--resume-reality-check" in _modernize_text()
    reality_check = (COMMANDS_DIR / "reality-check.md").read_text()
    assert "llm_responses/reality_check.json" in reality_check
    blocks = reality_check.split("\n\n")
    for i, block in enumerate(blocks):
        if "--resume-reality-check" in block:
            context = "\n\n".join(blocks[max(0, i - 1) : i + 1])
            assert "on its own" in context and "not from `/modernize`" in context, (
                "reality-check.md may only finalize when invoked on its own: " + block
            )
