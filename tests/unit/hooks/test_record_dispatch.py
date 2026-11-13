# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The hooks that record each subagent dispatch and report in the job's dispatch ledger
(#2215), so an unrecorded dispatch cannot end ``--wait-for-report`` early."""

from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path

import pytest

from scripts import run_schema_design
from scripts.hooks import dispatch_ledger

DB, JOB = "discourse", "job2215"
SESSION = "session-run-2215"
# The tool result of an async Agent launch, as the session log records it (2.1.x).
ASYNC = {"isAsync": True, "status": "async_launched", "agentId": "agent-part-1"}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".modernizer-state.json").write_text(
        json.dumps({"job_id": JOB, "database_name": DB, "session_id": SESSION})
    )
    (tmp_path / "artifacts" / DB / JOB).mkdir(parents=True)
    return tmp_path


def _job(project: Path) -> Path:
    return project / "artifacts" / DB / JOB


def _env(project: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "ARTIFACT_DIR"}
    env["CLAUDE_PROJECT_DIR"] = str(project)
    return env


def _post(response, tool: str = "Agent", **extra) -> dict:
    return {
        "hook_event_name": "PostToolUse",
        "session_id": SESSION,
        "tool_name": tool,
        "tool_input": {"description": "DynamoDB design, part 1 of 2", "prompt": "..."},
        "tool_response": response,
        **extra,
    }


def _outstanding(project: Path) -> list[str]:
    entries = dispatch_ledger.read(_job(project)) or {}
    return sorted(dispatch_ledger.outstanding(entries, SESSION))


@pytest.mark.parametrize(
    "response",
    [
        ASYNC,
        [
            {
                "type": "text",
                "text": "Async agent launched successfully.\nagentId: agent-part-1",
            }
        ],
        "Async agent launched successfully.\nagentId: agent-part-1 (internal ID)",
    ],
)
def test_an_agent_launch_is_recorded_as_dispatched(run_hook, project, response) -> None:
    res = run_hook("record_dispatch.py", _post(response), env=_env(project))
    assert res.code == 0 and res.stdout == res.stderr == ""
    assert _outstanding(project) == ["agent-part-1"]


def test_a_task_tool_launch_counts_too(run_hook, project) -> None:
    run_hook("record_dispatch.py", _post(ASYNC, tool="Task"), env=_env(project))
    assert _outstanding(project) == ["agent-part-1"]


def test_a_synchronous_agent_call_is_recorded_as_reported(run_hook, project) -> None:
    done = {**ASYNC, "isAsync": False, "status": "completed"}
    run_hook("record_dispatch.py", _post(done), env=_env(project))
    entries = dispatch_ledger.read(_job(project)) or {}
    assert list(entries) == ["agent-part-1"] and _outstanding(project) == []


def test_a_dispatch_with_no_job_cursor_is_a_no_op(run_hook, project) -> None:
    (project / ".modernizer-state.json").unlink()
    res = run_hook("record_dispatch.py", _post(ASYNC), env=_env(project))
    assert res.code == 0
    assert not (_job(project) / "_logs").exists()


@pytest.mark.parametrize(
    "state", [{"job_id": "../x", "database_name": DB}, {"job_id": "other", "database_name": DB}]
)
def test_a_bad_or_missing_job_folder_is_a_no_op(run_hook, project, state) -> None:
    (project / ".modernizer-state.json").write_text(json.dumps(state))
    assert run_hook("record_dispatch.py", _post(ASYNC), env=_env(project)).code == 0
    assert not (_job(project) / "_logs").exists()
    assert not (project / "artifacts" / DB / "other").exists()


@pytest.mark.parametrize(
    "event",
    [
        _post(ASYNC, tool="Bash"),
        _post(ASYNC, agent_id="sub1"),  # a subagent's own tool call
        _post({"status": "async_launched", "agentId": "bad id!"}),
        _post("no id here"),
    ],
)
def test_other_calls_write_nothing(run_hook, project, event) -> None:
    """Nothing in the ledger (an Agent result with no usable id is only logged)."""
    assert run_hook("record_dispatch.py", event, env=_env(project)).code == 0
    assert not (_job(project) / dispatch_ledger.LEDGER).exists()


@pytest.mark.parametrize("script", ["record_dispatch.py", "record_report.py"])
def test_a_broken_input_never_blocks(run_hook, project, script) -> None:
    assert run_hook(script, "not json", env=_env(project)).code == 0


def test_a_subagent_stop_records_the_report(run_hook, project) -> None:
    run_hook("record_dispatch.py", _post(ASYNC), env=_env(project))
    stop = {"hook_event_name": "SubagentStop", "session_id": SESSION, "agent_id": "agent-part-1"}
    assert run_hook("record_report.py", stop, env=_env(project)).code == 0
    assert _outstanding(project) == []


