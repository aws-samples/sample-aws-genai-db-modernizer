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
from src.shared.cutover_message import CUTOVER_MESSAGE
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
    move, ending = _section("Stage 3: Move"), _section("Ending the step")
    prove = _section("Stage 4: Prove")
    for command in _bash_commands():
        if "run_dms_step.py" not in command:
            continue
        args = run_dms_step._parser().parse_args(_helper_argv(command))
        if args.command in run_dms_step.ENDING:
            assert args.approve, command
            assert command in ending, f"stop or teardown outside Ending the step: {command}"
        elif args.command in (
            run_dms_step.RESTART,
            *run_dms_step.RECOVER,
            *run_dms_step.TABLE_RECOVER,
        ):
            assert args.approve, command
            assert command in prove, f"{args.command} outside Prove: {command}"
        elif args.command in run_dms_step.WRITES:
            assert args.approve, command
            assert command in move, f"an AWS write outside Move: {command}"
        else:
            assert not args.approve, command


def test_ending_the_step_follows_prove_asks_before_each_write_and_never_runs_under_auto() -> None:
    text = _text()
    assert (
        text.index("## Stage 4: Prove")
        < text.index("## Ending the step")
        < text.index("## Headless (`--auto`)")
    )
    ending = _flat(_section("Ending the step"))
    for question in ("{dms_reports}. Stop the replication task of stack {stack_name} in {region} now?",
                     "{dms_reports}. Delete the stack {stack_name} in {region} now?",
                     "{dms_reports}. Delete the stack {stack_name} in {region} again?"):  # fmt: skip
        assert f'"{question}" Only on a yes' in ending, question
    order = [ending.index(x) for x in ("plan --job-id", "Stop the replication task",
                                       "Delete the stack {stack_name} in {region} now?")]  # fmt: skip
    assert order == sorted(order)  # the stack and region come from plan before either question
    assert "Under `--auto`, never run `stop` or `teardown`" in ending
    assert "the Aurora cluster and its data stay" in ending
    headless = _flat(_section("Headless (`--auto`)"))
    assert "never stops the task or deletes the stack" in headless


def test_a_run_prints_one_result_line_whether_it_proves_or_ends_the_step() -> None:
    prove = _flat(_section("Stage 4: Prove"))
    hint = prove.index("how the step ends: to end the step, ask `/move step-1`")
    assert hint < prove.index("End with `MOVE_RESULT: complete")  # the hint comes before the line
    ending = _flat(_section("Ending the step"))
    assert "Ending the step is a run of its own" in ending
    assert "never inside a run that is still proving" in ending
    assert (
        "exactly one result line, a `stage=end` one, and never prints `MOVE_RESULT: complete`"
        in ending
    )
    assert set(re.findall(r"MOVE_RESULT: (\w+)", ending)) == {"stopped", "complete"}
    assert set(re.findall(r"MOVE_RESULT: stopped stage=(\w+)", ending)) == {"end"}
    result = _flat(_section("Result line"))
    assert "prints only its `stage=end` line, never a `complete` one" in result


def test_a_teardown_that_is_still_deleting_is_waited_on_read_only_and_reapproved_after_a_failure() -> (
    None
):
    ending = _section("Ending the step")
    flat = _flat(ending)
    assert "never rerun `teardown`" in flat and "once per yes" in flat
    after = ending.split('`"status": "deleting"`', 1)[1]
    first = _bash_commands(after)[0]
    args = run_dms_step._parser().parse_args(_helper_argv(first))
    assert args.command == "status" and args.until == "stack" and not args.approve
    assert (
        after.index("DELETE_FAILED")
        < after.index("again?")
        < after.index("run `teardown` once more")
    )


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
                    "Stage 3: Move", "Stage 4: Prove", "Ending the step"):  # fmt: skip
        section = _section(heading)
        if re.search(r"\b[Aa]sk\b", section):
            assert "Under `--auto`" in section, heading


def test_the_result_line_has_exactly_the_two_documented_forms() -> None:
    text = _text()
    assert "`MOVE_RESULT: complete job_id=<id> step=1 gate=<status>`" in text
    assert "`MOVE_RESULT: stopped stage=<stage> reason=<one line>`" in text
    assert "End every run with exactly one line" in text
    stages = set(re.findall(r"MOVE_RESULT: stopped stage=(\w+)", text)) - {"<stage>"}
    assert stages <= {"select", "prepare", "check", "move", "prove", "end"}
    assert stages == {"select", "prepare", "check", "move", "prove", "end"}


