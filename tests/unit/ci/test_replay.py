"""ci/llm/replay.py: keeping a passing real-model run and replaying it (#1409)."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from ci.llm import judge_facts, replay, run
from src.report.deliverables import FOLDER_FILENAMES

REPO = Path(__file__).resolve().parents[3]
FIXTURE = replay.DEFAULT_FIXTURE


def _records(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines()]


# ---------------------------------------------------------------------------
# trim_transcript
# ---------------------------------------------------------------------------

_CWD = "/runner/work/project"
# An access-key-shaped value, built at run time so no scanner reads a key here.
_FAKE_KEY = "AKIA" + "QWERTYUIOPASDFGH"


def _transcript() -> str:
    long_text = "x" * 3000
    lines = [
        {
            "type": "system",
            "subtype": "init",
            "cwd": _CWD,
            "session_id": "s",
            "memory_paths": {"auto": "/Users/alice/.claude/projects/-runner-work-project/m/"},
            "root_memory": "/root/.claude/projects/m/",
            "socket": "/tmp/cc-socks/4936.sock",
            "home": "/home/bob/notes.txt",
            "mac_tmp": "/var/folders/ab/cd1234/T/x.json",
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "thinking", "thinking": "private reasoning", "signature": "sig"},
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Write",
                        "input": {"file_path": f"{_CWD}/artifacts/a.json", "content": long_text},
                    },
                    {
                        "type": "tool_use",
                        "id": "t2",
                        "name": "Agent",
                        "input": {"prompt": long_text, "description": "design"},
                    },
                    {
                        "type": "tool_use",
                        "id": "t3",
                        "name": "Bash",
                        "input": {"command": f"uv run python scripts/x.py --root {_CWD}"},
                    },
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": long_text},
                    {"type": "tool_result", "tool_use_id": "t3", "content": '{"status": "ok"}'},
                ]
            },
            "tool_use_result": {"stdout": long_text},
        },
        {"type": "system", "subtype": "task_started", "output_file": "/tmp/-runner-work-project/o"},
    ]
    return "\n".join(json.dumps(r) for r in lines) + "\nnot json\n"


def test_the_runner_path_is_replaced_everywhere() -> None:
    out = replay.trim_transcript(_transcript())
    assert _CWD not in out
    assert "-runner-work-project" not in out
    assert _records(out.replace("not json\n", ""))[0]["cwd"] == replay.PLACEHOLDER_CWD
    assert f"--root {replay.PLACEHOLDER_CWD}" in out
    assert "/tmp/claude/-workspace-repo/o" in out


def test_home_temp_and_socket_paths_are_replaced_by_placeholders() -> None:
    out = replay.trim_transcript(_transcript())
    init = _records(out.replace("not json\n", ""))[0]
    assert init["memory_paths"]["auto"] == "/home/user/.claude/projects/-workspace-repo/m/"
    assert init["root_memory"] == "/home/user/.claude/projects/m/"
    assert init["socket"] == "/tmp/claude/agent.sock"
    assert init["home"] == "/home/user/notes.txt"
    assert init["mac_tmp"] == "/tmp/claude/x.json"
    assert replay.private_data(out) == []
    assert replay.scrub_paths("/tmp/claude-501/-x/out") == "/tmp/claude/-x/out"
    assert replay.scrub_paths("/private/tmp/a") == "/tmp/claude/a"
    assert replay.scrub_paths("/tmp/claude/a /tmp/claude") == "/tmp/claude/a /tmp/claude"


def test_private_data_names_paths_keys_and_accounts_but_not_ids() -> None:
    assert replay.private_data("/opt/runner/x and /builds/team/repo") == [
        "absolute path /opt/runner/x",
        "absolute path /builds/team/repo",
    ]
    assert replay.private_data(f"key {_FAKE_KEY}") == [f"aws access key {_FAKE_KEY}"]
    assert replay.private_data("account 123456789012 here") == ["aws account id 123456789012"]
    assert replay.private_data("arn:aws:iam::x") == ["aws arn arn:aws:"]
    # A uuid's tail, a long decimal, a localhost URL and the placeholders are not.
    assert (
        replay.private_data(
            "0a71e2ec-76cd-4e04-b68c-729457411851 1234.166666666664 "
            "http://localhost:3000/api/v1/x /workspace/repo/a /home/user/b /tmp/claude/c"
        )
        == []
    )


def test_long_results_and_written_files_are_cut_and_the_calls_kept() -> None:
    out = replay.trim_transcript(_transcript())
    assert out.endswith("not json\n")  # a line that is not JSON is kept as it was
    recs = _records(out.replace("not json\n", ""))
    thinking, write, agent, bash = recs[1]["message"]["content"]
    assert thinking == {"type": "thinking", "thinking": ""}
    assert write["input"]["file_path"].startswith(replay.PLACEHOLDER_CWD)
    assert "characters trimmed" in write["input"]["content"]
    assert len(write["input"]["content"]) < 2 * replay.TRIM_KEEP + 100
    assert agent["input"]["prompt"] == "x" * 3000  # the checks read dispatch prompts whole
    assert bash["input"]["command"].startswith("uv run python scripts/x.py")
    long_result, short_result = recs[2]["message"]["content"]
    assert "characters trimmed" in long_result["content"]
    assert short_result["content"] == '{"status": "ok"}'
    assert "characters trimmed" in recs[2]["tool_use_result"]["stdout"]


def test_a_short_value_is_never_cut() -> None:
    text = "y" * replay.TRIM_OVER
    assert replay._trim(text) == text
    assert replay._trim(text + "y").count("y") == 2 * replay.TRIM_KEEP


# ---------------------------------------------------------------------------
# save
# ---------------------------------------------------------------------------


def _e2e_llm_output(tmp_path: Path, passed: bool = True) -> Path:
    src = tmp_path / "llm-both-wordpress"
    synthesis = src / "deliverables" / "wordpress" / "synthesis"
    synthesis.mkdir(parents=True)
    (synthesis / "report.json").write_text('{"database_name": "wordpress"}')
    (synthesis / "llm_input.json").write_text("{}")
    (src / "results.json").write_text(
        json.dumps(
            {
                "pass": passed,
                "db": "wordpress",
                "job_id": "job1",
                "mode": "both",
                "fixture": "wordpress",
                "git_sha": "abc1234",
                "cost_usd": 3.9,
                "checks": {"transcript": True},
            }
        )
    )
    (src / "judge.json").write_text('{"mean": 3.3}')
    (src / "transcript.jsonl").write_text(_transcript())
    return src


def test_save_keeps_the_synthesis_files_the_trimmed_transcript_and_the_run(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "fixture"
    info = replay.save(_e2e_llm_output(tmp_path), dest)
    assert sorted(p.name for p in dest.iterdir()) == [
        "llm_input.json",
        "report.json",
        "run.json",
        "transcript.jsonl",
    ]
    assert info["db"] == "wordpress" and info["job_id"] == "job1" and info["mode"] == "both"
    assert info["judge_mean"] == 3.3
    assert json.loads((dest / "run.json").read_text()) == info
    assert _CWD not in (dest / "transcript.jsonl").read_text()


@pytest.mark.parametrize("leak", [_FAKE_KEY, "/opt/agent/work/x", "123456789012"])
def test_save_refuses_a_run_that_names_a_machine_or_an_account(tmp_path: Path, leak: str) -> None:
    src = _e2e_llm_output(tmp_path)
    (src / "deliverables" / "wordpress" / "synthesis" / "report.json").write_text(
        json.dumps({"database_name": "wordpress", "note": leak})
    )
    dest = tmp_path / "fixture"
    with pytest.raises(ValueError, match="names a machine or an account"):
        replay.save(src, dest)
    assert not dest.exists()  # nothing is written


def test_save_refuses_a_run_that_did_not_pass(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not pass"):
        replay.save(_e2e_llm_output(tmp_path, passed=False), tmp_path / "fixture")


# ---------------------------------------------------------------------------
# The saved fixture
# ---------------------------------------------------------------------------


def test_the_saved_transcript_passes_the_transcript_checks() -> None:
    """The current allowlist and transcript checks still accept the saved passing run."""
    saved = json.loads((FIXTURE / replay.RUN_FILE).read_text())
    summary = run.check_transcript(FIXTURE / replay.TRANSCRIPT_FILE, saved["mode"])
    assert (summary["db"], summary["job_id"], summary["mode"]) == (
        saved["db"],
        saved["job_id"],
        saved["mode"],
    )


def test_the_saved_run_names_no_machine_user_or_account() -> None:
    """Every record and field of every fixture file, not only the init record's cwd."""
    for path in sorted(FIXTURE.iterdir()):
        assert replay.private_data(path.read_text()) == [], path.name
    recs = [
        json.loads(line)
        for line in (FIXTURE / replay.TRANSCRIPT_FILE).read_text().splitlines()
        if line.strip()
    ]
    cwds = {r.get("cwd") for r in recs if r.get("type") == "system" and r.get("subtype") == "init"}
    assert cwds == {replay.PLACEHOLDER_CWD}


