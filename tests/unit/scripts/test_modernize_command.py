"""Guard rails for /modernize --auto: a headless run must never wait on a human.

This is a tripwire, not a simulation of a real run — the real proof is an
actual headless `claude -p` run against these commands.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

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
    "review-schema.md",
    "synthesize.md",
]

# A line matches a "question" tripwire if it asks the user something or waits
# for them, UNLESS it is explicitly scoped to a non-headless/non---auto path.
QUESTION_PATTERN = re.compile(r"\bAsk\b.*\?")
WAIT_PATTERN = re.compile(r"Wait for the user")
CONFIRM_PATTERN = re.compile(r"\bconfirm", re.IGNORECASE)
AUTO_EXEMPTION_PATTERN = re.compile(r"--auto|headless|[Uu]nless `--auto`")

# Issue #196: in a headless run, subagents repeatedly used Bash (`python3 -c`,
# `jq`, `cat`, `ls`, `sed`) to inspect request files and state instead of the
# Read/Grep tools. In restricted environments those commands are denied,
# which costs turns and can stall a phase. Every command a subagent or the
# orchestrator follows must carry this instruction, worded identically.
# Issue #275: the headless Claude Code session used in CI has no Grep tool,
# so the rule names the allowlisted scripts/search_artifacts.py for searching
# and keeps Grep only as "if this session has one".
TOOL_USE_RULE = (
    "Read files with the Read tool (use `offset`/`limit` for large files). "
    "Search file contents with `uv run python scripts/search_artifacts.py <regex> <path>` "
    "(or the Grep tool if this session has one). Use Bash only for the documented "
    "`uv run python scripts/…` commands; never use `cat`, `jq`, `python3 -c`, `sed`, "
    "`ls`, `cd` chains, heredocs or `grep`."
)

# Said right after the tool-use rule wherever it appears: a chained search is denied as
# a whole and fails the run (#681).
SEARCH_AS_IS = " Use a search's output as is: never pipe, chain or post-process it."

COMMANDS_WITH_TOOL_USE_RULE = [
    "modernize.md",
    "collect.md",
    "triage.md",
    "analyze.md",
    "assign.md",
    "reality-check.md",
    "synthesize.md",
    "design-schema-dynamodb.md",
    "design-schema-elasticache.md",
    "design-schema-opensearch.md",
    "design-schema-documentdb.md",
    "design-schema-aurora-mysql.md",
    "design-schema-aurora-postgresql.md",
    "analyze-aurora-mysql.md",
    "analyze-aurora-postgresql.md",
    "analyze-documentdb.md",
    "analyze-dynamodb.md",
    "analyze-elasticache.md",
    "analyze-opensearch.md",
    "design-schema.md",
    "review-schema.md",
]


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


def test_step_0_asks_no_mode_question() -> None:
    # Issue #346: "both" (chat + UI) is the default experience, so Step 0 no
    # longer asks the user to pick a mode -- it just resolves {experience_mode}.
    text = _modernize_text()
    step_0 = text.split("## Step 0", 1)[1].split("## CRITICAL", 1)[0]
    assert "Never ask the user which mode they want" in step_0
    assert "how would you like to follow" not in step_0.lower()
    assert "pick 1, 2, or 3" not in step_0.lower()
    assert QUESTION_PATTERN.search(step_0) is None


def test_default_experience_mode_is_both() -> None:
    text = _modernize_text()
    assert "Default is `both`" in text
    step_0 = text.split("## Step 0", 1)[1].split("## CRITICAL", 1)[0]
    assert "`--mode` if given, else `both`" in step_0
    assert "`--auto` without `--mode` also means `both`" in text


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


def test_setup_failure_falls_back_to_chat_instead_of_failing() -> None:
    # Issue #346: with "both" the default (no longer a deliberate user
    # choice), a UI that can't start must not take the whole run down with
    # it. Step 0 falls back to chat and keeps going, with or without --auto.
    step_0 = _modernize_text().split("## Step 0", 1)[1].split("## CRITICAL", 1)[0]
    assert "MODERNIZE_RESULT: failed phase=setup" not in step_0
    assert "Set `{experience_mode}` to `chat`" in step_0
    assert "never fail the run over this" in step_0


def test_setup_treats_missing_or_bad_output_as_error_too() -> None:
    # PR #348 review: a missing/unparseable JSON line or a non-zero exit
    # from start_local_ui.py must trigger the same chat fallback as an
    # explicit {"status": "error", ...} line.
    step_0 = _modernize_text().split("## Step 0", 1)[1].split("## CRITICAL", 1)[0]
    assert "missing or unparseable JSON line, or a non-zero exit" in step_0
    assert "counts the same as" in step_0


def test_step_0_uses_a_long_bash_timeout() -> None:
    # PR #348 review: a fresh clone builds the UI from scratch, which can
    # take several minutes -- the default 120000 ms Bash timeout isn't
    # enough, so Step 0 must call out the longer one explicitly.
    step_0 = _modernize_text().split("## Step 0", 1)[1].split("## CRITICAL", 1)[0]
    assert "600000 ms" in step_0


def test_step_0_never_waits_for_the_user_to_confirm_the_ui() -> None:
    # PR #348 review: "ready" already means the health and deep-link checks
    # passed, so Step 0 must not wait for a human to confirm the UI loaded
    # (that contradicts the Waiting Rule, and would hang a headless run).
    step_0 = _modernize_text().split("## Step 0", 1)[1].split("## CRITICAL", 1)[0]
    assert "confirm" not in step_0.lower()
    assert "continue immediately" in step_0


def test_result_line_mode_reflects_the_experience_mode_actually_used() -> None:
    # PR #348 review: mode=<mode> must be {experience_mode} (which is "chat"
    # after a fallback), not the --mode flag the user originally passed.
    text = _modernize_text()
    completion = text.split("### Completion", 1)[1].split("## Waiting Rule", 1)[0]
    assert "where `<mode>` is `{experience_mode}`" in completion
    assert "chat` if the UI never started or fell back" in completion


def test_ui_mode_is_not_called_a_single_channel() -> None:
    # PR #348 review: --mode ui is UI-first (chat still runs, scoped to
    # approval-gate numbers, UI pointers and the result line), not a single
    # channel the way --mode chat is.
    text = _modernize_text()
    assert "picks a single channel" not in text
    assert "UI-first" in text
    arguments = text.split("## Arguments", 1)[1].split("## Step 0", 1)[0]
    assert "no phase summaries" in arguments


def test_ui_mode_suppresses_phase_summaries_but_keeps_approval_gate_numbers() -> None:
    # PR #348 review: in ui mode, chat gives only the approval-gate numbers,
    # UI pointers and the result line -- no reality-check/schema-design/
    # completion phase summaries.
    text = _modernize_text()
    assert "present a brief summary" not in text  # the gate block is the summary (#1307)
    assert "Unless `{experience_mode}` is `ui`, tell the user schema design is complete" in text
    completion = text.split("### Completion", 1)[1].split("## Waiting Rule", 1)[0]
    assert "chat carries only the UI pointers: quote only its `Results page` line" in completion
    decision_gate = text.split("### Decision Gate: Assignment Approval", 1)[1].split(
        "### Phase 6", 1
    )[0]
    assert "including `ui`" in decision_gate


def test_decision_gate_quotes_the_printed_block_not_a_composed_table() -> None:
    # Issue #329 asked the gate to show the concrete distribution; since #1307 the
    # block is printed by a script from the artifacts and the command quotes it.
    text = _modernize_text()
    decision_gate = text.split("### Decision Gate: Assignment Approval", 1)[1].split(
        "### Phase 6", 1
    )[0]
    assert (
        "uv run python scripts/print_summary.py --job-id {job_id} --db {database_name} --gate"
        in decision_gate
    )
    assert "your next chat message must contain its stdout verbatim" in decision_gate
    assert "add no table or figure of your own" in decision_gate
    assert "never come from opening `reality-check/output.json`" in decision_gate
    assert "never tell the user you have not read them" in decision_gate
    assert "It says so itself when reality check changed nothing" in decision_gate
    # The status line still drives the steps after the gate.
    assert "`schema_design_engines`" in text
    assert "`pending_choices`" in text


def test_schema_design_phase_names_the_engine_list_field() -> None:
    # Regression test for the #329 review (PR #407): the orchestrator picked
    # schema design engines from after_distribution (owners only) and silently
    # dropped the cache-layer engine, which never owns a query. Phase 6 must
    # point at the explicit schema_design_engines field instead of leaving the
    # engine list to be inferred from after_distribution or anything else.
    text = _modernize_text()
    phase_6 = text.split("### Phase 6: Schema Design", 1)[1].split("### Phase 7", 1)[0]
    assert "`schema_design_engines`" in phase_6
    assert "never re-derive the list from `after_distribution`" in phase_6
    assert "only if `dynamodb` is in `schema_design_engines`" in phase_6


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


def test_commands_followed_by_subagents_or_orchestrator_state_the_tool_use_rule() -> None:
    for filename in COMMANDS_WITH_TOOL_USE_RULE:
        path = COMMANDS_DIR / filename
        assert path.exists(), f"missing command file: {filename}"
        text = path.read_text()
        assert TOOL_USE_RULE in text, f"{filename}: missing the tool-use rule verbatim"
        assert text.count(TOOL_USE_RULE + SEARCH_AS_IS) == text.count(TOOL_USE_RULE), filename


def test_no_command_or_skill_relies_on_grep_or_glob_tools() -> None:
    # Issue #275: the headless session may have no Grep or Glob tool. "Grep"
    # may only appear inside the shared rule (as "if this session has one"),
    # and "Glob" not at all.
    skills_dir = REPO_ROOT / "src" / "skills"
    files = [*COMMANDS_DIR.glob("*.md"), *skills_dir.rglob("*.md")]
    assert files
    for path in files:
        text = path.read_text()
        assert not re.search(r"\bGlob\b", text), f"{path}: names the Glob tool"
        flat = " ".join(text.split()).replace(TOOL_USE_RULE, "")
        assert not re.search(r"\bGrep\b", flat), f"{path}: names the Grep tool outside the rule"


def test_tool_use_rule_names_an_allowlisted_search_script() -> None:
    assert "uv run python scripts/search_artifacts.py" in TOOL_USE_RULE
    assert (REPO_ROOT / "scripts" / "search_artifacts.py").exists()
    for forbidden in ("`cat`", "`jq`", "`python3 -c`", "`sed`", "`ls`", "heredocs", "`grep`"):
        assert forbidden in TOOL_USE_RULE


def test_modernize_dispatch_text_includes_tool_use_rule() -> None:
    # The subagent dispatch rules must carry the rule too, not just rely on
    # the dispatched command's own skill text having it.
    assert TOOL_USE_RULE in _modernize_text()


def test_modernize_state_updates_use_edit_write_tools() -> None:
    # The orchestrator previously tried `sed -i` on .modernizer-state.json in
    # a headless run (also issue #196); state updates must go through the
    # Edit/Write tools instead.
    text = _modernize_text()
    assert "Edit/Write" in text
    assert "sed" in text  # named explicitly as what NOT to use


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


def test_schema_design_failure_ends_with_failed_phase_result() -> None:
    # A /design-schema-<engine> subagent that still fails validation after its
    # attempts returns `failed`; under --auto that is a phase failure with the
    # usual single retry, then a schema_design_<engine> MODERNIZE_RESULT (#203).
    error_handling = _modernize_text().split("## Error Handling", 1)[1]
    assert "MODERNIZE_RESULT: failed phase=schema_design_<engine>" in error_handling
    assert "Retry it once with a fresh subagent" in error_handling


def test_design_schema_commands_cap_attempts_and_fail_the_phase() -> None:
    # Each engine command bounds its finalize/merge retries and ends `failed`
    # instead of marking the phase complete when validation never passes (#203).
    for path in sorted(COMMANDS_DIR.glob("design-schema-*.md")):
        engine = path.stem.removeprefix("design-schema-").replace("-", "_")
        text = path.read_text()
        assert "at most 3" in text and "attempts in total" in text, path.name
        assert f'`phase_status.schema_design_{engine}` = "failed"' in text, path.name
        assert "Do not mark the phase complete" in text, path.name
        assert "Design only the tables and queries assigned to this engine" in text, path.name


# Issue #246: /modernize dispatched /design-schema-dynamodb, which dispatched
# its own group subagents in the background and ended its turn. The groups'
# completion went to the orchestrator, nothing ran --merge, and the
# orchestrator ended its turn "waiting" with nothing running, so the headless
# run exited with no MODERNIZE_RESULT line. Nesting is now one level deep.
NO_NESTING_RULE = "Do not dispatch subagents yourself."


def _fenced_blocks(text: str) -> list[str]:
    return re.findall(r"```[a-z]*\n(.*?)```", text, flags=re.DOTALL)


def _dispatch_templates(text: str) -> list[str]:
    """The task-text templates /modernize hands to its subagents."""
    return [b for b in _fenced_blocks(text) if b.lstrip().startswith(("Run /", "Follow /"))]


def _phase_6(text: str) -> str:
    return text.split("### Phase 6", 1)[1].split("### Phase 7", 1)[0]


def test_every_modernize_dispatch_template_forbids_nesting_and_carries_tool_rule() -> None:
    templates = _dispatch_templates(_modernize_text())
    # reality check, other engines, DynamoDB group, DynamoDB merge fix, synthesis
    assert len(templates) >= 5, templates
    for template in templates:
        flat = " ".join(template.split())
        assert NO_NESTING_RULE in flat, template
        assert TOOL_USE_RULE in flat, template
        assert "do not ask the user anything" in flat, template


def test_modernize_dispatch_rules_limit_nesting_to_one_level() -> None:
    rules = (
        _modernize_text().split("## Subagent Dispatch Rules", 1)[1].split("## Error Handling")[0]
    )
    assert NO_NESTING_RULE in rules
    assert "one level" in rules


def test_modernize_runs_dynamodb_groups_itself() -> None:
    phase_6 = _phase_6(_modernize_text())
    for step in (
        "--engine dynamodb --split",
        "--engine dynamodb --status",
        "--engine dynamodb --merge",
    ):
        assert step in phase_6, step
    # The orchestrator never hands the whole grouped design to one subagent.
    assert 'Run /design-schema-dynamodb"' not in phase_6
    assert "Run /design-schema-dynamodb for" not in phase_6
    assert "Follow /design-schema-dynamodb **Group draft task**" in phase_6
    assert "Follow /design-schema-dynamodb **Merge fix task**" in phase_6
    assert "at most 3 `--merge` attempts in total" in phase_6
    assert 'phase_status.schema_design_dynamodb` = "failed"' in phase_6
    assert "MODERNIZE_RESULT: failed phase=schema_design_dynamodb" in _modernize_text()


def test_modernize_never_ends_a_turn_waiting_on_nothing() -> None:
    text = _modernize_text()
    section = text.split("## Waiting Rule", 1)[1].split("\n## ", 1)[0]
    assert "Never end a turn waiting unless a dispatched subagent is still running" in section
    assert "phase_status" in section and "--status" in section
    assert "MODERNIZE_RESULT: failed phase=<phase> reason=" in section
    assert "headless and interactive" in section
    assert (
        "Under `--auto`, your final message must always contain a `MODERNIZE_RESULT` line" in text
    )


# Issue #313: a headless /modernize --auto run's merge-fix subagent looped a
# shell `for g in 0 1 2 3; do uv run python scripts/run_schema_design.py …
# --check-costs …; done` around every group draft, because no command gave
# it a single call that re-checks them all. CI's Bash allowlist denies shell
# loops and compound commands, so every bash example in every command file
# must be one call: no `for … in` loop, no `while` loop, no `&&`, and no `;`
# command chain (a `;` inside a quoted argument, e.g. `python -c "a; b"`, is
# not a chain and is fine).
_BASH_BLOCK_PATTERN = re.compile(r"```(?:bash|sh)\n(.*?)```", re.DOTALL)
_QUOTED_PATTERN = re.compile(r"'[^']*'|\"[^\"]*\"")


def _bash_blocks(text: str) -> list[str]:
    return _BASH_BLOCK_PATTERN.findall(text)


def test_no_command_bash_example_suggests_a_shell_loop_or_compound_command() -> None:
    for path in sorted(COMMANDS_DIR.glob("*.md")):
        text = path.read_text()
        for block in _bash_blocks(text):
            assert not re.search(
                r"\bfor\s+\w+\s+in\b", block
            ), f"{path.name}: shell for-loop in a command example:\n{block}"
            assert not re.search(
                r"\bwhile\b", block
            ), f"{path.name}: shell while-loop in a command example:\n{block}"
            assert (
                "&&" not in block
            ), f"{path.name}: '&&' compound command in a command example:\n{block}"
            unquoted = _QUOTED_PATTERN.sub("", block)
            assert (
                ";" not in unquoted
            ), f"{path.name}: ';' command chain in a command example:\n{block}"


def test_check_costs_commands_use_check_costs_all_to_recheck_every_group() -> None:
    # The fix for #313: re-checking several (or all) group drafts is one
    # allowed command, not a loop over --check-costs.
    design_schema_dynamodb = (COMMANDS_DIR / "design-schema-dynamodb.md").read_text()
    merge_fix_task = design_schema_dynamodb.split("## Merge fix task", 1)[1]
    assert "--check-costs-all" in merge_fix_task
    assert "for g in" not in merge_fix_task.lower()


def test_design_schema_dynamodb_group_task_writes_only_its_draft() -> None:
    text = (COMMANDS_DIR / "design-schema-dynamodb.md").read_text()
    group_task = text.split("## Group draft task", 1)[1].split("\n## ", 1)[0]
    assert "schema_draft_group_{G}.json" in group_task
    assert "Do not run `--merge`" in group_task
    assert NO_NESTING_RULE in group_task
    fix_task = text.split("## Merge fix task", 1)[1].split("\n## ", 1)[0]
    assert "Do not run `--merge`" in fix_task
    assert NO_NESTING_RULE in fix_task


def test_design_schema_dynamodb_standalone_waits_before_merging() -> None:
    text = (COMMANDS_DIR / "design-schema-dynamodb.md").read_text()
    assert "Do not end your turn while any group subagent is still running" in text
    assert "--engine dynamodb --status" in text
    # Run as somebody's subagent, it designs the groups itself: no nesting.
    assert "If you are yourself a subagent" in text
    assert NO_NESTING_RULE in text


def test_design_schema_dispatcher_does_not_nest_dynamodb_groups() -> None:
    text = (COMMANDS_DIR / "design-schema.md").read_text()
    assert (
        "its own\n   nested subagents" not in text
        and "nested subagents. This is mandatory" not in text
    )
    assert "/modernize" in text and "Phase 6" in text
    assert NO_NESTING_RULE in text


def test_experience_mode_is_recorded_after_the_state_file_exists() -> None:
    # Run 4 of #246: with --mode the orchestrator tried to Write a sidecar
    # `.modernizer-state.json.mode-note` before run_assessment.py had created
    # the state file. run_assessment.py now records the mode itself (--mode)
    # when it creates the state, and no other file is created next to it.
    text = _modernize_text()
    step_0 = text.split("## Step 0", 1)[1].split("## CRITICAL", 1)[0]
    assert "`--mode {experience_mode}`" in step_0 and "creates" in step_0
    assert "Do not create any other file" in step_0
    assert "Edit `experience_mode`" not in text
    phases_1_5 = text.split("### Phases 1-5", 1)[1].split("###", 1)[0]
    assert (
        "run_assessment.py --file {collector_file} --db-from-file-name --mode {experience_mode}"
        in phases_1_5
    )


def test_database_name_comes_from_the_collect_line_not_the_model() -> None:
    # #1096: the script derives the name from the collection file (the rule is
    # src/shared/collection_database_name.py); the command reads it from the
    # collect status line and never passes --db on the first command.
    phases_1_5 = _modernize_text().split("### Phases 1-5", 1)[1].split("\n### ", 1)[0]
    first = phases_1_5.split("```bash", 1)[1].split("```", 1)[0]
    assert "--db-from-file-name" in first
    assert "--db " not in first
    assert "prints the name as `database_name` on the `collect` line" in phases_1_5
    assert "database_name_source" in phases_1_5
    assert "never derive it yourself" in phases_1_5
    assert "Take `job_id` and `database_name` from the `collect` line" in phases_1_5
    assert "(the script rejects any other name)" not in phases_1_5


def test_merge_drafts_pending_is_handled_as_missing_groups_not_validation() -> None:
    # `--merge` refuses while group drafts are missing (#246): the commands
    # re-dispatch the `missing_groups` instead of counting a merge attempt.
    for text in (
        _phase_6(_modernize_text()),
        (COMMANDS_DIR / "design-schema-dynamodb.md").read_text(),
    ):
        assert "`missing_groups`" in text
        assert "does not count as a `--merge` attempt" in text


def test_merge_failed_and_invalid_drafts_are_handled_in_both_commands() -> None:
    # PR #247 review: --status reports `merge_failed` for a current but failed
    # merge, and unreadable drafts as `drafts_invalid` / `invalid_groups`.
    for text in (
        _phase_6(_modernize_text()),
        (COMMANDS_DIR / "design-schema-dynamodb.md").read_text(),
    ):
        assert '`"status": "merge_failed"`' in text
        assert "Treat it as a `--merge` attempt that printed `validation_failed`" in text
        assert "`drafts_invalid`" in text and "`invalid_groups`" in text
        assert "Any other output" in text and "non-zero exit" in text


def test_modernize_dynamodb_phase_gets_one_retry_in_total() -> None:
    error_handling = _modernize_text().split("## Error Handling", 1)[1]
    assert "The phase gets one retry in total." in error_handling


def test_standalone_dynamodb_redoes_bad_drafts_at_most_once() -> None:
    text = (COMMANDS_DIR / "design-schema-dynamodb.md").read_text()
    step_4 = text.split("4. **Wait for every group**", 1)[1].split("5. **Merge", 1)[0]
    assert "**at most once**" in step_4
    assert 'set `phase_status.schema_design_dynamodb` = "failed" and return `failed`' in step_4


def test_design_schema_never_dispatches_design_schema_dynamodb() -> None:
    text = (COMMANDS_DIR / "design-schema.md").read_text()
    assert "one subagent per engine from step 1 other than DynamoDB" in text
    for match in re.finditer(r"/design-schema-dynamodb", text):
        before = text[max(0, match.start() - 40) : match.start()]
        assert "Never dispatch `" in before, before


def test_merge_failed_is_the_merge_verdict_not_validation_passed() -> None:
    # --status takes merged/merge_failed from the verdict --merge printed; the
    # command text must not tie merge_failed to the output's validation_passed.
    phase_6 = _phase_6(_modernize_text())
    line = next(ln for ln in phase_6.splitlines() if '`"status": "merge_failed"`' in ln)
    assert "or the merged output has `validation_passed: false`" not in line
    assert "printed `validation_failed`" in line


# Issue #272: DynamoDB group inputs ran to 10k-20k lines, and group subagents
# paged them with `sed -n`, searched them with `grep` and wrote their drafts
# with heredoc scripts, all denied. The group task text now says how to read
# (one Read call per `input_pages` page) and how to write (one Write call).
# Searching goes through the allowlisted search_artifacts.py (#275).
GROUP_IO_RULE = (
    "Read the input with the Read tool, one call per page (`offset`, `limit`); "
    "search it with `uv run python scripts/search_artifacts.py <regex> <path>`; "
    "write the draft with one Write tool call. Never use `sed`, `cat`, `grep`, "
    "heredocs or other scripts to read, search or write files."
)


def _group_templates(text: str) -> list[str]:
    return [
        " ".join(t.split())
        for t in _dispatch_templates(text)
        if t.lstrip().startswith("Follow /design-schema-dynamodb **Group draft task**")
    ]


def test_dynamodb_group_dispatch_says_how_to_read_and_write() -> None:
    for text in (_modernize_text(), (COMMANDS_DIR / "design-schema-dynamodb.md").read_text()):
        templates = _group_templates(text)
        assert len(templates) == 1, templates
        template = templates[0]
        assert "Input: {INPUT_FILE}, in Read pages {INPUT_PAGES}." in template
        assert GROUP_IO_RULE in template
        assert TOOL_USE_RULE in template  # the general rule stays word for word
        assert "`input_pages`" in text  # the --split line field it comes from


def test_dynamodb_group_task_reads_by_pages_and_writes_in_one_call() -> None:
    text = (COMMANDS_DIR / "design-schema-dynamodb.md").read_text()
    group_task = text.split("## Group draft task", 1)[1].split("\n## ", 1)[0]
    flat = " ".join(group_task.split())
    assert GROUP_IO_RULE in flat
    assert "one call per page of the group's `input_pages` (`offset`, `limit`)" in flat
    assert "the whole JSON in one Write tool call" in flat
    assert "Do not generate it with a script." in flat
    assert "`query_text_lines`" in flat


def test_dynamodb_group_task_halves_a_page_read_refuses_and_skips_coverage() -> None:
    text = (COMMANDS_DIR / "design-schema-dynamodb.md").read_text()
    flat = " ".join(text.split("## Group draft task", 1)[1].split("\n## ", 1)[0].split())
    assert (
        "If Read reports a page is too large, read it in two halves with `offset`/`limit`" in flat
    )
    assert "Skip the skill's coverage check" in flat
    assert "would not fit in three Read pages" in text


# #625 review: the commands point at skill text by name instead of repeating
# it, so a later skill trim must not drop what they point at.
SKILLS_DIR = REPO_ROOT / "src" / "skills"


def test_group_draft_validation_passed_does_not_fail_a_merge() -> None:
    # #246: group subagents report validation_passed; a merge that passed
    # stays passed even when one group's own check is false.
    line = next(
        ln for ln in _phase_6(_modernize_text()).splitlines() if '`"status": "merge_failed"`' in ln
    )
    assert "a group draft's own `validation_passed: false` does not make a merge fail" in line


def test_state_updates_never_use_sed() -> None:
    assert "never `sed` or another Bash edit" in _modernize_text()


def test_skill_sections_the_commands_point_at_exist() -> None:
    dynamodb = (COMMANDS_DIR / "design-schema-dynamodb.md").read_text()
    assert "the skill's Output Construction rules" in dynamodb
    skill = (SKILLS_DIR / "dynamodb-data-modeling.md").read_text()
    assert re.search(r"^#+ .*Output Construction", skill, re.MULTILINE)
    for field in ("trade_offs", "unsupported_patterns", "hot_partition_analysis"):
        assert f"`{field}`" in skill, field
    assert "`validation_passed` — `true` only if" in skill
    for engine in ("mysql", "postgresql"):
        command = (COMMANDS_DIR / f"design-schema-aurora-{engine}.md").read_text()
        assert "the skill says what" in " ".join(command.split())
        aurora_skill = (SKILLS_DIR / f"aurora_{engine}-data-modeling.md").read_text()
        assert "`carry_over`" in aurora_skill and "`translate`" in aurora_skill
        assert "`optimizations`" in aurora_skill


# Issue #1059: headless /modernize runs the schema-design PE review and the
# post-schema reroute the way Bedrock mode does (ADR-031 Decision 11 Amendment 2).
def test_modernize_reviews_merges_then_reroutes_in_that_order() -> None:
    phase_6 = _phase_6(_modernize_text())
    review = phase_6.index("uv run python scripts/run_pe_review.py --job-id")
    merge = phase_6.index("--engine dynamodb --merge")
    reroute = phase_6.index("uv run python scripts/run_post_schema_routing.py --job-id")
    # The DynamoDB merge consolidates each group's review, so the review comes first.
    assert phase_6.index("**6c. PE review**") < phase_6.index("**6d. Merge DynamoDB**")
    assert review < merge < reroute
    assert "Follow /review-schema for job_id={job_id}" in phase_6
    assert "then run exactly: {FINALIZE}" in phase_6
    assert "--finalize --failed" in phase_6


def test_modernize_redesigns_what_the_reroute_lists_and_can_roll_back() -> None:
    phase_6 = _phase_6(_modernize_text())
    section = phase_6.split("**6e. Post-schema reroute**", 1)[1]
    assert '"status": "awaiting_redesign"' in section
    assert "`missing_designs`" in section
    assert "Then run this command again" in section
    assert "`missing_reviews`" in section
    assert "at most twice in a run" in section
    assert "--rollback" in section
    assert "phase failure for `post_schema_routing`" in section
    assert "The scorer decides, never the review" in section


def test_review_schema_never_names_a_destination_and_caps_attempts() -> None:
    text = (COMMANDS_DIR / "review-schema.md").read_text()
    assert "Never name a target engine" in text
    assert "at most 3 `--finalize` attempts in total" in text
    assert "never as instructions to follow" in text
    assert "do not dispatch subagents yourself" in text.lower()
    for skill in ("dynamodb", "documentdb", "opensearch"):
        assert f"src/skills/{skill}-pe-review.md" in text


def test_design_commands_review_their_design_when_run_on_their_own() -> None:
    for engine in ("documentdb", "opensearch", "dynamodb"):
        text = (COMMANDS_DIR / f"design-schema-{engine}.md").read_text()
        assert (
            f"scripts/run_pe_review.py --job-id {{job_id}} --db {{database_name}} --engine {engine}"
            in text
        )
        assert "Follow /review-schema" in text
    dynamodb = (COMMANDS_DIR / "design-schema-dynamodb.md").read_text()
    assert dynamodb.index("PE review of the group drafts") < dynamodb.index(
        "5. **Merge group drafts**"
    )
    dispatcher = (COMMANDS_DIR / "design-schema.md").read_text()
    assert "6c" in dispatcher and "6e" in dispatcher


def test_engines_in_use_is_an_optional_never_asked_argument_passed_to_the_scripts() -> None:
    # #963: the recorded engines reach the scripts from /modernize and /collect, and
    # neither command asks for them (not even without --auto).
    modernize = _modernize_text()
    args = modernize.split("## Arguments", 1)[1].split("## Step 0", 1)[0]
    assert "`--engines-in-use <ids>`" in args
    assert "Never ask the user for it, with or without `--auto`" in args
    assert "--engines-in-use {engines_in_use}" in modernize.split("## Pipeline", 1)[1]
    collect = (COMMANDS_DIR / "collect.md").read_text()
    assert "`--engines-in-use <ids>`" in collect
    assert "Never ask for it" in collect
    assert "add `--engines-in-use <ids>` to the command" in collect
    for text in (modernize, collect):
        assert TOOL_USE_RULE in text


def test_the_scripts_the_commands_name_accept_engines_in_use() -> None:
    for script in ("run_assessment.py", "run_collect.py"):
        source = (COMMANDS_DIR.parents[1] / "scripts" / script).read_text()
        assert '"--engines-in-use"' in source, script


def test_target_databases_is_an_optional_never_asked_argument_passed_to_the_script() -> None:
    # #1328: the customer's engine scope reaches run_assessment.py from /modernize only
    # when the user gives one; /modernize never asks for it.
    modernize = _modernize_text()
    args = modernize.split("## Arguments", 1)[1].split("## Step 0", 1)[0]
    assert "`--target-databases <ids>`" in args
    line = next(x for x in args.splitlines() if "`--target-databases <ids>`" in x)
    assert "Never ask the user for it, with or without `--auto`" in line
    assert "absent means every engine" in line
    assert "on the first `scripts/run_assessment.py` command only" in line
    assert "in Phases 1-5" not in line  # the resume commands refuse the flag
    pipeline = modernize.split("## Pipeline", 1)[1]
    assert "add `--target-databases {target_databases}` to it" in pipeline
    # #1328 review: no command that continues a job past triage carries the flag.
    later = [
        x
        for x in pipeline.splitlines()
        if "scripts/run_assessment.py" in x
        and any(f in x for f in ("--resume-reality-check", "--choose", "--decide-for-me"))
    ]
    assert later, "the resume and choice commands are listed"
    assert not any("--target-databases" in x for x in later), later
    source = (COMMANDS_DIR.parents[1] / "scripts" / "run_assessment.py").read_text()
    assert '"--target-databases"' in source


# Issue #1233: every UI pointer names the job's own page, quoted from
# `start_local_ui.py --page` output, never the UI root or a hand-built URL.
_HAND_BUILT_UI_URL = re.compile(r"https?://localhost(?!:<port>)(?::\d+)?(?:/[^\s>)`]*)?")


def test_approval_gate_points_at_the_job_summary_page_from_script_output() -> None:
    # Since the PR 1358 e2e-llm runs the printed gate block carries the summary-page URL
    # itself (unless chat), so one quoted block shows both and no second command can be
    # batched away (#1307).
    decision_gate = _modernize_text().split("### Decision Gate: Assignment Approval", 1)[1]
    decision_gate = decision_gate.split("### Phase 6", 1)[0]
    assert (
        "uv run python scripts/print_summary.py --job-id {job_id} --db {database_name} "
        "--gate --mode {experience_mode}" in decision_gate
    )
    assert "scripts/start_local_ui.py --page summary" not in decision_gate
    assert "/analysis/monitor/summary/<job_id>" in decision_gate
    assert "never give the UI root or build the URL yourself" in decision_gate


def test_the_gate_block_is_written_out_before_schema_design_starts() -> None:
    # The PR 1358 e2e-llm runs batched the gate command with Phase 6's first command and
    # never wrote the block out: tool output is not shown to the user.
    decision_gate = _modernize_text().split("### Decision Gate: Assignment Approval", 1)[1]
    decision_gate = " ".join(decision_gate.split("### Phase 6", 1)[0].split())
    assert "Run it on its own, never in the same batch of tool calls as another command" in (
        decision_gate
    )
    assert "start Phase 6 only in a later message" in decision_gate
    assert "before any Phase 6 command or dispatch" in decision_gate


def test_the_gate_section_names_the_hook_that_holds_schema_design() -> None:
    # #1401: the wording alone did not hold; a hook refuses Phase 6 until the gate ran first,
    # and the orchestrator must know what to do when it does.
    decision_gate = _modernize_text().split("### Decision Gate: Assignment Approval", 1)[1]
    decision_gate = " ".join(decision_gate.split("### Phase 6", 1)[0].split())
    assert "`scripts/hooks/require_gate_shown.py`" in decision_gate
    # Maintainer decision on #1401: the gate's own output is what the user sees; the hook holds
    # only the order.
    assert "unless a successful gate ran in an earlier message" in decision_gate
    assert "run the gate on its own first, then start schema design" in decision_gate
    assert "thinking" not in decision_gate


def test_auto_skips_only_the_gate_question_never_its_block_or_url() -> None:
    # Issue #1242: an --auto --mode both run skipped the gate table and URL.
    text = _modernize_text()
    auto_arg = next(line for line in text.splitlines() if line.startswith("- `--auto`"))
    assert "Skips every decision gate's question (auto-approve), never what the gate shows" in (
        auto_arg
    )
    decision_gate = text.split("### Decision Gate: Assignment Approval", 1)[1]
    decision_gate = decision_gate.split("### Phase 6", 1)[0]
    assert "With `--auto`, skip only that question" in decision_gate
    assert "the gate block and, unless `chat`, the summary-page `ui_url` are still shown" in (
        decision_gate
    )
    assert "in that order, before Phase 6 starts" in decision_gate


def test_completion_gets_the_results_page_url_from_the_printed_summary() -> None:
    # Since #1307 the final summary lists the page URLs itself (job_page_url, the function
    # `start_local_ui.py --page` prints), so the command no longer asks for them separately.
    completion = _modernize_text().split("### Completion", 1)[1].split("## Waiting Rule", 1)[0]
    assert "--final --mode {experience_mode}" in completion
    assert "the URLs of the results page and the summary page" in completion
    assert "start_local_ui.py --page results" not in completion
    # #1401: its output carries the pointer, so it runs only once synthesis has reported back.
    assert "Run it only after the `/synthesize` subagent has reported back" in completion


def test_no_command_hard_codes_a_local_ui_url() -> None:
    # The only localhost text a command may carry is the documented shape of
    # the `ui_url` it quotes (`http://localhost:<port>/...`), never a port.
    offenders = [
        f"{path.name}: {match.group(0)}"
        for path in sorted(COMMANDS_DIR.glob("*.md"))
        for match in _HAND_BUILT_UI_URL.finditer(path.read_text())
    ]
    assert offenders == []


def test_every_page_a_command_asks_for_is_one_start_local_ui_prints() -> None:
    from scripts.start_local_ui import JOB_PAGES

    asked = {
        page
        for path in COMMANDS_DIR.glob("*.md")
        for page in re.findall(r"start_local_ui\.py --page (\S+) --job-id", path.read_text())
    }
    # The summary and results pages come from print_summary.py's --gate and --final (#1307).
    assert asked == {"assignments"}
    assert asked <= set(JOB_PAGES)


def test_every_page_pointer_follows_the_experience_mode() -> None:
    # #1233 review: a chat run starts no UI, so no page URL may be printed for it. Since
    # #1307 /modernize asks for no page itself: print_summary.py prints the URLs only
    # when its --mode is not chat, and every call passes the run's mode.
    text = _modernize_text()
    assert "start_local_ui.py --page" not in text
    calls = re.findall(r"scripts/print_summary\.py ([^\n`]*)", text)
    assert len(calls) == 2
    assert all(c.endswith("--mode {experience_mode}") for c in calls), calls


def test_the_orchestrator_sets_single_pass_schema_status_from_the_script() -> None:
    # #332: subagents reported complete without writing phase_status; the orchestrator
    # now reads --status for every non-DynamoDB engine and sets it itself.
    phase_6 = _modernize_text().split("**6b.", 1)[1].split("**6c.", 1)[0]
    assert "--engine <engine> --status" in phase_6
    assert "not from the subagent's report" in phase_6
    assert (
        '`"status": "complete"`: set `phase_status.schema_design_<engine>` = "complete"' in phase_6
    )
    for status in ("validation_failed", "finalize_pending", "not_finalized"):
        assert f'"{status}"' in phase_6, status


def test_error_handling_retries_every_non_complete_schema_status() -> None:
    # #1294 review: not_finalized and finalize_pending fall under the one-retry rule too.
    text = _modernize_text()
    bullet = text.split("**Schema design under `--auto`:**", 1)[1].split("\n- ", 1)[0]
    for status in ("validation_failed", "finalize_pending", "not_finalized"):
        assert status in bullet, status
    assert "Retry it once with a fresh subagent, then run `--status` again" in bullet
    assert "MODERNIZE_RESULT: failed phase=schema_design_<engine>" in bullet


def test_the_cache_layer_fit_is_never_presented_as_a_workload_fit() -> None:
    # Issue #1251: a final summary showed ElastiCache "Workload fit 41%" next to
    # the owners' 73% and 85%. Since #1307 the script prints the summary, and its
    # cache layer row carries the reads it fronts (tests/unit/report/test_printed_summary.py).
    synthesize = (COMMANDS_DIR / "synthesize.md").read_text()
    present = synthesize.split("5. **Present report**", 1)[1].split("6. **Update state**", 1)[0]
    present = " ".join(present.split())
    assert "the cache layer's figure is the reads it fronts, never a workload fit" in present
    completion = _modernize_text().split("### Completion", 1)[1].split("## Waiting Rule", 1)[0]
    assert "The cache layer's figure is the reads it fronts, never shown as a workload fit" in (
        completion
    )


# Issue #1249: the final summary states only facts that were read, and never
# narrates the run's own process.
def _completion_text() -> str:
    return _modernize_text().split("### Completion", 1)[1].split("## Waiting Rule", 1)[0]


def test_completion_summary_is_printed_from_facts_and_quoted_verbatim() -> None:
    completion = _completion_text()
    assert (
        "uv run python scripts/print_summary.py --job-id {job_id} --db {database_name} --final"
        in completion
    )
    assert "from the synthesis `report.json` facts" in completion
    assert "your next chat message must contain its stdout verbatim" in completion
    assert "or open `report.json` or any other artifact for it" in completion
    assert "at the depth of the results page" in completion
    assert "one line per engine with why" in completion
    assert "(it names any file that was not rendered)" in completion
    assert "Build the final summary only from" not in completion  # no longer composed


def _gate_section() -> str:
    gate = _modernize_text().split("### Decision Gate: Assignment Approval", 1)[1]
    return " ".join(gate.split("### Phase 6", 1)[0].split())


def test_the_gate_block_is_pasted_into_the_next_chat_message_before_the_question() -> None:
    # #1605: Claude Code shows a tool call collapsed, so an interactive run said "The block
    # above is the approval gate" over a block the user never saw.
    gate = _gate_section()
    assert "The user does not see a command's output: Claude Code shows the call collapsed" in (
        gate
    )
    assert (
        "your next chat message must contain its stdout verbatim, the whole Markdown block "
        "(including the `Summary page` line unless `{experience_mode}` is `chat`), and only then "
        "ask the approval question below" in gate
    )
    assert 'Never write "the block above"' in gate
    assert gate.index("must contain its stdout verbatim") < gate.index(
        'Ask: "Approve this assignment'
    )
    assert "`scripts/hooks/remind_quote_printed_summary.py`" in gate


def test_the_final_summary_and_result_line_are_in_the_chat_message() -> None:
    completion = " ".join(_completion_text().split())
    assert "The user does not see a command's output (Claude Code shows the call collapsed)" in (
        completion
    )
    assert "your next chat message must contain its stdout verbatim in every mode but `ui`" in (
        completion
    )
    assert "quote only its `Results page` line there" in completion
    assert 'Never write "the summary above"' in completion
    assert "in that same chat message after the quoted summary" in completion


def test_no_command_claims_the_user_sees_tool_output() -> None:
    # #1605: the false claim behind the regression, gone from every command.
    for path in sorted(COMMANDS_DIR.glob("*.md")):
        text = " ".join(path.read_text().split())
        assert "they see a command's output" not in text, path.name
        assert "output is what the user sees" not in text, path.name
        assert "its output is what the user sees" not in text, path.name
    synthesize = " ".join((COMMANDS_DIR / "synthesize.md").read_text().split())
    assert "quote it verbatim in your chat message (the user does not see a command's output" in (
        synthesize
    )


def test_completion_summary_never_narrates_the_run_itself() -> None:
    completion = _completion_text()
    assert "The summary is about the assessment, never about the run" in completion
    assert "do not name a phase or step number" in completion


@pytest.mark.passes_on_main  # guards result-line and tool-use wording main already has
def test_completion_keeps_the_result_line_rules() -> None:
    completion = _completion_text()
    assert "End the run with exactly one line `MODERNIZE_RESULT: complete job_id=<id>" in (
        completion
    )
    assert "failed phase=<phase> reason=<one line>" in completion
    assert "Under `--auto`, your final message must always contain a `MODERNIZE_RESULT` line" in (
        completion
    )
    # The Completion section dispatches nothing; the tool-use rule sits in the
    # Phase 7 dispatch just before it and must stay word for word.
    phase_7 = _modernize_text().split("### Phase 7: Synthesis", 1)[1].split("### Completion", 1)[0]
    assert TOOL_USE_RULE + SEARCH_AS_IS in phase_7


def test_synthesize_present_report_quotes_the_printed_summary() -> None:
    text = (COMMANDS_DIR / "synthesize.md").read_text()
    step = " ".join(
        text.split("5. **Present report**", 1)[1].split("6. **Update state**", 1)[0].split()
    )
    assert (
        "uv run python scripts/print_summary.py --job-id {job_id} --db {database_name} --final"
        in step
    )
    assert "quote it verbatim" in step
    assert "leaves out a line whose figure the report does not carry" in step
    assert "Never comment on what you did or did not read" in step
    assert "read `report.json` for it" in step


def test_the_commands_run_print_summary_only_with_its_documented_flags() -> None:
    runs: list[str] = []
    for path in sorted(COMMANDS_DIR.glob("*.md")):
        for match in re.finditer(r"scripts/print_summary\.py ([^\n`]*)", path.read_text()):
            args = match.group(1).split()
            assert args[:4] == ["--job-id", "{job_id}", "--db", "{database_name}"], (
                path.name,
                args,
            )
            assert args[4:] in (
                ["--gate", "--mode", "{experience_mode}"],
                ["--final"],
                ["--final", "--mode", "{experience_mode}"],
            ), (path.name, args)
            runs.append(f"{path.name} {args[4]}")
    # The gate block and the completion summary in /modernize, and /synthesize's report step.
    assert sorted(runs) == ["modernize.md --final", "modernize.md --gate", "synthesize.md --final"]


# Issue #354: the DynamoDB groups showed up as "DynamoDB group 0 draft" and the
# reconciliation as "merge fix (attempt 1)": internal names, numbered from 0, with no
# total. The commands now name each group a part of one design, from 1, with the total
# and its query count (`--split` prints `query_count`), and the merge and fix by what
# they do. Section names, file names and `--status` keys stay as they are.
PART_LABEL = "`DynamoDB design, part {P} of {T} (queries {F}-{L} of {X})`"
MERGE_LABEL = "`Merging the {T} DynamoDB parts into one design`"
FIX_LABEL = "`Reconciling overlapping tables between parts (attempt {A} of 3)`"
PROGRESS_LABEL = "`DynamoDB design: {D} of {T} parts done`"


def test_modernize_labels_dynamodb_parts_merge_and_fix_for_the_user() -> None:
    phase_6 = " ".join(_phase_6(_modernize_text()).split())
    for label in (PART_LABEL, MERGE_LABEL, FIX_LABEL, PROGRESS_LABEL):
        assert label in phase_6, label
    assert "`{P}` = `group_index` + 1" in phase_6
    assert "`{F}` and `{L}` = that group's `query_first` and `query_last`" in phase_6
    assert "`{X}` = the line's `query_total`" in phase_6
    assert "A `group N` in a `--merge` line is part N + 1." in phase_6
    # The --split line's fields name query_count, so the label has its number.
    assert "`query_count`, `query_first`, `query_last`, `input_pages`" in phase_6
    # Headless and UI-first runs stay compact.
    assert "Under `--auto` or `--mode ui`, skip the progress lines" in phase_6
    # Internal names are kept, so artifacts and other tests are unaffected.
    assert "**Group draft task**" in phase_6 and "**Merge fix task**" in phase_6


def test_design_schema_commands_use_the_same_dynamodb_labels() -> None:
    dispatcher = (COMMANDS_DIR / "design-schema.md").read_text()
    assert "**DynamoDB labels**" in dispatcher and PART_LABEL in dispatcher
    standalone = " ".join((COMMANDS_DIR / "design-schema-dynamodb.md").read_text().split())
    for label in (PART_LABEL, MERGE_LABEL, FIX_LABEL, PROGRESS_LABEL):
        assert label in standalone, label
    assert "`query_count`, `query_first`, `query_last`, `input_pages`" in standalone


@pytest.mark.passes_on_main  # guards the field the labels read
def test_split_status_line_carries_each_groups_query_count(tmp_path) -> None:
    """The label's query count comes from the `--split` line, never from a count."""
    import importlib

    from src.storage.local_store import LocalArtifactStore

    run_schema_design = importlib.import_module("scripts.run_schema_design")
    store = LocalArtifactStore(base_dir=str(tmp_path))
    manifest = {"groups": [{"group_index": 0, "primary_tables": ["t"], "query_count": 18}]}
    (entry,) = run_schema_design._group_entries(store, "db/job/schema-dynamodb/v1", manifest)
    assert entry["query_count"] == 18


