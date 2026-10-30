"""ci/llm/replay.py: keeping a passing real-model run and replaying it (#1409)."""

from __future__ import annotations

import io
import json
import tarfile
from pathlib import Path

import pytest

from ci.llm import judge_facts, replay, run
from src.report.deliverables import FOLDER_FILENAMES

REPO = Path(__file__).resolve().parents[3]
FIXTURE = replay.DEFAULT_FIXTURE
REPO_ROOT = replay.REPO_ROOT


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
    assert replay.private_data("ok\n/opt/runner/x and /builds/team/repo") == [
        "absolute path at line 2",
        "absolute path at line 2",
    ]
    assert replay.private_data(f"key {_FAKE_KEY}") == ["aws access key at line 1"]
    assert replay.private_data("a\nb\naccount 123456789012 here") == ["aws account id at line 3"]
    assert replay.private_data("arn:aws:iam::x") == ["aws arn at line 1"]
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
    _write_job_archive(
        src / replay.JOB_ARCHIVE,
        {
            "assignment/v1/assignment.json": '{"v": 1}',
            "assignment/v2/assignment.json": '{"v": 2}',
            "collector/output.json": '{"tables": []}',
            "synthesis/v2/report.json": '{"database_name": "wordpress"}',
        },
    )
    return src


def _write_job_archive(path: Path, files: dict[str, str], prefix: str = "wordpress/job1/") -> None:
    with tarfile.open(path, "w:gz") as tar:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(prefix + name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def test_save_keeps_the_synthesis_files_the_trimmed_transcript_and_the_run(
    tmp_path: Path,
) -> None:
    dest = tmp_path / "fixture"
    info = replay.save(_e2e_llm_output(tmp_path), dest)
    assert sorted(p.name for p in dest.iterdir()) == [
        "job",
        "llm_input.json",
        "report.json",
        "run.json",
        "transcript.jsonl",
    ]
    # The job folder is kept except its synthesis folder (the top-level files), and
    # the version that synthesis folder had is recorded.
    assert sorted(
        p.relative_to(dest / "job").as_posix() for p in (dest / "job").rglob("*.json")
    ) == [
        "assignment/v1/assignment.json",
        "assignment/v2/assignment.json",
        "collector/output.json",
    ]
    assert info["synthesis_version"] == 2
    assert info["db"] == "wordpress" and info["job_id"] == "job1" and info["mode"] == "both"
    assert info["judge_mean"] == 3.3
    assert json.loads((dest / "run.json").read_text()) == info
    assert _CWD not in (dest / "transcript.jsonl").read_text()


@pytest.mark.parametrize(
    "leak",
    # A named id: test ids land in .test_durations and CI logs (#1718).
    [pytest.param(_FAKE_KEY, id="aws-access-key"), "/opt/agent/work/x", "123456789012"],
)
def test_save_refuses_a_run_that_names_a_machine_or_an_account(tmp_path: Path, leak: str) -> None:
    src = _e2e_llm_output(tmp_path)
    (src / "deliverables" / "wordpress" / "synthesis" / "report.json").write_text(
        json.dumps({"database_name": "wordpress", "note": leak})
    )
    dest = tmp_path / "fixture"
    with pytest.raises(ValueError, match="names a machine or an account"):
        replay.save(src, dest)
    assert not dest.exists()  # nothing is written


def test_save_refuses_a_run_without_the_full_job_upload(tmp_path: Path) -> None:
    src = _e2e_llm_output(tmp_path)
    (src / replay.JOB_ARCHIVE).unlink()
    dest = tmp_path / "fixture"
    with pytest.raises(ValueError, match="no job.tar.gz"):
        replay.save(src, dest)
    assert not dest.exists()


def test_save_refuses_a_job_file_that_names_a_machine(tmp_path: Path) -> None:
    src = _e2e_llm_output(tmp_path)
    _write_job_archive(
        src / replay.JOB_ARCHIVE, {"collector/output.json": '{"host": "/home/alice/db"}'}
    )
    dest = tmp_path / "fixture"
    with pytest.raises(ValueError, match="job/collector/output.json"):
        replay.save(src, dest)
    assert not dest.exists()


def test_save_refuses_an_archive_member_that_climbs_out_of_the_job(tmp_path: Path) -> None:
    src = _e2e_llm_output(tmp_path)
    _write_job_archive(src / replay.JOB_ARCHIVE, {"../../escape.json": "{}"})
    with pytest.raises(ValueError, match="unsafe member"):
        replay.save(src, tmp_path / "fixture")


def test_save_writes_a_job_file_over_the_repo_limit_compact(tmp_path: Path) -> None:
    src = _e2e_llm_output(tmp_path)
    big = json.dumps({"rows": ["x" * 20 for _ in range(30_000)]}, indent=4)
    assert len(big) > replay.MAX_FIXTURE_FILE_BYTES
    _write_job_archive(src / replay.JOB_ARCHIVE, {"collector/output.json": big})
    dest = tmp_path / "fixture"
    replay.save(src, dest)
    kept = (dest / "job" / "collector" / "output.json").read_text()
    assert len(kept.encode()) <= replay.MAX_FIXTURE_FILE_BYTES
    assert json.loads(kept) == json.loads(big)


def test_save_refuses_a_run_that_did_not_pass(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="does not pass"):
        replay.save(_e2e_llm_output(tmp_path, passed=False), tmp_path / "fixture")


# ---------------------------------------------------------------------------
# The saved fixture
# ---------------------------------------------------------------------------


def test_the_saved_transcript_passes_the_transcript_checks() -> None:
    """The current allowlist and transcript checks, the chat checks of #1605 included, still
    accept the saved passing run."""
    saved = json.loads((FIXTURE / replay.RUN_FILE).read_text())
    summary = run.check_transcript(FIXTURE / replay.TRANSCRIPT_FILE, saved["mode"])
    assert (summary["db"], summary["job_id"], summary["mode"]) == (
        saved["db"],
        saved["job_id"],
        saved["mode"],
    )
    records = run.iter_jsonl(FIXTURE / replay.TRANSCRIPT_FILE)
    assert run.check_printed_blocks_shown(records, saved["mode"]) == (True, None)


def _without_chat_quotes(records: list[dict]) -> list[dict]:
    """The saved run as a pre-#1605 orchestrator wrote it: the gate block and the final summary
    only in their tool output, the chat text pointing at "the block above"."""
    out: list[dict] = json.loads(json.dumps(records))
    for rec in out:
        if rec.get("type") != "assistant" or rec.get("parent_tool_use_id") is not None:
            continue
        for block in rec["message"]["content"]:
            text = block.get("text") if block.get("type") == "text" else None
            if isinstance(text, str) and "## Assignment for approval" in text:
                block["text"] = "The block above is the approval gate. Approve this assignment?"
            elif isinstance(text, str) and "## Assessment:" in text:
                tail = text[text.find("MODERNIZE_RESULT") :] if "MODERNIZE_RESULT" in text else ""
                block["text"] = "The summary above is the assessment.\n" + tail
    return out


def test_the_saved_run_without_its_chat_quotes_fails(tmp_path: Path) -> None:
    """The #1605 regression: the same run with the blocks only in tool output fails."""
    saved = json.loads((FIXTURE / replay.RUN_FILE).read_text())
    records = _without_chat_quotes(run.iter_jsonl(FIXTURE / replay.TRANSCRIPT_FILE))
    ok, reason = run.check_printed_blocks_shown(records, saved["mode"])
    assert not ok and "approval-gate block never appeared" in str(reason)
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(json.dumps(r) + "\n" for r in records))
    with pytest.raises(run.TranscriptError):
        run.check_transcript(path, saved["mode"])