def test_prepare_lays_the_run_out_as_a_job_and_renders_it(tmp_path: Path) -> None:
    info = replay.prepare(FIXTURE, tmp_path)
    job = tmp_path / info["db"] / info["job_id"]
    assert (job / "synthesis" / "v1" / "report.json").is_file()
    # Every file of the deliverables folder (FOLDER_FILENAMES, the one list), the
    # detailed engineering report (#1364) included.
    names = sorted(p.name for p in (job / "deliverables").iterdir())
    assert names == sorted(FOLDER_FILENAMES)
    # The saved run predates the routing records: its detailed report says so.
    detailed = (job / "deliverables" / "engineering-report-detailed.md").read_text()
    assert "## Records not available" in detailed
    # A second prepare starts from a clean job, not on top of the first.
    (job / "stray.json").write_text("{}")
    replay.prepare(FIXTURE, tmp_path)
    assert not (job / "stray.json").exists()


def test_the_judge_facts_read_the_real_report() -> None:
    """Built from a real report.json, not a hand-written one (#1215: a field the
    writer stores as a count, read as a list)."""
    report = json.loads((FIXTURE / "report.json").read_text())
    llm_input = json.loads((FIXTURE / "llm_input.json").read_text())
    facts = judge_facts.build_facts(report, llm_input)
    totals = facts["totals"]
    assert totals["open_risks"] == report["facts"]["open_risk_count"]
    assert totals["migration_wave_count"] == report["facts"]["migration_wave_count"]
    assert facts["engines"]


# ---------------------------------------------------------------------------
# NEEDS_FULL_JOB
# ---------------------------------------------------------------------------


def test_every_check_left_out_of_the_replay_exists() -> None:
    """A renamed check would leave its --deselect pointing at nothing, and the
    renamed one would fail the replay; each entry names a real test function."""
    for node, reason in replay.NEEDS_FULL_JOB.items():
        path, _, name = node.partition("::")
        assert name.endswith("[external]"), node
        assert reason, node
        tree = ast.parse((REPO / path).read_text())
        functions = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert name.removesuffix("[external]") in functions, node


def test_deselect_args_pair_each_node_with_the_flag() -> None:
    args = replay.deselect_args()
    assert args[0::2] == ["--deselect"] * len(replay.NEEDS_FULL_JOB)
    assert args[1::2] == list(replay.NEEDS_FULL_JOB)