def test_split_status_line_numbers_queries_consecutively_in_group_order(tmp_path) -> None:
    """Part 1 gets 1-N and part 2 continues from N+1; the range comes from the script."""
    import importlib

    from src.storage.local_store import LocalArtifactStore

    run_schema_design = importlib.import_module("scripts.run_schema_design")
    store = LocalArtifactStore(base_dir=str(tmp_path))
    counts = [18, 20, 0, 7]
    manifest = {
        "groups": [
            {"group_index": i, "primary_tables": ["t"], "query_count": n}
            for i, n in enumerate(counts)
        ]
    }
    entries = run_schema_design._group_entries(store, "db/job/schema-dynamodb/v1", manifest)
    assert [(e["query_first"], e["query_last"]) for e in entries] == [
        (1, 18),
        (19, 38),
        (None, None),
        (39, 45),
    ]


# #1593: a headless run piped `--status` into `uv run python` to pick out its status
# (a hook refused it): every `--status` block says how to read its line instead,
# pointing at a file inside the working directory, never at one Claude Code saved
# outside it.
STATUS_LINE_RULE = (
    "Run it exactly as shown, on its own, and read `status` and the keys named here "
    "from the one short JSON line it prints. Never pipe it into another command "
    "(`uv run python`, `tail`, `jq`, ...): a hook refuses a piped command. For DynamoDB "
    "the line lists only the groups to redo; every group's entry is in the `--split` "
    "line and in the `manifest` file the line names, under `artifacts/`: read that "
    "file with the Read tool."
)
_STATUS_BLOCK = re.compile(r"```bash\n[^`]*--status\n[ ]*```\n")