def test_the_disclaimer_and_the_cutover_message_are_quoted_from_the_scripts_never_written_here() -> (
    None
):
    flat = _flat()
    for text in (CUSTOMER_RESPONSIBILITY,):
        for sentence in re.split(r"(?<=[.:;])\s+", text):
            words = sentence.split()
            for i in range(len(words) - 5):
                assert " ".join(words[i : i + 6]) not in flat, sentence
    assert "quote the line's `disclaimer` field verbatim" in flat
    assert "quote the line's `cutover_message` field verbatim" in flat
    assert "never give cutover advice" in flat
    # The one sentence the cutover gets is the script's, and the rule says to say only it.
    assert f'Wherever the cutover comes up, say only: "{CUTOVER_MESSAGE}"' in flat


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
    assert "--until replicating --wait-seconds 540" in prove
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
              "validating", "validation_stalled", "table_errors", "replicating", "task_stopped",
              "stack_create_failed", "stack_in_review"}  # fmt: skip
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


def test_ending_defines_a_result_line_for_every_no_and_for_waits_that_run_out() -> None:
    ending = _flat(_section("Ending the step"))
    for question, declined in (
        (
            "{dms_reports}. Stop the replication task of stack {stack_name} in {region} now?",
            "to stop the task",
        ),
        ("{dms_reports}. Delete the stack {stack_name} in {region} now?", "to delete the stack"),
        (
            "{dms_reports}. Delete the stack {stack_name} in {region} again?",
            "to delete the stack again",
        ),
    ):
        after = ending.split(f'"{question}"', 1)[1]
        no = after.index(
            "on a no, end with `MOVE_RESULT: stopped stage=end reason=customer declined"
        )
        assert no < after.index("```") if "```" in after else True, question
        assert f"reason=customer declined {declined}" in after, question
    for still in ("task still stopping", "stack still deleting"):
        assert (
            f"`MOVE_RESULT: stopped stage=end reason={still} ({{dms_reports}}): run /move step-1 again"
            in ending
        )
    assert ending.index("Still `DELETE_IN_PROGRESS` after the 4 waits") < ending.index(
        "reason=stack still deleting"
    )
    result = _flat(_section("Result line"))
    assert "A question the customer answers no ends the run with `MOVE_RESULT: stopped" in result


def test_a_stop_that_is_still_stopping_is_waited_on_read_only_and_a_restarted_task_is_asked_again() -> (
    None
):
    ending = _section("Ending the step")
    after = ending.split('`"status": "stopping"`', 1)[1]
    first = _bash_commands(after)[0]
    args = run_dms_step._parser().parse_args(_helper_argv(first))
    assert args.command == "status" and args.until == "task_stopped" and not args.approve
    flat = _flat(after.split("```", 2)[0] + after.split("```", 2)[2])
    assert "never rerun `stop`" in _flat(after.split("```", 1)[0])
    assert "the task started again" in flat and "ask the stop question again" in flat
    assert "needs a new yes" in flat


def test_the_delete_question_is_asked_only_once_the_task_is_shown_stopped() -> None:
    ending = _flat(_section("Ending the step"))
    proof = ending.index("Ask the delete question only once that is shown")
    assert proof < ending.index('"{dms_reports}. Delete the stack {stack_name} in {region} now?"')
    for evidence in ('`"stopped"`, `"no_task"` or `"no_stack"`',
                     "a `status --until task_stopped` line with `reached` true",
                     "Never on `stopping`, and never from a stack status alone"):  # fmt: skip
        assert evidence in ending[proof:], evidence
    assert "the wait reads the task whatever the stack's state" in ending


def test_teardowns_own_delete_failed_gets_the_same_question_as_the_status_wait() -> None:
    ending = _flat(_section("Ending the step"))
    routed = ending.index('`"status": "delete_failed"`: handle it as the `DELETE_FAILED` below')
    assert routed < ending.index(
        '"{dms_reports}. Delete the stack {stack_name} in {region} again?"'
    )
    statuses = {"deleted", "deleting", "delete_failed"}
    source = (REPO_ROOT / "scripts" / "run_dms_step.py").read_text()
    for status in statuses:
        assert f'`"status": "{status}"`' in ending and f'"{status}"' in source, status


