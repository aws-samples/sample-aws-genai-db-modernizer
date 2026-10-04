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

# Issue #196: in a headless run, subagents repeatedly used Bash (`python3 -c`,
# `jq`, `cat`, `ls`, `sed`) to inspect request files and state instead of the
# Read/Grep tools. In restricted environments those commands are denied,
# which costs turns and can stall a phase. Every command a subagent or the
# orchestrator follows must carry this instruction, worded identically.
TOOL_USE_RULE = (
    "Inspect files with the Read and Grep tools. Use Bash only for the "
    "documented `uv run python scripts/…` commands; do not use `cat`, `jq`, "
    "`python3 -c`, `sed`, `ls` or `cd` chains."
)

COMMANDS_WITH_TOOL_USE_RULE = [
    "modernize.md",
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


def test_commands_followed_by_subagents_or_orchestrator_state_the_tool_use_rule() -> None:
    for filename in COMMANDS_WITH_TOOL_USE_RULE:
        path = COMMANDS_DIR / filename
        assert path.exists(), f"missing command file: {filename}"
        text = path.read_text()
        assert TOOL_USE_RULE in text, f"{filename}: missing the tool-use rule verbatim"


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
        "run_assessment.py --file {collector_file} --db {database_name} --mode {experience_mode}"
        in (phases_1_5)
    )


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
    assert "one subagent per selected engine other than DynamoDB" in text
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
GROUP_IO_RULE = (
    "Read the input with the Read tool, one call per page (`offset`, `limit`); "
    "search with the Grep tool if this session has one, else Read the page again; "
    "write the draft with one Write tool call. Never use `sed`, `cat`, `grep`, "
    "heredocs or scripts to read, search or write files."
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