@pytest.mark.passes_on_main  # guards that no replay-only skip of the chat checks comes back
def test_replay_runs_every_transcript_check() -> None:
    script = (REPO_ROOT / "ci" / "replay.sh").read_text()
    assert "--recorded-before-chat-quote" not in script
    assert 'ci/llm/run.py check-transcript "$FIXTURE/transcript.jsonl" --mode "$MODE"' in script


@pytest.mark.passes_on_main  # guards that the fixture names no machine; it now also covers job/
def test_the_saved_run_names_no_machine_user_or_account() -> None:
    """Every record and field of every fixture file, not only the init record's cwd."""
    for path in sorted(p for p in FIXTURE.rglob("*") if p.is_file()):
        assert replay.private_data(path.read_text()) == [], path.relative_to(FIXTURE).as_posix()
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
    run_info = json.loads((FIXTURE / "run.json").read_text())
    version = run_info["synthesis_version"]
    assert (job / "synthesis" / f"v{version}" / "report.json").is_file()
    # The saved job folder (#1463): the artifacts the external checks read.
    for name in ("collector/output.json", "referee-triage/triage.json"):
        assert (job / name).is_file(), name
    assert list(job.glob("assignment/v*/assignment.json"))
    assert list(job.glob("schema-*/v*/schema_output.json"))
    # Every file of the deliverables folder (FOLDER_FILENAMES, the one list), the
    # detailed engineering report (#1364) included.
    names = sorted(p.name for p in (job / "deliverables").iterdir())
    assert names == sorted(FOLDER_FILENAMES)
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
# the full job (#1463)
# ---------------------------------------------------------------------------