def test_every_question_and_result_line_names_what_dms_reports() -> None:
    """The customer sees DMS's own task status (stopped, failed with its reason, never started),
    never a generic "not running" (maintainer feedback on #1907)."""
    ending = _flat(_section("Ending the step"))
    questions = re.findall(r'"([^"]*\?)" Only on a yes', ending)
    assert len(questions) == 3 and all(
        q.startswith("{dms_reports}. ") for q in questions
    ), questions
    lines = [m for m in re.findall(r"`MOVE_RESULT: stopped stage=end reason=([^`]*)`", ending)
             if m != "<its message>"]  # fmt: skip
    assert len(lines) == 6 and all("({dms_reports})" in m for m in lines), lines
    assert "Quote it verbatim in every question and result line below" in ending
    assert 'never shorten it to a generic "not running"' in ending
    assert (
        "When `task_status` is `failed`, also show the line's `last_failure` and `stop_reason`"
        in ending
    )
    for words in ("DMS reports the task stopped (not running)",
                  "DMS reports the task failed (not running): <DMS's reason>",
                  "DMS reports the task never started (ready, not running)"):  # fmt: skip
        assert f"`{words}`" in ending, words
    assert (
        run_dms_step.dms_reports({"Status": "stopped"})
        == "DMS reports the task stopped (not running)"
    )
    assert run_dms_step.dms_reports({"Status": "ready"}) == (
        "DMS reports the task never started (ready, not running)"
    )
    assert run_dms_step.dms_reports({"Status": "failed", "LastFailureMessage": "x"}) == (
        "DMS reports the task failed (not running): x"
    )
    assert "not running" not in _flat(_section("Ending the step")).replace(
        "(not running)", ""
    ).replace("ready, not running", "").replace('a generic "not running"', "").replace(
        "counts as not running", ""
    )


# --- --until, stalled validation, progress lines (#1995, #1996, #1997) -------------------------


def test_until_is_documented_with_its_stages_its_result_lines_and_unchanged_auto_rules() -> None:
    arguments = _flat(_section("Arguments"))
    assert "`--until ready|moving`" in arguments
    for line in ("`MOVE_RESULT: stopped stage=check reason=until ready`",
                 "`MOVE_RESULT: stopped stage=move reason=until moving`",
                 "`MOVE_RESULT: stopped stage=select reason=--until must be ready or moving`"):  # fmt: skip
        assert line in arguments, line
    assert "`--until moving` still needs `--inputs` and `--approve-deploy`" in arguments
    assert "A later `/move step-1` without `--until` resumes" in arguments
    move = _flat(_section("Stage 3: Move"))
    assert (
        "With `--until ready`, nothing in this stage runs, with or without `--approve-deploy`: "
        "end with `MOVE_RESULT: stopped stage=check reason=until ready` before step 1" in move
    )


def test_until_moving_waits_read_only_for_the_task_to_run_after_the_start() -> None:
    move = _section("Stage 3: Move")
    after = move.split("**6. With `--until moving` only", 1)[1]
    assert move.index("start --job-id") < move.index("**6. With `--until moving` only")
    command = _bash_commands(after)[0]
    args = run_dms_step._parser().parse_args(_helper_argv(command))
    assert args.command == "status" and args.until == "running" and not args.approve
    assert "running" in run_dms_step.UNTIL
    flat = _flat(after)
    assert "end with `MOVE_RESULT: stopped stage=move reason=until moving`" in flat
    assert "up to 4 times" in flat and "task still starting" in flat


def test_prove_stops_on_a_stalled_validation_with_what_to_check() -> None:
    prove = _flat(_section("Stage 4: Prove"))
    assert (
        "`validation_stalled`" in prove
        and '"validation_stalled"' in (REPO_ROOT / "scripts" / "run_dms_step.py").read_text()
    )
    assert (
        "end with `MOVE_RESULT: stopped stage=prove reason=validation not running: "
        "<validation_check>`" in prove
    )
    assert "never keep polling it" in prove
    assert "(`validation_enabled` false) is never reported this way" in prove


