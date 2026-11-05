# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`/move` walks a customer through step 1 (DMS into Aurora) after `/modernize` (#1886).

A tripwire on the command text: the shared tool-use rule, only real scripts and subcommands
whose examples parse, one result line, approval before every AWS write (and none under
``--auto`` without ``--inputs`` and ``--approve-deploy``), and the disclaimer and the cutover
message quoted from the scripts' output, never written into the command. A real customer run
is the proof; ``tests/unit/scripts/test_run_dms_step.py`` covers the helper itself.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from scripts import run_dms_step, validate_skills
from src.shared.cutover_message import CUTOVER_BODY, CUTOVER_HEADLINE
from src.tools.data_movement.dms_step import CUSTOMER_RESPONSIBILITY
from tests.unit.scripts.test_modernize_command import (
    COMMANDS_WITH_TOOL_USE_RULE,
    SEARCH_AS_IS,
    TOOL_USE_RULE,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
MOVE = REPO_ROOT / ".claude" / "commands" / "move.md"
STEP_SCRIPTS = ("run_transformation_contract.py", "run_data_movement_plan.py", "render_dms_step.py")


def _text() -> str:
    return MOVE.read_text()


def _flat(text: str | None = None) -> str:
    return " ".join((_text() if text is None else text).split())


def _section(heading: str) -> str:
    return _text().split(f"## {heading}", 1)[1].split("\n## ", 1)[0]


def _bash_commands(text: str | None = None) -> list[str]:
    blocks = re.findall(r"```bash\n(.*?)```", _text() if text is None else text, re.DOTALL)
    return [line.strip() for block in blocks for line in block.splitlines() if line.strip()]


def _helper_argv(command: str) -> list[str]:
    return shlex.split(command.replace("{", "").replace("}", ""))[4:]


def test_move_carries_the_tool_use_rule_and_is_guarded_with_the_others() -> None:
    assert "move.md" in COMMANDS_WITH_TOOL_USE_RULE
    assert TOOL_USE_RULE + SEARCH_AS_IS in _text()
    assert "Never run `aws` yourself" in _flat(_section("Tool Use"))


def test_every_script_the_command_names_exists_and_validates() -> None:
    assert validate_skills.validate(REPO_ROOT) == []
    commands = _bash_commands()
    scripts = {shlex.split(c)[3] for c in commands}
    assert scripts == {*(f"scripts/{s}" for s in STEP_SCRIPTS), "scripts/run_dms_step.py"}
    for script in scripts:
        assert (REPO_ROOT / script).is_file(), script


def test_prepare_runs_contract_then_plan_then_render_in_that_order() -> None:
    prepare = [shlex.split(c)[3] for c in _bash_commands(_section("Stage 1: Prepare"))]
    assert prepare == [*(f"scripts/{s}" for s in STEP_SCRIPTS), "scripts/run_dms_step.py"]


def test_every_helper_example_parses_with_the_helpers_own_parser() -> None:
    used = set()
    for command in _bash_commands():
        if "run_dms_step.py" not in command:
            continue
        args = run_dms_step._parser().parse_args(_helper_argv(command))
        used.add(args.command)
        assert "--artifact-root" not in command and "--inputs" not in command, command
    assert used == set(run_dms_step.LOCAL) | set(run_dms_step.READS) | set(run_dms_step.WRITES)


def test_every_aws_write_passes_approve_and_only_in_the_move_stage() -> None:
    move = _section("Stage 3: Move")
    for command in _bash_commands():
        if "run_dms_step.py" not in command:
            continue
        args = run_dms_step._parser().parse_args(_helper_argv(command))
        if args.command in run_dms_step.WRITES:
            assert args.approve, command
            assert command in move, f"an AWS write outside Move: {command}"
        else:
            assert not args.approve, command


def test_move_asks_before_the_change_set_its_execution_and_the_task_start() -> None:
    move = _flat(_section("Stage 3: Move"))
    for question in ("Create the change set for this stack?", "Execute this change set?",
                     "Start the replication task now?"):  # fmt: skip
        assert f'Ask: "{question}" Only on a yes' in move, question
    assert "never an earlier or a general one" in move


def test_auto_never_writes_without_inputs_and_approve_deploy() -> None:
    headless = _flat(_section("Headless (`--auto`)"))
    assert (
        "Without `--inputs`: no AWS call at all. End with "
        "`MOVE_RESULT: stopped stage=check reason=needs customer inputs`" in headless
    )
    assert (
        "ends before any AWS write with `MOVE_RESULT: stopped stage=move reason=deploy needs --approve-deploy`"
        in headless
    )
    assert "refuse to run under `MODERNIZER_CI_SANDBOX=1`" in headless
    prepare = _flat(_section("Stage 1: Prepare"))
    assert (
        "without `--inputs`, end with `MOVE_RESULT: stopped stage=check reason=needs customer inputs`"
        in prepare
    )
    move = _flat(_section("Stage 3: Move"))
    assert (
        "Under `--auto`, `--approve-deploy` stands for every approval of this stage; without it"
        in move
    )
    arguments = _flat(_section("Arguments"))
    assert "`--approve-deploy` — only with `--auto` and `--inputs`" in arguments


def test_every_stage_that_asks_says_what_auto_does_instead() -> None:
    for heading in ("Step 0: The job and the step", "Stage 1: Prepare", "Stage 2: Check",
                    "Stage 3: Move"):  # fmt: skip
        section = _section(heading)
        if re.search(r"\b[Aa]sk\b", section):
            assert "Under `--auto`" in section, heading


def test_the_result_line_has_exactly_the_two_documented_forms() -> None:
    text = _text()
    assert "`MOVE_RESULT: complete job_id=<id> step=1 gate=<status>`" in text
    assert "`MOVE_RESULT: stopped stage=<stage> reason=<one line>`" in text
    assert "End every run with exactly one line" in text
    stages = set(re.findall(r"MOVE_RESULT: stopped stage=(\w+)", text)) - {"<stage>"}
    assert stages <= {"select", "prepare", "check", "move", "prove"}
    assert stages == {"select", "prepare", "check", "move", "prove"}


def test_the_disclaimer_and_the_cutover_message_are_quoted_from_the_scripts_never_written_here() -> (
    None
):
    flat = _flat()
    for text in (CUSTOMER_RESPONSIBILITY, CUTOVER_BODY, CUTOVER_HEADLINE):
        for sentence in re.split(r"(?<=[.:;])\s+", text):
            words = sentence.split()
            for i in range(len(words) - 5):
                assert " ".join(words[i : i + 6]) not in flat, sentence
    assert "quote the line's `disclaimer` field verbatim" in flat
    assert "quote the line's `cutover_message` field verbatim" in flat
    assert "never recommend a time to cut over" in flat


def test_the_command_never_handles_a_secret_value() -> None:
    flat = _flat(_section("Credentials and secrets"))
    assert "Never ask for, write, print or repeat a password or a secret's value" in flat
    assert "`read_profile` for the read-only calls" in flat
    assert "Recommend a read-only profile" in flat
    example = re.search(r"```json\n(.*?)```", _text(), re.DOTALL)
    assert example is not None
    assert "password" not in example.group(1).lower()
    keys = set(re.findall(r'"(\w+)":', example.group(1)))
    assert keys <= set(run_dms_step.MoveInputs.model_fields)


def test_auto_uses_the_inputs_files_confirmed_checks_and_never_fills_them_in() -> None:
    check = _flat(_section("Stage 2: Check"))
    assert "it uses the file's `confirmed_checks`, and never adds to them" in check
    assert "with no `--confirm` flag" in check
    assert "never fill it in yourself" in check
    example = re.search(r"```json\n(.*?)```", _text(), re.DOTALL)
    assert example is not None and '"confirmed_checks"' in example.group(1)
    assert "confirmed_checks" in run_dms_step.MoveInputs.model_fields


def test_the_inputs_blocks_name_only_fields_the_helper_accepts() -> None:
    blocks = _section("Stage 2: Check").split("**Preflight.**", 1)[0].split("Write them", 1)[0]
    kinds = {"rds_instance", "aurora_cluster", "self_managed"}
    named = set(re.findall(r"`([a-z]+(?:_[a-z]+)+)`", blocks)) - kinds
    fields = set(run_dms_step.MoveInputs.model_fields)
    assert len(named) > 15 and named <= fields, named - fields


def test_preflight_walks_failures_and_confirmations_and_never_confirms_for_the_customer() -> None:
    check = _flat(_section("Stage 2: Check"))
    for status in ('"status": "pass"', '"status": "fail"', '"status": "needs_confirmation"'):
        assert status in check
    assert "`first_failure`" in check and "`to_confirm`" in check
    assert "Never confirm a row for them" in check


def test_prove_polls_inside_the_script_and_never_ends_the_turn_while_loading() -> None:
    prove = _flat(_section("Stage 4: Prove"))
    assert "--until caught_up --wait-seconds 540" in prove
    assert "Never end the turn while it is still loading" in prove
    waiting = _flat(_section("Waiting Rule"))
    assert "never a turn that ends with nothing running" in waiting
    for command in _bash_commands():
        args = (
            run_dms_step._parser().parse_args(_helper_argv(command))
            if "run_dms_step.py" in command
            else None
        )
        if args is not None and args.wait_seconds:
            assert args.wait_seconds <= run_dms_step.MAX_WAIT_SECONDS


def test_resume_maps_every_status_phase_to_a_stage() -> None:
    resume = _flat(_section("Step 0: The job and the step"))
    phases = {"no_stack", "stack_in_progress", "stack_failed", "connections_not_tested",
              "connections_testing", "connections_failed", "ready_to_start", "loading",
              "validating", "caught_up", "task_stopped"}  # fmt: skip
    source = (REPO_ROOT / "scripts" / "run_dms_step.py").read_text()
    for phase in phases:
        assert f'"{phase}"' in source, phase
        assert f"`{phase}`" in resume, phase


def test_the_command_dispatches_no_subagents() -> None:
    assert "Do not dispatch subagents" in _text()


def test_settings_ci_allows_every_script_the_command_runs() -> None:
    import json

    allow = json.loads((REPO_ROOT / ".claude" / "settings.ci.json").read_text())["permissions"][
        "allow"
    ]
    for script in (*STEP_SCRIPTS, "run_dms_step.py"):
        assert f"Bash(uv run python scripts/{script} *)" in allow, script
