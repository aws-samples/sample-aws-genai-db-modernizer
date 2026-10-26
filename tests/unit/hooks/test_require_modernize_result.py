"""The Stop hook that keeps a /modernize --auto run from ending with nothing running (#1574).

Twice a scoped discourse run's orchestrator ended its turn after the last DynamoDB part
reported ("Still waiting on DynamoDB parts 27 and 44."), with nothing left running, so the
headless session ended with no merge, synthesis or MODERNIZE_RESULT line.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

DB, JOB = "discourse", "job1574"
SCRIPT = "require_modernize_result.py"
AUTO_PROMPT = (
    "<command-message>modernize</command-message>\n<command-name>/modernize</command-name>\n"
    "<command-args>input/discourse-collection.json --auto --mode both</command-args>"
)


def _rec(kind: str, text: str, **extra: object) -> dict:
    return {
        "type": kind,
        "message": {"role": kind, "content": [{"type": "text", "text": text}]},
        **extra,
    }


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".modernizer-state.json").write_text(
        json.dumps({"job_id": JOB, "database_name": DB, "current_phase": "schema_design"})
    )
    vdir = tmp_path / "artifacts" / DB / JOB / "schema-dynamodb" / "v1"
    vdir.mkdir(parents=True)
    (vdir / "groups_manifest.json").write_text(
        json.dumps({"groups": [{"group_index": 0}, {"group_index": 1}, {"group_index": 2}]})
    )
    for g in range(3):
        (vdir / f"schema_draft_group_{g}.json").write_text("{}")
    return tmp_path


def _transcript(project: Path, records: list[dict]) -> str:
    path = project / "session.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return str(path)


def _event(project: Path, records: list[dict] | None = None, **extra: object) -> dict:
    records = (
        records
        if records is not None
        else [
            _rec("user", AUTO_PROMPT),
            _rec("assistant", "Still waiting on DynamoDB parts 27 and 44."),
        ]
    )
    return {
        "hook_event_name": "Stop",
        "session_id": "s1",
        "cwd": str(project),
        "transcript_path": _transcript(project, records),
        "stop_hook_active": False,
        "last_assistant_message": "Still waiting on DynamoDB parts 27 and 44.",
        "background_tasks": [],
        **extra,
    }


def _blocked(res) -> bool:
    """Blocked by this hook: exit 2 with its own reason (a missing script also exits 2)."""
    return res.blocked and res.stderr.startswith("Blocked: nothing is running")


def _allowed(res) -> bool:
    """Allowed by this hook: it ran and exited 0 (a missing script exits 2)."""
    return res.code == 0


def _env(project: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "ARTIFACT_DIR"}
    env["CLAUDE_PROJECT_DIR"] = str(project)
    return env


def test_every_part_done_and_nothing_running_blocks_with_the_next_step(run_hook, project) -> None:
    res = run_hook(SCRIPT, _event(project), env=_env(project))
    assert _blocked(res), res.stderr
    assert "nothing is running" in res.stderr and "MODERNIZE_RESULT" in res.stderr
    assert "Every DynamoDB part has its draft (3 of 3)" in res.stderr
    assert f"--job-id {JOB} --db {DB} --engine dynamodb --status" in res.stderr
    assert "\n" not in res.stderr.strip()


def test_a_part_still_running_is_a_legitimate_wait(run_hook, project) -> None:
    tasks = [{"id": "a1", "type": "local_agent", "description": "DynamoDB design, part 3 of 3"}]
    assert _allowed(run_hook(SCRIPT, _event(project, background_tasks=tasks), env=_env(project)))


def test_without_the_tasks_field_a_missing_draft_is_a_wait(run_hook, project) -> None:
    (
        project / "artifacts" / DB / JOB / "schema-dynamodb" / "v1" / "schema_draft_group_2.json"
    ).unlink()
    event = _event(project)
    del event["background_tasks"]
    assert _allowed(run_hook(SCRIPT, event, env=_env(project)))


def test_without_the_tasks_field_every_draft_done_still_blocks(run_hook, project) -> None:
    event = _event(project)
    del event["background_tasks"]
    assert _blocked(run_hook(SCRIPT, event, env=_env(project)))


def test_with_nothing_running_and_no_result_it_blocks_whatever_the_phase(run_hook, project) -> None:
    (
        project / "artifacts" / DB / JOB / "schema-dynamodb" / "v1" / "schema_draft_group_2.json"
    ).unlink()
    res = run_hook(SCRIPT, _event(project), env=_env(project))
    assert _blocked(res)
    assert "Run the documented status command of the current phase" in res.stderr


def test_a_started_merge_or_review_names_no_dynamodb_step(run_hook, project) -> None:
    vdir = project / "artifacts" / DB / JOB / "schema-dynamodb" / "v1"
    (vdir / "merge_record.json").write_text("{}")
    res = run_hook(SCRIPT, _event(project), env=_env(project))
    assert _blocked(res) and "Every DynamoDB part" not in res.stderr
    (vdir / "merge_record.json").unlink()
    requests = project / "artifacts" / DB / JOB / "llm_requests"
    requests.mkdir()
    (requests / "pe_review_dynamodb_group_0.json").write_text("{}")
    res = run_hook(SCRIPT, _event(project), env=_env(project))
    assert _blocked(res) and "Every DynamoDB part" not in res.stderr


def test_a_subagent_session_is_never_blocked(run_hook, project) -> None:
    assert _allowed(run_hook(SCRIPT, _event(project, agent_id="a1"), env=_env(project)))
    stop = _event(project, hook_event_name="SubagentStop")
    assert _allowed(run_hook(SCRIPT, stop, env=_env(project)))
    sidechain = [_rec("user", AUTO_PROMPT, isSidechain=True)]
    assert _allowed(run_hook(SCRIPT, _event(project, sidechain), env=_env(project)))


def test_a_terminal_result_line_allows_the_stop(run_hook, project) -> None:
    done = "MODERNIZE_RESULT: complete job_id=job1574 db=discourse mode=both"
    event = _event(project, last_assistant_message=done)
    assert _allowed(run_hook(SCRIPT, event, env=_env(project)))
    records = [_rec("user", AUTO_PROMPT), _rec("assistant", f"Summary.\n\n{done}")]
    event = _event(project, records, last_assistant_message="")
    assert _allowed(run_hook(SCRIPT, event, env=_env(project)))
    failed = "MODERNIZE_RESULT: failed phase=schema_design_dynamodb reason=drafts missing"
    assert _allowed(
        run_hook(SCRIPT, _event(project, last_assistant_message=failed), env=_env(project))
    )
    # A result a subagent wrote is not the run's.
    records = [_rec("user", AUTO_PROMPT), _rec("assistant", done, parent_tool_use_id="t1")]
    assert _blocked(run_hook(SCRIPT, _event(project, records), env=_env(project)))


def test_an_interactive_or_other_session_is_never_blocked(run_hook, project) -> None:
    interactive = AUTO_PROMPT.replace(" --auto", "")
    for prompt in (interactive, "<command-name>/synthesize</command-name>", "Fix the bug"):
        records = [_rec("user", prompt), _rec("assistant", "Approve this assignment?")]
        assert _allowed(run_hook(SCRIPT, _event(project, records), env=_env(project))), prompt
    # A plain-text prompt with -y counts as --auto.
    records = [_rec("user", "/modernize x.json -y"), _rec("assistant", "Waiting.")]
    assert _blocked(run_hook(SCRIPT, _event(project, records), env=_env(project)))


@pytest.mark.parametrize(
    "broken",
    [
        "not json",
        "[]",
        json.dumps({"hook_event_name": "Stop", "background_tasks": []}),  # no transcript
        json.dumps({"hook_event_name": "Stop", "background_tasks": [], "transcript_path": "/nope"}),
    ],
)
def test_an_error_allows_the_stop(run_hook, project, broken: str) -> None:
    assert run_hook(SCRIPT, broken, env=_env(project)).code == 0


def test_no_state_file_or_a_bad_job_id_allows(run_hook, project) -> None:
    (project / ".modernizer-state.json").write_text(
        json.dumps({"job_id": "../x", "database_name": DB})
    )
    assert _allowed(run_hook(SCRIPT, _event(project), env=_env(project)))
    (project / ".modernizer-state.json").unlink()
    assert _allowed(run_hook(SCRIPT, _event(project), env=_env(project)))


def test_the_hook_reads_and_writes_nothing_outside_its_inputs(run_hook, project) -> None:
    event = _event(project)
    before = sorted(str(p) for p in project.rglob("*"))
    assert _blocked(run_hook(SCRIPT, event, env=_env(project)))
    assert sorted(str(p) for p in project.rglob("*")) == before


def test_the_session_log_shape_of_a_headless_slash_command_counts(run_hook, project) -> None:
    # The session log (transcript_path) of a real `claude -p "/modernize ... --auto"` run
    # records the prompt as one string with the command tags, not as text blocks.
    records = [
        {
            "parentUuid": None,
            "isSidechain": False,
            "type": "user",
            "message": {"role": "user", "content": AUTO_PROMPT},
        },
        _rec("assistant", "Still waiting on DynamoDB parts 27 and 44.", isSidechain=False),
    ]
    assert _blocked(run_hook(SCRIPT, _event(project, records), env=_env(project)))