def test_prove_relays_each_progress_line_and_reads_the_result_line_last() -> None:
    prove = _flat(_section("Stage 4: Prove"))
    assert (
        'It prints one `"status": "progress"` line per poll, then its result line, always last'
        in prove
    )
    assert "Relay each progress line in one short sentence, from its fields only" in prove
    for field in run_dms_step.PROGRESS_FIELDS:
        if field != "phase":
            assert f"`{field}`" in prove, field
    tool_use = _flat(_section("Tool Use"))
    assert TOOL_USE_RULE + SEARCH_AS_IS in _text()  # the shared paragraph stays word for word
    assert (
        "Prove's poll prints a progress line per poll first: its result line is the last"
        in tool_use
    )


def test_a_stalled_validation_is_offered_one_restart_explained_and_asked_for() -> None:
    """#2048: the restart is explained in plain words, asked for, run once, then Prove again."""
    prove = _flat(_section("Stage 4: Prove"))
    command = (
        "uv run python scripts/run_dms_step.py restart-validation --job-id {job_id} --db {db} "
        "--approve"
    )
    assert command in _bash_commands(_section("Stage 4: Prove"))
    stalled = prove[prove.index("**Validation stalled.**") :]
    for words in ("replication pauses for a minute or two", "resumes from its checkpoint",
                  "nothing reloads", "nothing is deleted"):  # fmt: skip
        assert words in stalled, words
    question = "Restart the replication task of stack {stack_name} to restart DMS validation now?"
    assert f'"{question}" Only on a yes' in stalled
    order = [stalled.index(x) for x in ("nothing is deleted", question, "restart-validation --job-id",
                                        "run the Poll again")]  # fmt: skip
    assert order == sorted(order)  # explained, asked, run, then Prove again
    assert "at most once per run" in stalled
    assert "stalls again" in stalled
    stalled_line = (
        "`MOVE_RESULT: stopped stage=prove reason=validation not running: <validation_check>`"
    )
    assert stalled.count(stalled_line) >= 2  # on a no, and when it stalls again: today's line


def test_auto_never_restarts_a_stalled_validation() -> None:
    prove = _flat(_section("Stage 4: Prove"))
    assert "Under `--auto`, never run `restart-validation`" in prove
    assert "`--approve-deploy` does not cover it" in prove
    headless = _flat(_section("Headless (`--auto`)"))
    assert "never restarts the task" in headless


def test_prove_warns_on_the_sources_retention_and_stops_when_it_turns_critical() -> None:
    """#2060: a warning reaches the customer on the progress line; critical ends Prove."""
    prove = _flat(_section("Stage 4: Prove"))
    assert "when the level is `warning`, add `source_warning` verbatim" in prove
    assert (
        "a result line with `stopped_for` `source_retention`" in prove
        and '"stopped_for"' in (REPO_ROOT / "scripts" / "run_dms_step.py").read_text()
    )
    assert (
        "end with `MOVE_RESULT: stopped stage=prove reason=source retention critical: "
        "<source_retention message>`" in prove
    )
    # Read before the rerun rule, and whatever `reached` says (a replicating line can be critical).
    assert prove.index("Read every result line for `stopped_for` first") < prove.index(
        "Unless the line has `stopped_for`: after a result line with `reached` false"
    )
    assert "(whatever its `reached`)" in prove
    critical = prove.split("a result line with `stopped_for` `source_retention`", 1)[1]
    assert critical.index("show its `source_retention` `message` first") < critical.index(
        "when its `phase` is `replicating`"
    )
    assert "its `check_on_source` when it has one" in prove
    assert "a `task_stopped` line with `stopped_for` ends with this line only" in prove
    stopped = prove.split("A `phase` of `task_stopped`", 1)[1]
    assert stopped.index("`stopped_task` verbatim") < stopped.index("reason=task <task_status>")


def test_ending_the_step_shows_what_is_left_on_the_source() -> None:
    """#2060: the replication slot outlives the stack; the end line says so."""
    ending = _flat(_section("Ending the step"))
    assert "show every entry of the line's `source_followups`" in ending
    assert (
        "reason=step 1 ended: stack <stack_name> deleted ({dms_reports}); on the source: "
        "<source_reminder>`" in ending
    )
    assert "the customer runs them, never you" in ending


# --- A stopped or failed task: one recovery, explained and asked for (#2061) -----------------


def _recovery() -> str:
    prove = _flat(_section("Stage 4: Prove"))
    return prove[prove.index("**Stopped or failed task.**") :]