@pytest.mark.parametrize("name", ["modernize.md", "design-schema-dynamodb.md"])
def test_every_status_block_says_how_to_read_its_line_without_a_pipe(name: str) -> None:
    text = (COMMANDS_DIR / name).read_text()
    blocks = list(_STATUS_BLOCK.finditer(text))
    assert blocks, name
    for block in blocks:
        after = " ".join(text[block.end() : block.end() + 600].split())
        assert after.startswith(STATUS_LINE_RULE), (name, after[:200])


def test_the_group_draft_runs_its_cost_check_right_after_the_write() -> None:
    """#1593: twice a Group draft subagent called an unlisted settings skill between
    its Write and `--check-costs` (denied under dontAsk). Step 5 says the next call is
    the check, and that the task calls no skill once it has its instructions."""
    text = (COMMANDS_DIR / "design-schema-dynamodb.md").read_text()
    task = text[text.index("## Group draft task") : text.index("## Merge fix task")]
    step5 = task[task.index("5. Write the draft") : task.index("6. Run the cost")]
    assert "the next tool call after the Write is the `--check-costs` command" in step5
    assert "never calls the Skill tool" in step5


def test_the_merge_fix_task_reads_check_costs_all_details_from_artifacts() -> None:
    """#1593: --check-costs-all prints one short line and writes every group's
    result to a details file under artifacts/; the task says to Read it."""
    text = (COMMANDS_DIR / "design-schema-dynamodb.md").read_text()
    task = text[text.index("## Merge fix task") :]
    after = task[task.index("--check-costs-all\n```") :][:700]
    assert "`details`, a file under `artifacts/`" in after
    assert "Read that file with the Read tool; never pipe the command." in after


