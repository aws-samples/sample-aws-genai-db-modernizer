"""Hook 5: no schema design before the approval gate is in the chat (#1401, #1626)."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from scripts.hooks import require_gate_shown as hook
from tests.unit.hooks.conftest import bash

JOB = "f71cb847"
URL = f"http://localhost:3000/analysis/monitor/summary/{JOB}"
RESUME = (
    f"uv run python scripts/run_assessment.py --job-id {JOB} --db wordpress --resume-reality-check"
)
GATE = f"uv run python scripts/print_summary.py --job-id {JOB} --db wordpress --gate --mode both"
SPLIT = (
    f"uv run python scripts/run_schema_design.py --job-id {JOB} --db wordpress "
    "--engine dynamodb --split"
)
DESIGN_AURORA = (
    f"uv run python scripts/run_schema_design.py --job-id {JOB} --db wordpress "
    "--engine aurora_mysql --llm-mode external"
)
HEADING = f"## Assignment for approval: wordpress (job {JOB})"
BLOCK = (
    f"{HEADING}\n\nRecommended architecture: Aurora MySQL, DynamoDB and ElastiCache\n\n"
    "| Engine | Queries before | Queries final | Share of final | Outcome |\n\n"
    f"- Summary page (the query flow and every query's engine): {URL}\n"
)
CHAT_BLOCK = f"{HEADING}\n\n| Engine | Queries before | Queries final |\n"
STATUS = '{"phase": "reality_check", "status": "complete", "assignment_version": 2}'
ISSUE_URL = "https://github.com/example/repo/issues/1"


def _prompt(command: str, args: str = "") -> dict:
    """A top-level slash-command prompt, as the session transcript records it."""
    text = (
        f"<command-message>{command}</command-message>\n<command-name>/{command}</command-name>\n"
        f"<command-args>{args}</command-args>"
    )
    return {
        "type": "user",
        "parent_tool_use_id": None,
        "message": {"role": "user", "content": text},
    }


def _use(
    use_id: str, command: str, *, msg: str | None = None, parent: str | None = None, tool="Bash"
) -> dict:
    data = {"command": command} if tool == "Bash" else {"prompt": command}
    return {
        "type": "assistant",
        "parent_tool_use_id": parent,
        "message": {
            "role": "assistant",
            "id": msg or f"msg-{use_id}",
            "content": [{"type": "tool_use", "id": use_id, "name": tool, "input": data}],
        },
    }


def _result(use_id: str, text: str, *, parent: str | None = None, is_error: bool = False) -> dict:
    block: dict[str, Any] = {
        "type": "tool_result",
        "tool_use_id": use_id,
        "content": [{"type": "text", "text": text}],
    }
    if is_error:
        block["is_error"] = True
    return {
        "type": "user",
        "parent_tool_use_id": parent,
        "message": {"role": "user", "content": [block]},
    }


def _thinking(msg: str) -> dict:
    """A thinking block as the transcript records it: no text (batch10231016 wrote the gate
    block only there)."""
    return {
        "type": "assistant",
        "parent_tool_use_id": None,
        "message": {
            "role": "assistant",
            "id": msg,
            "content": [{"type": "thinking", "thinking": "", "signature": "narration"}],
        },
    }


def _text(text: str, msg: str = "msg-text") -> dict:
    return {
        "type": "assistant",
        "parent_tool_use_id": None,
        "message": {"role": "assistant", "id": msg, "content": [{"type": "text", "text": text}]},
    }


def _after_reality_check() -> list[dict]:
    return [
        _prompt("modernize", "docs/examples/wordpress/collection.json --auto"),
        _use("u-resume", RESUME),
        _result("u-resume", STATUS),
    ]


def _gate_ran() -> list[dict]:
    return [*_after_reality_check(), _use("u-gate", GATE, msg="msg-gate"), _result("u-gate", BLOCK)]


def _gate_shown(msg: str = "msg-next") -> list[dict]:
    """The gate ran, and the orchestrator pasted its block into its own chat text."""
    return [*_gate_ran(), _text(f"{BLOCK}\nApproving automatically (`--auto`).", msg=msg)]


def _write(tmp_path: Path, records: list[dict], *, session_format: bool = False) -> str:
    """A transcript file: the headless stream-json shape (``parent_tool_use_id``) or the
    interactive session shape (``isSidechain``)."""
    lines = []
    for rec in records:
        rec = dict(rec)
        if session_format:
            rec["isSidechain"] = rec.pop("parent_tool_use_id") is not None
        lines.append(json.dumps(rec))
    path = tmp_path / "transcript.jsonl"
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def _event(
    tmp_path: Path,
    records: list[dict],
    command: str = SPLIT,
    *,
    call_msg: str | None = "msg-next",
    tool: str = "Bash",
    session: bool = False,
    **kw,
) -> dict:
    """The PreToolUse event of call ``u-call``; ``call_msg`` is the assistant message its
    tool_use is recorded in (``None``: not recorded yet)."""
    if call_msg is not None:
        records = [*records, _use("u-call", command, msg=call_msg, tool=tool)]
    data = {"command": command} if tool == "Bash" else {"prompt": command}
    event = {
        "hook_event_name": "PreToolUse",
        "tool_name": tool,
        "tool_input": data,
        "tool_use_id": "u-call",
        "transcript_path": _write(tmp_path, records, session_format=session),
    }
    event.update(kw)
    return event


@pytest.fixture
def run(run_hook):
    env = {**os.environ, "MODERNIZER_GATE_HOOK_WAIT_S": "0"}
    return lambda event: run_hook("require_gate_shown.py", event, env=env)


@pytest.mark.parametrize("session", [False, True])
def test_the_gate_batched_with_the_split_is_refused(tmp_path: Path, run, session: bool) -> None:
    # The PR 1358 and batch10222044 runs: gate, summary page and split in one message.
    res = run(_event(tmp_path, _gate_ran(), call_msg="msg-gate", session=session))
    assert res.blocked
    assert res.stderr.startswith("Blocked: ")
    assert "run the gate on its own first, then start schema design" in res.stderr
    assert f"--job-id {JOB} --db wordpress --gate" in res.stderr
    assert res.stderr.count("\n") == 1


@pytest.mark.passes_on_main  # what the hook must keep allowing
@pytest.mark.parametrize("session", [False, True])
def test_the_gate_quoted_in_chat_lets_schema_design_start(tmp_path, run, session) -> None:
    # pr1606, batch10271044: the gate alone, then its block pasted as chat text, then the
    # split. The text may share the call's message (it is recorded first) or come before it.
    records = [*_gate_shown("msg-next")]
    assert run(_event(tmp_path, records, session=session)).code == 0
    assert run(_event(tmp_path, _gate_shown("msg-quote"), session=session)).code == 0


@pytest.mark.parametrize("session", [False, True])
def test_the_gate_only_in_tool_output_is_refused(tmp_path, run, session) -> None:
    # pr1624, pr1624b (#1626): the gate's output, then thinking and the split in a new message,
    # with no chat text: the user never saw the block or its summary-page URL.
    records = [*_gate_ran(), _thinking("msg-next")]
    res = run(_event(tmp_path, records, session=session))
    assert res.blocked
    assert res.stderr.startswith("Blocked: schema design waits for the approval gate")
    assert "not in thinking, not inside a tool call" in res.stderr
    # Every missing line in one refusal, so one round fixes them all.
    assert "Missing from your chat text (3 line(s)):" in res.stderr
    for line in hook.gate_lines_to_show(BLOCK):
        assert f"\n- {line}\n" in res.stderr
    # The refusal carries the block itself, line for line: exactly what to paste.
    assert BLOCK.strip("\n") in res.stderr
    assert run(_event(tmp_path, _gate_ran(), session=session)).blocked  # no later text at all


def test_the_gate_written_only_in_thinking_is_refused(tmp_path: Path, run) -> None:
    # batch10231016: the model said it wrote the block, but every message was thinking and a
    # call. Thinking is not shown, so it never counts.
    records = [*_gate_ran()]
    for n in range(3):
        records += [_thinking(f"msg-t{n}"), _use(f"u-t{n}", SPLIT, msg=f"msg-t{n}")]
        records.append(_result(f"u-t{n}", "PreToolUse:Bash hook error: Blocked: ..."))
    assert run(_event(tmp_path, records)).blocked


@pytest.mark.parametrize(
    "missing",
    [
        f"- Summary page (the query flow and every query's engine): {URL}",
        HEADING,
        "| Engine | Queries before | Queries final | Share of final | Outcome |",
    ],
)
def test_a_quote_missing_a_key_line_is_refused(tmp_path: Path, run, missing: str) -> None:
    partial = BLOCK.replace(missing, "")
    res = run(_event(tmp_path, [*_gate_ran(), _text(partial, msg="msg-next")]))
    assert res.blocked
    assert "Missing from your chat text (1 line(s)):" in res.stderr
    assert f"\n- {missing}\n" in res.stderr


def test_a_summary_url_alone_is_not_the_gate(tmp_path: Path, run) -> None:
    # The URL quoted with no numbers: the user has the link but not the assignment.
    assert run(_event(tmp_path, [*_gate_ran(), _text(f"Summary: {URL}", msg="msg-next")])).blocked


@pytest.mark.passes_on_main  # what the hook must keep allowing
def test_the_quote_is_whitespace_insensitive(tmp_path: Path, run) -> None:
    reflowed = BLOCK.replace("| Engine | Queries", "|  Engine |  Queries").replace("\n\n", "\n")
    assert run(_event(tmp_path, [*_gate_ran(), _text(reflowed, msg="msg-next")])).code == 0


def test_text_only_in_a_tool_input_does_not_count(tmp_path: Path, run) -> None:
    # Writing the block into a file or an echo is not showing it to the user.
    write = _use("u-w", f"echo '{BLOCK}'", msg="msg-next")
    records = [*_gate_ran(), write, _result("u-w", BLOCK)]
    assert run(_event(tmp_path, records, call_msg="msg-later")).blocked


def test_a_quote_after_the_call_does_not_count(tmp_path: Path, run) -> None:
    # The call's own record ends the window: text written after it was not shown before it.
    records = [*_gate_ran(), _use("u-call", SPLIT, msg="msg-next"), _text(BLOCK, msg="msg-next")]
    event = _event(tmp_path, records, call_msg=None)
    assert run(event).blocked


def test_a_subagent_quote_does_not_count(tmp_path: Path, run) -> None:
    quoted = _text(BLOCK, msg="msg-sub")
    quoted["parent_tool_use_id"] = "u-agent"
    records = [*_gate_ran(), quoted]
    assert run(_event(tmp_path, records)).blocked
    assert run(_event(tmp_path, records, session=True)).blocked


def test_a_gate_whose_result_is_not_recorded_yet_is_refused(tmp_path: Path, run) -> None:
    records = [*_after_reality_check(), _use("u-gate", GATE, msg="msg-gate")]
    assert run(_event(tmp_path, records, call_msg="msg-gate")).blocked
    assert run(_event(tmp_path, records, call_msg=None)).blocked


@pytest.mark.passes_on_main  # existing behaviour, now with the gate quoted
def test_a_call_not_recorded_yet_counts_as_later_only_after_a_later_message(tmp_path, run) -> None:
    # The hook looks the call up by tool_use_id; until it is recorded, a later assistant message
    # after the gate's result is what shows the gate ran first.
    assert run(_event(tmp_path, _gate_shown(), call_msg=None)).code == 0
    assert run(_event(tmp_path, _gate_ran(), call_msg=None)).blocked
    event = _event(tmp_path, _gate_shown(), call_msg=None)
    event.pop("tool_use_id")
    assert run(event).code == 0


def test_messages_without_ids_are_told_apart_by_the_results_between_them(tmp_path, run) -> None:
    def no_ids(records: list[dict]) -> list[dict]:
        out: list[dict] = json.loads(json.dumps(records))
        for rec in out:
            rec.get("message", {}).pop("id", None)
        return out

    batched = [*_after_reality_check(), _use("u-gate", GATE), _use("u-call", SPLIT)]
    batched.append(_result("u-gate", BLOCK))
    assert run(_event(tmp_path, no_ids(batched), call_msg=None)).blocked
    later = [*_gate_shown(), _use("u-call", SPLIT)]
    assert run(_event(tmp_path, no_ids(later), call_msg=None)).code == 0
    unquoted = [*_gate_ran(), _use("u-call", SPLIT)]
    assert run(_event(tmp_path, no_ids(unquoted), call_msg=None)).blocked


@pytest.mark.passes_on_main  # existing behaviour, now with the gate quoted
def test_a_call_that_lands_in_the_transcript_a_moment_late_is_seen(tmp_path, run_hook) -> None:
    import threading

    event = _event(tmp_path, _gate_ran(), call_msg=None)
    path = Path(event["transcript_path"])

    def _append() -> None:
        # The quote and the call it precedes, both recorded a moment after the event.
        with path.open("a") as handle:
            handle.write(json.dumps(_text(BLOCK, msg="msg-next")) + "\n")
            handle.write(json.dumps(_use("u-call", SPLIT, msg="msg-next")) + "\n")

    timer = threading.Timer(0.6, _append)
    timer.start()
    try:
        env = {**os.environ, "MODERNIZER_GATE_HOOK_WAIT_S": "3"}
        assert run_hook("require_gate_shown.py", event, env=env).code == 0
    finally:
        timer.cancel()


def test_skipping_the_gate_in_a_modernize_run_is_refused(tmp_path: Path, run) -> None:
    # The batch10230234 run: reality check, then start_local_ui --page summary and the split;
    # print_summary.py --gate never ran.
    records = [
        *_after_reality_check(),
        _use("u-ui", f"uv run python scripts/start_local_ui.py --page summary --job-id {JOB}"),
        _result("u-ui", json.dumps({"status": "url", "ui_url": URL})),
        _text(f"The summary page: {URL}"),
    ]
    res = run(_event(tmp_path, records))
    assert res.blocked
    assert f"--job-id {JOB} --db wordpress --gate" in res.stderr


@pytest.mark.parametrize(
    "result",
    [
        _result("u-gate", '{"status": "error", "message": "No reality-check output"}'),
        _result("u-gate", BLOCK, is_error=True),
    ],
)
def test_a_failed_gate_is_no_gate(tmp_path: Path, run, result: dict) -> None:
    records = [*_after_reality_check(), _use("u-gate", GATE, msg="msg-gate"), result]
    assert run(_event(tmp_path, [*records, _thinking("msg-next")])).blocked


def test_a_chat_mode_gate_needs_its_block_but_no_url(tmp_path: Path, run) -> None:
    # Chat mode's block has no Summary page line: its heading and table must be in the chat,
    # and no URL is asked for.
    gate = GATE.replace("--mode both", "--mode chat")
    records = [*_after_reality_check(), _use("u-gate", gate, msg="msg-gate")]
    records.append(_result("u-gate", CHAT_BLOCK))
    assert run(_event(tmp_path, records, call_msg="msg-gate")).blocked
    res = run(_event(tmp_path, [*records, _thinking("msg-next")]))
    assert res.blocked
    assert "Summary page" not in res.stderr
    shown = [*records, _text(CHAT_BLOCK, msg="msg-next")]
    assert run(_event(tmp_path, shown)).code == 0


def test_a_subagent_gate_does_not_count(tmp_path: Path, run) -> None:
    records = [
        *_after_reality_check(),
        _use("u-gate", GATE, msg="msg-sub", parent="u-agent"),
        _result("u-gate", BLOCK, parent="u-agent"),
        _thinking("msg-next"),
    ]
    assert run(_event(tmp_path, records)).blocked
    assert run(_event(tmp_path, records, session=True)).blocked


def test_the_latest_gate_decides(tmp_path: Path, run) -> None:
    # A re-gate batched with the call: the earlier gate does not make up for it, and neither
    # does the earlier quote: the new block is shown on its own.
    regate = [_use("u-gate2", GATE, msg="msg-again"), _result("u-gate2", BLOCK)]
    records = [*_gate_shown("msg-quote"), *regate]
    assert run(_event(tmp_path, records, call_msg="msg-again")).blocked
    assert run(_event(tmp_path, records, call_msg="msg-later")).blocked
    quoted = [*records, _text(BLOCK, msg="msg-later")]
    assert run(_event(tmp_path, quoted, call_msg="msg-later")).code == 0


@pytest.mark.passes_on_main  # existing behaviour, now with the gate quoted
def test_the_gate_of_another_job_does_not_count(tmp_path: Path, run) -> None:
    other = RESUME.replace(JOB, "aaaa1111")
    records = [*_gate_shown(), _use("u-r2", other), _result("u-r2", STATUS)]
    assert run(_event(tmp_path, records, SPLIT.replace(JOB, "aaaa1111"))).blocked
    assert run(_event(tmp_path, records)).code == 0


@pytest.mark.parametrize(
    "command",
    [
        SPLIT.replace("--split", "--status"),
        SPLIT.replace("--split", "--merge"),
        DESIGN_AURORA,
        f"uv run python ./scripts/run_schema_design.py --job-id={JOB} --engine dynamodb --split",
    ],
)
def test_every_schema_design_call_is_held(tmp_path: Path, run, command) -> None:
    assert run(_event(tmp_path, _gate_ran(), command, call_msg="msg-gate")).blocked
    assert run(_event(tmp_path, _gate_ran(), command)).blocked
    assert run(_event(tmp_path, _gate_shown(), command)).code == 0


def test_a_design_schema_dispatch_is_held(tmp_path: Path, run) -> None:
    prompt = f"Run /design-schema-aurora-mysql for job_id={JOB} db=wordpress. Unattended."
    assert run(_event(tmp_path, _gate_ran(), prompt, call_msg="msg-gate", tool="Agent")).blocked
    assert run(_event(tmp_path, _gate_ran(), prompt, tool="Agent")).blocked
    assert run(_event(tmp_path, _gate_shown(), prompt, tool="Agent")).code == 0
    group = f"Follow /design-schema-dynamodb **Group draft task** for job_id={JOB} group=0."
    assert run(_event(tmp_path, _gate_ran(), group, call_msg="msg-gate", tool="Agent")).blocked
    for other in (
        f"Run /reality-check for job_id={JOB} db=wordpress. Unattended.",
        f"Review the PR. It changes how /design-schema-dynamodb runs for job_id={JOB}.",
    ):
        assert (
            run(_event(tmp_path, _gate_ran(), other, call_msg="msg-gate", tool="Agent")).code == 0
        )


@pytest.mark.parametrize("command", [SPLIT, DESIGN_AURORA])
def test_reality_check_then_design_schema_by_hand_is_not_held(
    tmp_path: Path, run, command: str
) -> None:
    # /reality-check on its own runs --resume-reality-check at top level; /design-schema next
    # in the same session must not be asked for /modernize's gate.
    records = [
        _prompt("modernize", "docs/examples/wordpress/collection.json"),
        _text("Done."),
        _prompt("reality-check", f"job_id={JOB}"),
        _use("u-resume", RESUME),
        _result("u-resume", STATUS),
        _prompt("design-schema-dynamodb", f"job_id={JOB}"),
    ]
    assert run(_event(tmp_path, records, command)).code == 0
    # No slash command recorded at all: not a /modernize run either.
    assert run(_event(tmp_path, records[3:5], command)).code == 0


# The reviewer's reproductions on PR 1402: commands that only mention the scripts.
_MENTIONS = [
    f'gh issue create --title x --body "Runs {RESUME} then {GATE} and {SPLIT}"',
    f"cat > /tmp/notes.md <<'EOF'\n{GATE}\n{RESUME}\nEOF",
    f"echo '{GATE}' && echo '{RESUME}'",
    f"git log --grep 'print_summary.py --gate --job-id {JOB}'",
]
_NOT_SCHEMA_DESIGN = [
    "git diff origin/main -- scripts/run_schema_design.py",
    "uv run ruff check scripts/run_schema_design.py",
    "uv run python scripts/run_schema_design.py --help",
    f"uv run python scripts/run_schema_design.py --job-id {JOB} -h",
    "uv run python scripts/run_schema_design.py --engine dynamodb --split",  # no job id
    f"uv run pytest tests/unit/scripts/test_run_schema_design.py -k {JOB}",
]


@pytest.mark.parametrize("mention", _MENTIONS)
@pytest.mark.parametrize("command", [*_NOT_SCHEMA_DESIGN, SPLIT])
def test_a_command_that_only_mentions_the_scripts_arms_nothing(
    tmp_path: Path, run, mention: str, command: str
) -> None:
    records = [
        _prompt("modernize"),
        _use("u-m", mention, msg="msg-m"),
        _result("u-m", f"{ISSUE_URL}\n"),
    ]
    assert run(_event(tmp_path, records, command, call_msg="msg-m")).code == 0


@pytest.mark.parametrize("command", _NOT_SCHEMA_DESIGN)
def test_commands_that_are_not_schema_design_are_never_held(tmp_path, run, command) -> None:
    assert run(_event(tmp_path, _gate_ran(), command, call_msg="msg-gate")).code == 0


@pytest.mark.parametrize(
    "command",
    [
        "uv run python scripts/print_summary.py --db wordpress --gate --mode both",
        "uv run python scripts/run_assessment.py --db wordpress --resume-reality-check",
    ],
)
def test_a_gate_or_resume_without_a_job_id_arms_nothing(tmp_path, run, command) -> None:
    records = [_prompt("modernize"), _use("u-x", command, msg="msg-x"), _result("u-x", BLOCK)]
    assert run(_event(tmp_path, records, call_msg="msg-x")).code == 0


@pytest.mark.parametrize(
    "command",
    [
        GATE,
        f"uv run python scripts/start_local_ui.py --page summary --job-id {JOB}",
        f"uv run python scripts/run_assessment.py --job-id {JOB} --db wordpress --decide-for-me",
        f"uv run python scripts/run_pe_review.py --job-id {JOB} --db wordpress",
    ],
)
def test_other_commands_are_never_held(tmp_path: Path, run, command: str) -> None:
    assert run(_event(tmp_path, _after_reality_check(), command)).code == 0


def test_schema_design_on_its_own_is_not_held(tmp_path: Path, run) -> None:
    records = [_prompt("design-schema-dynamodb", f"job_id={JOB}"), _text("Designing.")]
    assert run(_event(tmp_path, records)).code == 0


def test_calls_inside_a_subagent_are_not_held(tmp_path: Path, run) -> None:
    event = _event(tmp_path, _gate_ran(), call_msg="msg-gate", agent_id="a1", agent_type="x")
    assert run(event).code == 0


def test_a_missing_or_unreadable_transcript_allows(tmp_path: Path, run, run_hook) -> None:
    event = bash(SPLIT)
    assert run(event).code == 0
    event["transcript_path"] = str(tmp_path / "missing.jsonl")
    assert run(event).code == 0
    assert run_hook("require_gate_shown.py", "not json").code == 0
    (tmp_path / "bad.jsonl").write_text("not json\n{]\n")
    event["transcript_path"] = str(tmp_path / "bad.jsonl")
    assert run(event).code == 0


@pytest.mark.passes_on_main  # existing behaviour, now with the gate quoted
def test_the_transcript_is_read_one_record_at_a_time(tmp_path: Path) -> None:
    records = hook.iter_records(_write(tmp_path, [*_gate_shown(), _use("u-call", SPLIT)]))
    assert not isinstance(records, list)
    assert hook.gate_status(records, JOB, "u-call") == ("ran_before", "wordpress")


def test_the_summary_line_pattern_matches_the_transcript_check() -> None:
    # ci/llm/run.py imports third-party modules, which a hook must not: the hook carries a
    # copy of the pattern check_ui_pointers credits for the gate's summary-page URL.
    from ci.llm import run as llm_run

    assert hook.SUMMARY_LINE_RE.pattern == llm_run._PRINTED_SUMMARY_RE.pattern
    assert hook.SUMMARY_LINE_RE.flags == llm_run._PRINTED_SUMMARY_RE.flags


def test_the_printed_gate_block_carries_the_line_the_hook_requires() -> None:
    from src.report.printed_summary import render_gate_block

    text = render_gate_block({}, database_name="wordpress", job_id=JOB, summary_url=URL)
    assert hook.gate_url(text) == URL
    chat = render_gate_block({}, database_name="wordpress", job_id=JOB, summary_url=None)
    assert hook.gate_url(chat) is None


def test_the_gate_lines_match_the_transcript_check() -> None:
    # The hook carries copies of the lines check_printed_blocks_shown asks the chat for: the
    # two must agree, or the hook would refuse a run the check passes (or the reverse).
    from ci.llm import run as llm_run
    from src.report.printed_summary import render_gate_block

    assert hook.GATE_HEADING == llm_run._GATE_HEADING
    assert hook.GATE_SHOWN_LINE_RE.pattern == llm_run._GATE_SHOWN_LINE_RE.pattern
    blocks = [
        render_gate_block({}, database_name="wordpress", job_id=JOB, summary_url=URL),
        render_gate_block({}, database_name="wordpress", job_id=JOB, summary_url=None),
        BLOCK,
        CHAT_BLOCK,
        *(_fixture_gate_block(name) for name in _DISCOURSE_FIXTURES),
    ]
    for block in blocks:
        assert hook.gate_lines_to_show(block) == llm_run._gate_lines_to_show(block)
        assert hook.missing_lines(block, ["x"]) == llm_run._missing_lines(
            llm_run._gate_lines_to_show(block), ["x"]
        )


def test_an_error_inside_the_hook_allows(tmp_path: Path, run) -> None:
    # A record shape the hook does not expect (a tool_use whose input is a string) must not
    # wedge the session: any error fails open, and the transcript check stays the backstop.
    odd = _use("u-odd", "x", msg="msg-odd")
    odd["message"]["content"][0]["input"] = "not an object"
    res = run(_event(tmp_path, [*_gate_ran(), odd]))
    assert res.code == 0
    assert res.stderr == ""


_FIXTURES = Path(__file__).parent / "fixtures"
# The two scoped discourse e2e-llm runs (both mode) that failed check-transcript with "quoted
# the summary-page ui_url only after schema design started" (#1626): top-level records up to the
# first Phase 6 call, trimmed with ci/llm/replay.py's trim_transcript, the gate output kept.
_DISCOURSE_FIXTURES = ("gate_not_quoted_discourse_1.jsonl", "gate_not_quoted_discourse_2.jsonl")


def _fixture_records(name: str) -> list[dict]:
    path = _FIXTURES / name if "/" not in name else Path(name)
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _fixture_gate_block(name: str) -> str:
    records = _fixture_records(name)
    gate_ids = {
        block["id"]
        for rec in records
        for block in rec["message"]["content"]
        if block.get("type") == "tool_use" and "--gate" in (block["input"].get("command") or "")
    }
    return next(
        hook._result_text(block["content"])
        for rec in records
        for block in rec["message"]["content"]
        if block.get("type") == "tool_result" and block.get("tool_use_id") in gate_ids
    )


def _first_phase6(records: list[dict]) -> tuple[int, dict]:
    for i, rec in enumerate(records):
        if rec.get("type") != "assistant" or rec.get("parent_tool_use_id") is not None:
            continue
        for block in rec["message"]["content"]:
            if block.get("type") == "tool_use":
                event = {"tool_name": block["name"], "tool_input": block["input"]}
                if hook.phase6_job(event):
                    return i, block
    raise AssertionError("no Phase 6 call")


def _replay_event(tmp_path: Path, records: list[dict]) -> dict:
    """The PreToolUse event Claude Code sent for the run's first Phase 6 call, with the
    transcript as it stood then (every record up to and including that call)."""
    at, call = _first_phase6(records)
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(json.dumps(rec) + "\n" for rec in records[: at + 1]))
    return {
        "hook_event_name": "PreToolUse",
        "tool_name": call["name"],
        "tool_input": call["input"],
        "tool_use_id": call["id"],
        "transcript_path": str(path),
    }


@pytest.mark.parametrize("name", _DISCOURSE_FIXTURES)
def test_replaying_the_failed_discourse_runs_refuses_their_early_split(
    tmp_path: Path, run, name: str
) -> None:
    records = _fixture_records(name)
    block = _fixture_gate_block(name)
    url = hook.gate_url(block)
    assert url is not None  # the gate printed the summary-page URL: the UI was up
    res = run(_replay_event(tmp_path, records))
    assert res.blocked
    assert "the user has not seen it" in res.stderr
    assert url in res.stderr  # the refusal hands over the line to paste
    # Pasting the block as chat text, right before the same call, lets it run.
    at, _call = _first_phase6(records)
    shown = [*records[:at], _text(block, msg=records[at]["message"]["id"]), records[at]]
    assert run(_replay_event(tmp_path, shown)).code == 0


@pytest.mark.passes_on_main  # what the hook must keep allowing
def test_replaying_the_saved_wordpress_run_allows_its_split(tmp_path: Path, run) -> None:
    # tests/fixtures/real_runs/wordpress-both (pr1606) pasted the gate block before the split.
    saved = Path(__file__).parents[2] / "fixtures/real_runs/wordpress-both/transcript.jsonl"
    assert run(_replay_event(tmp_path, _fixture_records(str(saved)))).code == 0


def _interactive(records: list[dict], args: str) -> list[dict]:
    """``records`` with the ``/modernize`` prompt's arguments replaced by ``args``."""
    return [_prompt("modernize", args), *records[1:]]