def test_a_stopped_task_is_offered_one_resume_and_a_failed_one_one_retry() -> None:
    prove = _bash_commands(_section("Stage 4: Prove"))
    for command in ("resume", "retry"):
        line = f"uv run python scripts/run_dms_step.py {command} --job-id {{job_id}} --db {{db}} --approve"
        assert line in prove, command
    recovery = _recovery()
    resume_q = "Resume the replication task of stack {stack_name} now?"
    retry_q = "Retry the replication task of stack {stack_name} now?"
    for question in (resume_q, retry_q):
        assert f'"{question}" Only on a yes' in recovery, question
    assert "at most once per run" in recovery
    # Plain words before each question: what the write does, and what it never does.
    resume = recovery[recovery.index("`recovery` `resume`") : recovery.index(resume_q)]
    for words in ("after its full load finished", "resumes from its checkpoint", "nothing reloads",
                  "nothing is deleted", "ask whether the customer has fixed it first",
                  "`Stop Reason NORMAL`"):  # fmt: skip
        assert words in resume, words
    retry = recovery[recovery.index("`recovery` `retry`") : recovery.index(retry_q)]
    for words in ("the full load never started", "`FullLoadRows`", "refuses and changes nothing",
                  "may already hold rows"):  # fmt: skip
        assert words in retry, words
    assert "the Aurora tables are still empty" not in recovery  # more than the check proves
    assert "a run that already restarted the task, reloaded tables or revalidated" in recovery
    stalled = _flat(_section("Stage 4: Prove")).split("**Validation stalled.**", 1)[1]
    assert "it counts as the run's one recovery" in stalled
    assert "never in a run that already resumed, retried, reloaded tables or revalidated" in stalled
    assert "names a command only when that command would run" in recovery
    assert "A `recovery` of null with `customer_steps`" in recovery
    assert '"recovery"' in (REPO_ROOT / "scripts" / "run_dms_step.py").read_text()
    assert recovery.index("resume --job-id") < recovery.index("run the Poll again")


def test_the_hint_is_shown_verbatim_before_any_recovery_is_offered() -> None:
    recovery = _recovery()
    assert "show its `hint` verbatim" in recovery
    assert recovery.index("`hint`") < recovery.index("Resume the replication task")
    assert "A `hint` of null means" in recovery and "never guess a fix" in recovery
    assert '"hint"' in (REPO_ROOT / "scripts" / "run_dms_step.py").read_text()


def test_a_failed_task_with_rows_loaded_gets_the_customers_steps_and_no_sql_from_us() -> None:
    recovery = _recovery()
    assert '`"kind": "target_may_hold_rows"`' in recovery
    assert "every entry of `customer_steps` verbatim, each on its own" in recovery
    assert "an SQL entry is SQL only" in recovery
    assert "never connects to the customer's databases or runs SQL on them" in recovery
    assert "never run them yourself" in recovery
    assert (
        "`MOVE_RESULT: stopped stage=prove reason=task may have written rows to Aurora: "
        in recovery
    )


def test_auto_never_resumes_or_retries_and_the_task_stopped_ending_is_kept() -> None:
    recovery = _recovery()
    assert "Under `--auto`, never run `resume` or `retry`" in recovery
    assert "`--approve-deploy` does not cover them" in recovery
    headless = _flat(_section("Headless (`--auto`)"))
    assert "never resumes or retries a stopped or failed task" in headless
    ending = "`MOVE_RESULT: stopped stage=prove reason=task <task_status>: <last_failure or stop_reason>`"
    assert recovery.count(ending) >= 2  # on a no, and after a second stop in the same run


def test_a_credentials_error_says_refresh_and_run_again() -> None:
    creds = _flat(_section("Credentials and secrets"))
    assert '`"kind": "credentials"`' in creds
    assert "refresh their credentials" in creds and "run `/move step-1` again" in creds