def test_prepare_refuses_a_fixture_without_a_saved_job(tmp_path: Path) -> None:
    old = tmp_path / "old"
    old.mkdir()
    for name in (*replay.SYNTHESIS_FILES, replay.RUN_FILE):
        (old / name).write_text((FIXTURE / name).read_text())
    with pytest.raises(RuntimeError, match="no job/ folder"):
        replay.prepare(old, tmp_path / "root")


def test_no_external_check_is_left_out_of_the_replay() -> None:
    """Every external-mode check runs (the old NEEDS_FULL_JOB deselect list is gone)."""
    script = (REPO_ROOT / "ci" / "replay.sh").read_text()
    assert "--deselect" not in script
    assert not hasattr(replay, "NEEDS_FULL_JOB")


@pytest.mark.parametrize(
    "leak",
    [
        "postgres://admin:hunter2pw@db.example.org/wp",  # pragma: allowlist secret
        '"password": "hunter2pw"',  # pragma: allowlist secret
        "db.abc.us-east-1.rds.amazonaws.com",
        "10.12.34.56",
        "dba@customer-corp.com",
        "bucket-123456789012-logs",
        "C:\\\\Users\\\\jdoe\\\\work",
    ],
)
def test_save_refuses_a_job_file_with_a_credential_host_or_person(
    tmp_path: Path, leak: str
) -> None:
    src = _e2e_llm_output(tmp_path)
    _write_job_archive(src / replay.JOB_ARCHIVE, {"collector/output.json": f"[1,\n{leak}\n]"})
    dest = tmp_path / "fixture"
    with pytest.raises(ValueError, match="names a machine or an account") as exc:
        replay.save(src, dest)
    assert "job/collector/output.json" in str(exc.value)
    assert not dest.exists()


def test_save_refuses_an_archive_member_over_the_size_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(replay, "MAX_ARCHIVE_MEMBER_BYTES", 100)
    src = _e2e_llm_output(tmp_path)
    _write_job_archive(src / replay.JOB_ARCHIVE, {"collector/output.json": "[" + "1," * 200 + "1]"})
    with pytest.raises(ValueError, match="is over 100 bytes"):
        replay.save(src, tmp_path / "fixture")


def test_the_fixture_says_where_its_job_came_from() -> None:
    """The judge, checks and cost in run.json belong to the transcript's run; the job
    folder may come from another (#1463), and run.json must say so."""
    run_info = json.loads((FIXTURE / "run.json").read_text())
    assert run_info["job_source"] in ("e2e-llm", "research run (not e2e-llm)")
    if run_info["job_source"] != "e2e-llm":
        assert run_info["job_relabelled_from"] and run_info["job_timestamp"]
        assert "mixed" in (REPO_ROOT / "ci" / "README.md").read_text()


@pytest.mark.parametrize(
    "leak",
    [
        "/opt/agent/work/x",
        pytest.param(_FAKE_KEY, id="aws-access-key"),
        "123456789012",
        "arn:aws:iam::x",
        "dba@customer-corp.com",
        "db.abc.us-east-1.rds.amazonaws.com",
        "10.12.34.56",
        "bucket-123456789012-logs",
    ],
)
def test_a_finding_names_its_kind_and_line_never_the_value(leak: str) -> None:
    findings = replay.job_private_data(f"one\ntwo\n{leak}\n")
    assert findings
    assert all(f.endswith("at line 3") for f in findings), findings
    for f in findings:
        assert leak not in f and leak.split("/")[-1] not in f