def test_an_auto_run_is_told_to_paste_and_carry_on(tmp_path: Path, run) -> None:
    # --auto never asks the user: paste the block, then make the same call again.
    for args in ("docs/examples/wordpress/collection.json --auto", "c.json -y --mode both"):
        res = run(_event(tmp_path, [*_interactive(_gate_ran(), args), _thinking("msg-next")]))
        assert res.blocked
        assert "then make this call again after that text" in res.stderr
        assert "ask the approval question again" not in res.stderr


def test_a_run_that_asks_the_user_is_told_to_ask_for_approval_again(tmp_path, run) -> None:
    # #1627 review: a model that asked for approval without showing the block must not paste it
    # and carry straight on; the user approves only after seeing it.
    records = _interactive(_gate_ran(), "docs/examples/wordpress/collection.json --mode both")
    records += [_text("Approve this assignment?", msg="msg-ask"), _prompt("approve")]
    records[-1]["message"]["content"] = "approve"
    res = run(_event(tmp_path, records))
    assert res.blocked
    assert "ask the approval question again and end your turn" in res.stderr
    assert "this run is not --auto" in res.stderr
    assert "then make this call again after that text" not in res.stderr
    # Once the block is shown, the same run may go on (the user approved after seeing it).
    shown = [*records, _text(BLOCK, msg="msg-shown"), _prompt("approve")]
    shown[-1]["message"]["content"] = "approve"
    assert run(_event(tmp_path, shown)).code == 0


def test_the_mode_comes_from_the_latest_modernize_prompt(tmp_path: Path) -> None:
    # A plain-text prompt (a headless run's "-p" text) and the tagged form both count; with no
    # /modernize prompt recorded the run is treated as unattended (the wording it had before).
    plain = {
        "type": "user",
        "parent_tool_use_id": None,
        "message": {"role": "user", "content": "/modernize c.json --auto --mode both"},
    }
    asked = _interactive(_gate_ran(), "c.json")
    for records, auto in (
        ([plain, *_gate_ran()[1:]], True),
        (asked, False),
        ([*asked[:1], _prompt("modernize", "c.json --auto"), *asked[1:]], True),
        (_gate_ran()[1:], None),
    ):
        path = _write(tmp_path, [*records, _thinking("msg-next")])
        check = hook.gate_check(hook.iter_records(path), JOB)
        assert (check.status, check.auto) == ("not_quoted", auto)