def test_a_failed_stack_shows_its_first_failure_and_the_way_on_and_is_never_redeployed_here() -> (
    None
):
    """#2063: a rolled-back create is ended by a later /move run, never deleted or redeployed
    by this one or by --auto; a rolled-back update still goes on to Prove."""
    move = _flat(_section("Stage 3: Move"))
    failed = move.split("**Failed stack.**", 1)[1]
    for needle in ("`stack_create_failed`", "`stack_failed`", "`stack_failure`",
                   "`hint` verbatim", "`stack_action` verbatim", "never guess one",
                   "ask `/move step-1` to end the step",
                   "This run never deletes, rolls back or redeploys a failed stack, and neither "
                   "does `--auto` (`--approve-deploy` covers only the normal deploy path, never a "
                   "redeploy after a failed create)",
                   "`MOVE_RESULT: stopped stage=move reason=stack <stack_status>: <stack_failure "
                   "reason, or stack_reason>`",
                   "`UPDATE_ROLLBACK_COMPLETE` is no failed stack"):  # fmt: skip
        assert needle in failed, needle
    assert (
        '`"kind": "stack_not_updatable"`' in move and "handle it as step 3's failed stack" in move
    )
    for command in _bash_commands(_section("Stage 3: Move")):
        assert run_dms_step._parser().parse_args(_helper_argv(command)).command != "teardown"
    resume = _flat(_section("Step 0: The job and the step"))
    assert (
        "`stack_in_review` (a change set was created and never executed) → Move at step 1" in resume
    )


def test_ending_a_failed_create_needs_this_jobs_tag_and_never_touches_termination_protection() -> (
    None
):
    ending = _flat(_section("Ending the step"))
    assert "A stack whose create failed (`stack_create_failed`)" in ending
    assert "only when its own `modernizer:job-id` tag names this job" in ending
    assert (
        "Termination protection is the customer's: never turn it off, and never offer to" in ending
    )


def test_the_stack_wait_after_execute_names_the_change_set_and_handles_stack_in_review() -> None:
    move = _section("Stage 3: Move")
    after = move.split("**3. Wait for the stack.**", 1)[1]
    args = run_dms_step._parser().parse_args(_helper_argv(_bash_commands(after)[0]))
    assert (
        args.command == "status" and args.until == "stack" and args.change_set == "change_set_name"
    )
    flat = _flat(after)
    assert (
        "When Resume brought you here (no change set in this run), leave out `--change-set" in flat
    )
    assert "A `phase` of `stack_in_review` means the change set was never executed" in flat
    assert "Go back to step 1 once in this run" in flat
    assert (
        "`MOVE_RESULT: stopped stage=move reason=stack REVIEW_IN_PROGRESS: change set not executed`"
        in flat
    )


# --- #2062: step 1 shows the full load and replication in progress, never cutover advice ------


def test_move_says_only_that_the_full_load_is_complete_and_replication_is_in_progress() -> None:
    flat = _flat()
    assert 'once the gate passes, say "Full load complete. Replication is in progress."' in flat
    gate = _flat(_section("Stage 4: Prove")).split("**Gate.**", 1)[1]
    assert (
        'On `pass`, say its `result` verbatim ("Full load complete. Replication is in progress.")'
        in gate
    )
    assert (
        "never present the records DMS still validates, or the replication lag, as a signal" in flat
    )


def test_prove_waits_for_replicating_never_for_the_earlier_phase() -> None:
    prove = _flat(_section("Stage 4: Prove"))
    assert "caught_up" not in _text()
    assert "when its `phase` is `replicating`" in prove
    assert "**Gate.** When `phase` is `replicating`" in prove
    assert "`pending_tables` (the tables whose first validation has not finished)" in prove


def test_the_result_line_forms_are_unchanged() -> None:
    """The renamed phase changes no MOVE_RESULT form: headless callers parse these."""
    prove = set(re.findall(r"MOVE_RESULT: stopped stage=prove reason=[^`]*", _text()))
    assert prove == {
        "MOVE_RESULT: stopped stage=prove reason=<its message>",
        "MOVE_RESULT: stopped stage=prove reason=source retention critical: <source_retention message>",
        "MOVE_RESULT: stopped stage=prove reason=still loading: run /move step-1 again to resume",
        "MOVE_RESULT: stopped stage=prove reason=table errors: <the errored tables>",
        "MOVE_RESULT: stopped stage=prove reason=task <task_status>: <last_failure or stop_reason>",
        "MOVE_RESULT: stopped stage=prove reason=task may have written rows to Aurora: empty the "
        "step's tables on Aurora, then reload (customer_steps)",
        "MOVE_RESULT: stopped stage=prove reason=validation not running: <validation_check>",
    }
    assert "`MOVE_RESULT: complete job_id={job_id} step=1 gate=<the gate line's status>`" in _text()
