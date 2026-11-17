# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``/modernize`` runs the routine repair loop (#2385), one fresh subagent per round.

After the post-schema reroute (the last step that writes a routine-migration version)
and before synthesis, the orchestrator writes the repair request, dispatches one
subagent per round while the line says ``awaiting_llm``, at most ``max_rounds`` times,
and runs each round's ``--finalize`` itself. The proof follows this machine
(``--proof auto``). It asks nothing, deploys nothing and never ends the run.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.unit.scripts.test_modernize_command import (
    DISPATCHED_SUBCOMMANDS,
    NO_NESTING_RULE,
    SEARCH_AS_IS,
    TOOL_USE_RULE,
)

REPO = Path(__file__).resolve().parents[3]
COMMANDS = REPO / ".claude" / "commands"
MODERNIZE = COMMANDS / "modernize.md"
SETTINGS = REPO / ".claude" / "settings.ci.json"
CI_README = REPO / "ci" / "README.md"
REQUEST = (
    "uv run python scripts/run_routine_repair.py --job-id {job_id} --db {database_name} "
    "--llm-mode external --proof auto"
)
FINALIZE = (
    "uv run python scripts/run_routine_repair.py --job-id {job_id} --db {database_name} "
    "--finalize --proof {proof}"
)


def _text() -> str:
    return MODERNIZE.read_text()


def _section() -> str:
    text = _text()
    assert "### Phase 6f: Routine repair" in text, "no routine repair phase in /modernize"
    return text.split("### Phase 6f: Routine repair", 1)[1].split("\n### ", 1)[0]


def _templates(section: str) -> list[str]:
    blocks = re.findall(r"```text\n(.*?)```", section, flags=re.DOTALL)
    return [" ".join(b.split()) for b in blocks]


def test_the_phase_runs_after_the_reroute_and_before_synthesis() -> None:
    text = _text()
    reroute = text.index("**6e. Post-schema reroute**")
    repair = text.index("### Phase 6f: Routine repair")
    synthesis = text.index("### Phase 7: Synthesis")
    assert reroute < repair < synthesis


def test_the_orchestrator_writes_the_request_and_runs_each_finalize() -> None:
    section = _section()
    assert REQUEST in section
    assert FINALIZE in section
    # The subagent writes the answer only: the loop's control stays with script stdout.
    (template,) = _templates(section)
    assert "Do not run `scripts/run_routine_repair.py`" in template
    assert "Write only {RESPONSE}" in template


def test_one_fresh_subagent_per_round_at_most_max_rounds() -> None:
    flat = " ".join(_section().split())
    assert "one fresh subagent per round" in flat
    assert "at most `max_rounds`" in flat
    assert '"status": "awaiting_llm"' in flat
    assert "never reuse" in flat  # a round's subagent is never resumed or reused


def test_the_round_task_text_carries_the_headless_rules() -> None:
    (template,) = _templates(_section())
    assert template.startswith("Follow /repair-routines")
    assert TOOL_USE_RULE + SEARCH_AS_IS in template
    assert NO_NESTING_RULE in template
    assert "Unattended: do not ask the user anything." in template
    assert "Do not update .modernizer-state.json" in template


def test_the_proof_follows_the_machine_and_finalize_reuses_it() -> None:
    flat = " ".join(_section().split())
    assert "`--proof auto`" in flat
    assert "`repaired_pending_proof`" in flat
    assert "the line's `proof`" in flat


def test_the_phase_asks_nothing_and_deploys_nothing() -> None:
    section = _section()
    flat = " ".join(section.split())
    assert "?" not in re.sub(r"```.*?```", "", section, flags=re.DOTALL)
    assert not re.search(r"\bAsk\b", section)
    assert "never asks the user anything, with or without `--auto`" in flat
    assert "deploys no routine" in flat
    for step_1 in ("run_dms_step.py", "proof-target", "/move", "/test-routines"):
        assert step_1 not in section, step_1


def test_a_repair_failure_never_ends_the_run() -> None:
    flat = " ".join(_section().split())
    assert '`phase_status.routine_repair` = "failed"' in flat
    assert "never ends the run" in flat
    assert "MODERNIZE_RESULT" not in flat
    assert "one retry" in flat


def test_under_auto_the_round_follows_the_waiting_rule() -> None:
    flat = " ".join(_section().split())
    assert "--mark-dispatched" in flat
    assert "--wait-for-report 110" in flat
    assert "`nothing_pending`" in flat


@pytest.mark.passes_on_main  # a guard: the phase must not change the result line
def test_the_result_line_is_unchanged() -> None:
    text = _text()
    assert text.count("End the run with exactly one line `MODERNIZE_RESULT: complete") == 1
    assert "`MODERNIZE_RESULT: complete job_id=<id> db=<db> mode=<mode>`" in text


def test_the_repair_command_is_a_dispatched_subcommand_with_a_skill_allow() -> None:
    assert "repair-routines.md" in DISPATCHED_SUBCOMMANDS
    allow = json.loads(SETTINGS.read_text())["permissions"]["allow"]
    assert "Skill(repair-routines)" in allow
    assert "Bash(uv run python scripts/run_routine_repair.py *)" in allow


def test_ci_readme_gives_the_rationale_for_the_new_calls() -> None:
    flat = " ".join(CI_README.read_text().split())
    assert "`Skill(repair-routines)` (#2385)" in flat
    assert "`/modernize` Phase 6f" in flat
    assert "`--proof auto`" in flat


def test_the_round_paths_are_the_lines_paths_under_artifacts() -> None:
    # #2385 review: the subagent writes {RESPONSE} as printed; the script prints it under
    # the artifact root (tests/unit/scripts/test_run_routine_repair.py pins the path and the
    # allow rule), so the command never adds or drops a prefix.
    flat = " ".join(_section().split())
    assert "the two paths exactly as printed, under `artifacts/`" in flat
    assert "./artifacts/{" not in _section()
    repair = (COMMANDS / "repair-routines.md").read_text()
    assert "./artifacts/{llm_" not in repair
    assert "Read `{llm_request}`" in repair and "to `{llm_response}`" in repair


def test_the_waiting_command_names_its_own_script() -> None:
    flat = " ".join(_section().split())
    assert (
        "uv run python scripts/run_schema_design.py --job-id {job_id} --db {database_name} "
        "--mark-dispatched <agentId> --wait-for-report 110" in flat
    )
    assert "never `run_routine_repair.py`" in flat


def test_the_waiting_rule_and_error_handling_carry_the_6f_exception() -> None:
    text = _text()
    waiting = " ".join(text.split("## Waiting Rule", 1)[1].split("\n## ", 1)[0].split())
    assert "6f after a round: that round's `run_routine_repair.py --finalize`" in waiting
    assert "A hung 6f round subagent (`Routine repair, round ...`) is the one exception" in waiting
    errors = " ".join(text.split("## Error Handling", 1)[1].split())
    assert "**Routine repair (Phase 6f)** is the exception to the steps above" in errors
    assert "never ends the run and asks nothing, with or without `--auto`" in errors
    flat = " ".join(_section().split())
    assert "A round subagent that is hung" in flat and "TaskStop" in flat


def test_an_interactive_run_is_told_the_proof_starts_local_engines() -> None:
    flat = " ".join(_section().split())
    assert "Without `--auto`, when `{proof}` is `local`" in flat
    assert "starts two disposable local database engines with no network listener" in flat
    assert "it is a notice, not a question" in flat