def test_a_subagent_stop_with_no_agent_id_writes_nothing(run_hook, project) -> None:
    run_hook("record_dispatch.py", _post(ASYNC), env=_env(project))
    run_hook(
        "record_report.py",
        {"hook_event_name": "SubagentStop", "session_id": SESSION},
        env=_env(project),
    )
    assert _outstanding(project) == ["agent-part-1"]


def _wait(project: Path) -> tuple[str, float]:
    slept, now = [], [0.0]

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += seconds

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        run_schema_design.run_wait_for_report(
            _job(project), 110, sleep=sleep, clock=lambda: now[0], session=SESSION
        )
    return json.loads(buf.getvalue().strip().splitlines()[-1])["status"], sum(slept)


def test_the_wait_blocks_after_a_hooked_dispatch_the_orchestrator_never_marked(
    run_hook, project
) -> None:
    """The ledger said nothing was outstanding (every earlier report recorded); the
    orchestrator then dispatched synthesis and recorded nothing. The hook did."""
    dispatch_ledger.mark_dispatched(_job(project), ["d1"], session=SESSION)
    dispatch_ledger.mark_reported(_job(project), ["d1"], session=SESSION)
    assert _wait(project) == ("nothing_pending", 0.0)
    run_hook("record_dispatch.py", _post({**ASYNC, "agentId": "synth1"}), env=_env(project))
    assert _wait(project) == ("no_change", 110.0)
    run_hook(
        "record_report.py",
        {"hook_event_name": "SubagentStop", "session_id": SESSION, "agent_id": "synth1"},
        env=_env(project),
    )
    assert _wait(project) == ("nothing_pending", 0.0)


@pytest.mark.parametrize(
    "state",
    [
        {"job_id": JOB, "database_name": DB, "session_id": "session-other"},
        {"job_id": JOB, "database_name": DB},  # a cursor with no session
    ],
)
def test_a_launch_from_a_session_not_running_the_job_is_a_no_op(run_hook, project, state) -> None:
    """A developer session, or another command, on the same clone: the cursor names
    another session (or none), so nothing is written into this job's ledger."""
    (project / ".modernizer-state.json").write_text(json.dumps(state))
    assert run_hook("record_dispatch.py", _post(ASYNC), env=_env(project)).code == 0
    assert not (_job(project) / "_logs").exists()


def test_each_event_and_an_unexpected_result_are_logged(run_hook, project) -> None:
    run_hook("record_dispatch.py", _post(ASYNC, tool_use_id="tu1"), env=_env(project))
    run_hook("record_dispatch.py", _post({"status": "odd"}, tool_use_id="tu2"), env=_env(project))
    log = (_job(project) / "_logs" / "dispatch_hooks.log").read_text()
    assert "received PostToolUse Agent tu1" in log and "dispatched agent-part-1" in log
    assert "received PostToolUse Agent tu2" in log and "no agent id in the Agent result" in log


def test_a_send_message_resume_reopens_its_subagent(run_hook, project) -> None:
    run_hook("record_dispatch.py", _post(ASYNC), env=_env(project))
    dispatch_ledger.mark_reported(_job(project), ["agent-part-1"], session=SESSION)
    assert _outstanding(project) == []
    resume = {
        "hook_event_name": "PostToolUse",
        "session_id": SESSION,
        "tool_name": "SendMessage",
        "tool_input": {"to": "agent-part-1", "message": "fix the draft"},
        "tool_response": "sent",
    }
    assert run_hook("record_dispatch.py", resume, env=_env(project)).code == 0
    assert _outstanding(project) == ["agent-part-1"]


def test_a_send_message_to_an_unknown_name_is_a_no_op(run_hook, project) -> None:
    event = {
        "hook_event_name": "PostToolUse",
        "session_id": SESSION,
        "tool_name": "SendMessage",
        "tool_input": {"to": "main", "message": "x"},
    }
    assert run_hook("record_dispatch.py", event, env=_env(project)).code == 0
    assert (dispatch_ledger.read(_job(project)) or {}) == {}


def test_eight_dispatch_hooks_at_once_lose_no_entry(project) -> None:
    """The reviewer's reproduction: 6b's second message launches the parts together, so
    eight hook processes write the ledger at the same moment."""
    import subprocess  # nosec B404 -- this repo's own hook, fixed argv
    import sys

    from tests.unit.hooks.conftest import HOOKS

    ids = [f"part-{i}" for i in range(8)]
    procs = [
        subprocess.Popen(  # nosec B603 -- fixed argv
            [sys.executable, "-I", str(HOOKS / "record_dispatch.py")],
            stdin=subprocess.PIPE,
            env=_env(project),
            text=True,
        )
        for _ in ids
    ]
    for proc, agent in zip(procs, ids, strict=True):
        proc.stdin.write(json.dumps(_post({**ASYNC, "agentId": agent})))  # type: ignore[union-attr]
    for proc in procs:
        proc.stdin.close()  # type: ignore[union-attr]
    assert all(proc.wait(timeout=30) == 0 for proc in procs)
    assert _outstanding(project) == sorted(ids)
