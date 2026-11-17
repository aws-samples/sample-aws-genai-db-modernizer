# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""`/test-routines` tests the carried routines on a throwaway Aurora test cluster (#2411).

A tripwire on the command text, as ``test_move_command.py`` is for ``/move``: the shared
tool-use rule, only real scripts and subcommands whose examples parse, each AWS write behind
``--approve`` and asked for on its own with the scripts' own questions, ``--auto`` refused
before any command, and one result line. ``test_run_dms_step_proof_target.py`` and
``test_run_dms_step_proof_run.py`` cover the helper itself.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

from scripts import run_dms_step, validate_skills
from tests.unit.scripts.test_modernize_command import (
    COMMANDS_WITH_TOOL_USE_RULE,
    SEARCH_AS_IS,
    TOOL_USE_RULE,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
COMMAND = REPO_ROOT / ".claude" / "commands" / "test-routines.md"
PREPARE = ("run_transformation_contract.py", "run_data_movement_plan.py", "render_dms_step.py")


def _text() -> str:
    return COMMAND.read_text()


def _flat(text: str | None = None) -> str:
    return " ".join((_text() if text is None else text).split())


def _section(heading: str) -> str:
    return _text().split(f"## {heading}", 1)[1].split("\n## ", 1)[0]


def _bash_commands(text: str | None = None) -> list[str]:
    blocks = re.findall(r"```bash\n(.*?)```", _text() if text is None else text, re.DOTALL)
    return [line.strip() for block in blocks for line in block.splitlines() if line.strip()]


def _helper_argv(command: str) -> list[str]:
    return shlex.split(command.replace("{", "").replace("}", ""))[4:]


def test_it_carries_the_tool_use_rule_and_names_only_real_scripts() -> None:
    assert "test-routines.md" in COMMANDS_WITH_TOOL_USE_RULE
    assert TOOL_USE_RULE + SEARCH_AS_IS in _text()
    assert "Never run `aws` yourself" in _flat(_section("Tool Use"))
    assert validate_skills.validate(REPO_ROOT) == []
    scripts = [shlex.split(c)[3] for c in _bash_commands()]
    assert scripts[:3] == [f"scripts/{s}" for s in PREPARE]
    assert set(scripts) == {*(f"scripts/{s}" for s in PREPARE), "scripts/run_dms_step.py"}


def test_it_runs_every_test_cluster_command_and_each_write_only_with_approve() -> None:
    used = set()
    for command in _bash_commands():
        if "run_dms_step.py" not in command:
            continue
        args = run_dms_step._parser().parse_args(_helper_argv(command))
        used.add(args.command)
        assert not args.auto, command
        assert args.approve == (args.command in run_dms_step.PROOF_WRITES), command
    assert used == set(run_dms_step.PROOF_TARGET)


def test_each_write_is_asked_on_its_own_with_the_scripts_question_verbatim() -> None:
    """The cost is shown before the deploy question; deploy, the test and the teardown each
    have their own question, quoted from the scripts, never written here."""
    flat = _flat()
    cost = _flat(_section("Step 4: The cost"))
    assert "`cost`" in cost and "ask its `question` verbatim" in cost
    assert cost.index("`cost`") < cost.index("ask its `question` verbatim")
    assert "Only on a yes to the plan's `question`" in _flat(_section("Step 5: Deploy"))
    test = _flat(_section("Step 6: The test"))
    assert test.index("Ask the plan's `run_question` verbatim. Only on a yes") < test.index(
        "proof-target-run"
    )
    teardown = _flat(_section("Step 7: Teardown"))
    assert teardown.index("Ask the `teardown_question` verbatim") < teardown.index("Only on a yes")
    assert "Deploy the routine-proof" not in flat and "Run the routine proof on" not in flat


def test_auto_is_refused_before_any_command() -> None:
    args = _flat(_section("Arguments"))
    assert "`--auto`: refused" in args and "Run no command at all" in args
    assert (
        "`TEST_ROUTINES_RESULT: stopped stage=select reason=--auto: the test cluster runs only in "
        "a run you attend; the routines stay not tested`" in args
    )


def test_a_result_means_no_difference_on_the_calls_made_and_nothing_more() -> None:
    flat = _flat()
    assert "showed no difference on the calls made, and nothing more" in flat
    assert "An outcome of `proved` means the test found no difference on the calls made" in flat
    assert "never say it works, is correct or behaves like the original on every input" in flat


def test_the_scratch_schema_test_and_the_repair_hook_are_named() -> None:
    test = _flat(_section("Step 6: The test"))
    assert "this is the scratch-schema test (#1945)" in test
    assert "never runs on the customer's migration Aurora" in test
    assert "only when it ran on the routines file as it is now" in test
    assert "(`--proof test-cluster`) are not part of this command yet (#2384)" in test


def test_every_no_ends_with_a_stopped_line_and_the_routines_held_back() -> None:
    flat = _flat()
    for line in (
        "TEST_ROUTINES_RESULT: stopped stage=plan reason=customer declined the test cluster; "
        "every carried routine stays held back as not tested",
        "TEST_ROUTINES_RESULT: stopped stage=run reason=customer declined the test; every "
        "carried routine stays held back as not tested",
    ):
        assert f"`{line}`" in flat, line
    assert "bills until it is torn down" in _flat(_section("Step 7: Teardown"))


def test_the_result_line_has_exactly_the_two_documented_forms() -> None:
    result = _flat(_section("Result line"))
    assert "`TEST_ROUTINES_RESULT: complete job_id=<id> proved=<n> held=<n>`" in result
    assert "`TEST_ROUTINES_RESULT: stopped stage=<stage> reason=<one line>`" in result
    stages = set(re.findall(r"stage=([a-z]+)", _text()))
    assert {"select", "prepare", "plan", "deploy", "run"} <= stages
    assert stages <= {"select", "prepare", "plan", "deploy", "run", "teardown"}
    assert "`select`, `prepare`, `plan`, `deploy`, `run` or `teardown`" in result
    assert "exactly one line" in result


def test_it_tests_on_both_aurora_targets() -> None:
    """#2298: the MySQL test runs as a user created from a hash, so both targets are tested."""
    flat = _flat()
    assert "on both targets, Aurora PostgreSQL and Aurora MySQL" in flat
    assert "not available yet" not in flat


def test_it_dispatches_no_subagents() -> None:
    assert "Do not dispatch subagents" in _flat(_section("Waiting Rule"))


def test_the_docs_list_the_command() -> None:
    docs = (REPO_ROOT / "docs" / "use-claude-code.md").read_text()
    assert "| `/test-routines` |" in docs


def test_a_rerun_finds_the_deployed_cluster_and_never_asks_to_deploy_it_again() -> None:
    """Review of #2420: the teardown has its own path, and a cluster already deployed is
    offered the test and the teardown, never a second deploy."""
    where = _flat(_section("Step 3: Where the test cluster stands"))
    assert "proof-target-status --job-id {job_id} --db {db}" in where
    assert where.index('`"status": "deployed"`') < where.index("Never ask to deploy it again")
    assert "go to Step 6 without asking its `question`" in where
    assert "When the customer only asked to tear it down, go to Step 7" in where
    assert "show its `stack_status` and `stack_reason`, then go to Step 7" in where
    flat = _flat()
    assert flat.index("## Step 3: Where") < flat.index("## Step 4: The cost")


def test_a_deploy_that_runs_out_of_time_says_it_bills_and_how_to_tear_it_down() -> None:
    deploy = _flat(_section("Step 5: Deploy"))
    assert "bills until it is torn down" in deploy
    assert "offers the test and the teardown, or the teardown alone" in deploy
    assert (
        "`TEST_ROUTINES_RESULT: stopped stage=deploy reason=test cluster still deploying; it "
        "bills until torn down: run /test-routines again to test or tear it down`" in deploy
    )


def test_a_rerun_on_a_stack_still_being_created_waits_then_offers_the_test() -> None:
    """Round 2 review of #2420: after a deploy time-out the stack reads ``changing``; a rerun
    waits for it read-only, as the deploy step does, then acts on the last line, so it offers
    the test and the teardown as the time-out message says."""
    where = _section("Step 3: Where the test cluster stands")
    flat = _flat(where)
    changing = flat.index('`"status": "changing"`')
    assert (
        "wait for it, read-only, in the same turn, up to 4 times, then act on the last line" in flat
    )
    waits = [c for c in _bash_commands(where) if c.endswith("--wait-seconds 540")]
    assert waits and "proof-target-status --job-id {job_id} --db {db}" in waits[0]
    assert changing < flat.index("Any other status (the stack failed)")
    assert "waits while it is still being created, then offers the test and the teardown" in _flat(
        _section("Step 5: Deploy")
    )
