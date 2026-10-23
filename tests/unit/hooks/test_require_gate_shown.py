"""Hook 5: no schema design before the approval gate ran on its own (#1401)."""

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


@pytest.mark.parametrize("session", [False, True])
def test_the_gate_in_an_earlier_message_lets_schema_design_start(tmp_path, run, session) -> None:
    # batch10230505, batch10231016, pr1402: the gate alone, then the split in a later message.
    # Whether the model also retyped the block (or wrote it only in thinking) does not matter:
    # the gate's own output is what the user sees.
    records = [*_gate_ran(), _thinking("msg-next")]
    assert run(_event(tmp_path, records, session=session)).code == 0
    assert run(_event(tmp_path, _gate_ran(), session=session)).code == 0


def test_a_gate_whose_result_is_not_recorded_yet_is_refused(tmp_path: Path, run) -> None:
    records = [*_after_reality_check(), _use("u-gate", GATE, msg="msg-gate")]
    assert run(_event(tmp_path, records, call_msg="msg-gate")).blocked
    assert run(_event(tmp_path, records, call_msg=None)).blocked


def test_a_call_not_recorded_yet_counts_as_later_only_after_a_later_message(tmp_path, run) -> None:
    # The hook looks the call up by tool_use_id; until it is recorded, a later assistant message
    # after the gate's result is what shows the gate ran first.
    assert run(_event(tmp_path, [*_gate_ran(), _thinking("msg-next")], call_msg=None)).code == 0
    assert run(_event(tmp_path, _gate_ran(), call_msg=None)).blocked
    event = _event(tmp_path, [*_gate_ran(), _thinking("msg-next")], call_msg=None)
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
    later = [*_gate_ran(), _use("u-call", SPLIT)]
    assert run(_event(tmp_path, no_ids(later), call_msg=None)).code == 0


def test_a_call_that_lands_in_the_transcript_a_moment_late_is_seen(tmp_path, run_hook) -> None:
    import threading

    event = _event(tmp_path, _gate_ran(), call_msg=None)
    path = Path(event["transcript_path"])

    def _append() -> None:
        with path.open("a") as handle:
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


def test_a_chat_mode_gate_is_not_held(tmp_path: Path, run) -> None:
    # Only a block that carries the Summary page line keeps holding the job.
    gate = GATE.replace("--mode both", "--mode chat")
    records = [*_after_reality_check(), _use("u-gate", gate, msg="msg-gate")]
    records.append(_result("u-gate", CHAT_BLOCK))
    assert run(_event(tmp_path, records, call_msg="msg-gate")).code == 0


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
    # A re-gate batched with the call: the earlier gate does not make up for it.
    records = [*_gate_ran(), _use("u-gate2", GATE, msg="msg-next"), _result("u-gate2", BLOCK)]
    assert run(_event(tmp_path, records, call_msg="msg-next")).blocked
    assert run(_event(tmp_path, records, call_msg="msg-later")).code == 0


def test_the_gate_of_another_job_does_not_count(tmp_path: Path, run) -> None:
    other = RESUME.replace(JOB, "aaaa1111")
    records = [*_gate_ran(), _use("u-r2", other), _result("u-r2", STATUS)]
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
    assert run(_event(tmp_path, _gate_ran(), command)).code == 0


def test_a_design_schema_dispatch_is_held(tmp_path: Path, run) -> None:
    prompt = f"Run /design-schema-aurora-mysql for job_id={JOB} db=wordpress. Unattended."
    assert run(_event(tmp_path, _gate_ran(), prompt, call_msg="msg-gate", tool="Agent")).blocked
    assert run(_event(tmp_path, _gate_ran(), prompt, tool="Agent")).code == 0
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


def test_the_transcript_is_read_one_record_at_a_time(tmp_path: Path) -> None:
    records = hook.iter_records(_write(tmp_path, [*_gate_ran(), _use("u-call", SPLIT)]))
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