def test_phase_6_waits_on_the_status_lines_never_on_a_tally() -> None:
    # A scoped discourse run (45 DynamoDB parts) ended after its last part reported:
    # the orchestrator, told to write no progress text, kept no tally and replied
    # "Still waiting on the remaining design subagents." 21 times with nothing left
    # running, and never ran --status. Each report now runs the status command, and
    # its line decides whether to wait or go on.
    text = " ".join(_modernize_text().split())
    phase_6 = " ".join(_phase_6(_modernize_text()).split())
    assert "reply with no text and wait for the next report" not in text
    assert "is never your own tally of reports: the status lines say it" in phase_6
    design = phase_6.split("**Done signal.**", 1)[1].split("**6c.", 1)[0]
    assert (
        "After every design subagent's report, before anything else, run the DynamoDB `--status`"
        in design
    )
    assert "names a group whose part has not reported yet: that part is still running" in design
    assert "End the turn with no text" in design
    assert "`merge_pending` (every group draft is written" in design
    assert "Go on at once, in the same turn" in design
    assert "name only groups whose part has already reported" in design
    # #1594's short --status line: only the groups to redo, plus group_count and the
    # manifest path; drafts_missing holds group indexes, so the part number is spelled out.
    assert "`drafts_missing` (group indexes; part = index + 1)" in design
    assert "`group_count`" in design and "`manifest` path" in design
    assert "Never end a turn after a report without that status line" in design
    review = phase_6.split("**6c.", 1)[1].split("**6d.", 1)[0]
    assert (
        "**Done signal.** After every review subagent's report, run the same `scripts/run_pe_review.py` command again"
        in review
    )
    assert '`"status": "complete"` means every review is in: go on at once' in review
    waiting = text.split("## Waiting Rule", 1)[1].split("## Subagent Dispatch Rules", 1)[0]
    assert "a subagent is still running only when the last status line" in waiting
    assert TOOL_USE_RULE in _modernize_text()


def test_the_waiting_rule_names_the_stop_hook_that_holds_it() -> None:
    # #1574: text alone was not enough (a later stack run stalled the same way), so a
    # Stop hook refuses the stall; the command says what its refusal means.
    waiting = _modernize_text().split("## Waiting Rule", 1)[1].split("## Subagent Dispatch", 1)[0]
    waiting = " ".join(waiting.split())
    assert "`scripts/hooks/require_modernize_result.py`" in waiting
    assert "`Blocked: nothing is running ...`" in waiting
    assert (REPO_ROOT / "scripts" / "hooks" / "require_modernize_result.py").exists()
