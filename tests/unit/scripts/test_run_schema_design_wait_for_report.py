# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""``run_schema_design.py --wait-for-report`` (#2175).

A headless ``/modernize --auto`` run lost its result line: its orchestrator ended its
turn while DynamoDB part 1 was still running, and 600 s after that last turn Claude Code
stopped the part and ended the session. Headless, the orchestrator now runs this bounded
wait instead of ending the turn: it returns as soon as a design or review lands, and
otherwise after at most 110 seconds, with one line.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts import run_schema_design

DB, JOB = "wordpress", "job2175"


def _job(root: Path) -> Path:
    vdir = root / DB / JOB / "schema-dynamodb" / "v2"
    vdir.mkdir(parents=True)
    (vdir / "schema_draft_group_1.json").write_text("{}")
    (root / DB / JOB / "schema-elasticache" / "v2").mkdir(parents=True)
    return root / DB / JOB


def _run(monkeypatch, capsys, root: Path, *extra: str):
    argv = ["run_schema_design.py", "--job-id", JOB, "--db", DB, "--artifact-root", str(root)]
    monkeypatch.setattr(sys, "argv", [*argv, *extra])
    code = 0
    try:
        run_schema_design.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    return code, lines


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now


def test_it_returns_as_soon_as_a_part_draft_lands(monkeypatch, capsys, tmp_path):
    job = _job(tmp_path)
    clock = _Clock()

    def sleep(seconds: float) -> None:
        clock.sleeps.append(seconds)
        clock.now += seconds
        if len(clock.sleeps) == 3:  # part 1 writes its draft during the third poll
            (job / "schema-dynamodb" / "v2" / "schema_draft_group_0.json").write_text("{}")

    monkeypatch.setattr("time.sleep", sleep)
    monkeypatch.setattr("time.monotonic", clock)
    code, lines = _run(monkeypatch, capsys, tmp_path, "--wait-for-report", "110")

    assert code == 0 and len(lines) == 1, lines  # one compact line, never one per poll
    line = json.loads(lines[0])
    assert line["status"] == "landed"
    assert line["landed"] == ["schema-dynamodb/v2/schema_draft_group_0.json"]
    assert line["waited_s"] == 6.0 and len(clock.sleeps) == 3


@pytest.mark.parametrize(
    ("relative", "content", "mark"),
    [
        (
            "schema-elasticache/v2/schema_output.json",
            {},
            "schema-elasticache/v2/schema_output.json",
        ),
        (
            "schema-elasticache/v2/design_trace.json",
            {"pe_review": {"verdict": "APPROVED"}},
            "schema-elasticache/v2/design_trace.json#pe_review",
        ),
    ],
)
def test_a_finalized_design_or_a_recorded_review_lands(
    monkeypatch, capsys, tmp_path, relative, content, mark
):
    job = _job(tmp_path)
    clock = _Clock()

    def sleep(seconds: float) -> None:
        clock.now += seconds
        (job / relative).write_text(json.dumps(content))

    monkeypatch.setattr("time.sleep", sleep)
    monkeypatch.setattr("time.monotonic", clock)
    _, lines = _run(monkeypatch, capsys, tmp_path, "--wait-for-report", "110")
    assert json.loads(lines[0])["landed"] == [mark]


def test_an_edit_or_a_trace_with_no_review_never_ends_the_wait(monkeypatch, capsys, tmp_path):
    """A part edits its draft after --check-costs, and a design writes its trace before
    any review: neither is a report, so the wait runs to its bound."""
    job = _job(tmp_path)
    clock = _Clock()

    def sleep(seconds: float) -> None:
        clock.sleeps.append(seconds)
        clock.now += seconds
        (job / "schema-dynamodb" / "v2" / "schema_draft_group_1.json").write_text('{"x": 1}')
        (job / "schema-elasticache" / "v2" / "design_trace.json").write_text("{}")

    monkeypatch.setattr("time.sleep", sleep)
    monkeypatch.setattr("time.monotonic", clock)
    code, lines = _run(monkeypatch, capsys, tmp_path, "--wait-for-report", "30")
    line = json.loads(lines[0])
    assert code == 0 and line["status"] == "no_change" and "landed" not in line
    assert line["waited_s"] == 30.0 and sum(clock.sleeps) == 30.0


def test_the_wait_is_capped_under_the_bash_tool_timeout(monkeypatch, capsys, tmp_path):
    _job(tmp_path)
    clock = _Clock()

    def sleep(seconds: float) -> None:
        clock.now += seconds

    monkeypatch.setattr("time.sleep", sleep)
    monkeypatch.setattr("time.monotonic", clock)
    _, lines = _run(monkeypatch, capsys, tmp_path, "--wait-for-report", "900")
    assert json.loads(lines[0])["waited_s"] == 110.0


@pytest.mark.parametrize(
    "extra",
    [
        ("--wait-for-report", "0"),
        ("--wait-for-report", "-5"),
        ("--wait-for-report", "10", "--status", "--engine", "dynamodb"),
        ("--wait-for-report", "10", "--merge", "--engine", "dynamodb"),
        ("--wait-for-report", "10", "--engine", "dynamodb"),
    ],
)
def test_a_bad_wait_is_refused_without_waiting(monkeypatch, capsys, tmp_path, extra):
    _job(tmp_path)
    monkeypatch.setattr("time.sleep", lambda _s: pytest.fail("slept"))
    code, lines = _run(monkeypatch, capsys, tmp_path, *extra)
    assert code != 0 and json.loads(lines[-1])["status"] == "error"


def test_a_job_with_no_artifacts_is_refused(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr("time.sleep", lambda _s: pytest.fail("slept"))
    code, lines = _run(monkeypatch, capsys, tmp_path, "--wait-for-report", "10")
    assert code != 0 and "no artifacts" in json.loads(lines[-1])["message"]


@pytest.mark.passes_on_main
def test_every_other_mode_still_needs_an_engine(monkeypatch, capsys, tmp_path):
    _job(tmp_path)
    code, _ = _run(monkeypatch, capsys, tmp_path, "--status")
    assert code != 0


# ---------------------------------------------------------------------------
# #2215: the wait reads the dispatch ledger. A saved discourse run with one redesign round
# called it again after the last report had landed (its line said to run it again), and
# each such call blocked the whole 110 s before printing no_change: about 225 s on the
# critical path. Artifacts cannot say a subagent is idle (a design between two --finalize
# attempts, a retry not started, a synthesis just dispatched all look idle), so the
# orchestrator records each dispatch and report, and only an empty ledger ends the wait.
# ---------------------------------------------------------------------------

from scripts.hooks import dispatch_ledger  # noqa: E402

NOW = 1_800_000_000.0
SESSION = "session-run-2215"


@pytest.fixture(autouse=True)
def _this_session(monkeypatch):
    """Every command here runs in the session that runs the job."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SESSION)


def _ledger(
    job: Path, dispatched=(), reported=(), at: float = NOW - 60, session: str = SESSION
) -> None:
    if dispatched:
        dispatch_ledger.mark_dispatched(job, list(dispatched), at, session=session)
    if reported:
        dispatch_ledger.mark_reported(job, list(reported), at + 1, session=session)


def _fake_time(monkeypatch, on_sleep=None) -> _Clock:
    clock = _Clock()

    def sleep(seconds: float) -> None:
        clock.sleeps.append(seconds)
        clock.now += seconds
        if on_sleep:
            on_sleep(len(clock.sleeps))

    monkeypatch.setattr("time.sleep", sleep)
    monkeypatch.setattr("time.monotonic", clock)
    monkeypatch.setattr("time.time", lambda: NOW)
    return clock


def _wait(monkeypatch, capsys, root: Path, *extra: str) -> dict:
    code, lines = _run(monkeypatch, capsys, root, "--wait-for-report", "110", *extra)
    assert code == 0 and len(lines) == 1, lines
    line: dict = json.loads(lines[0])
    return line


def _says_wait_again(line: dict) -> bool:
    return "run this again" in line["next"]


def test_the_last_report_already_in_returns_at_once(monkeypatch, capsys, tmp_path):
    job = _job(tmp_path)
    _ledger(job, dispatched=["part1", "part2", "docdb"], reported=["part1", "part2", "docdb"])
    clock = _fake_time(monkeypatch)
    line = _wait(monkeypatch, capsys, tmp_path)
    assert line["status"] == "nothing_pending" and line["outstanding"] == []
    assert line["waited_s"] == 0.0 and clock.sleeps == []
    assert f"scripts/run_pe_review.py --job-id {JOB} --db {DB}`" in line["next"]
    assert "not this wait" in line["next"] and not _says_wait_again(line)


def test_one_still_running_keeps_the_wait(monkeypatch, capsys, tmp_path):
    job = _job(tmp_path)
    _ledger(job, dispatched=["part1", "docdb"], reported=["part1"])
    clock = _fake_time(monkeypatch)
    line = _wait(monkeypatch, capsys, tmp_path)
    assert line["status"] == "no_change" and line["waited_s"] == 110.0
    assert sum(clock.sleeps) == 110.0
    assert line["outstanding"] == ["docdb"] and _says_wait_again(line)


def _everything_else_done(job: Path) -> None:
    """Every other design and review is in and the DynamoDB merge is due: the artifacts
    alone would call the run idle."""
    vdir = job / "schema-dynamodb" / "v2"
    for g in (0, 1):
        (vdir / f"design_trace_group_{g}.json").write_text('{"pe_review": {}}')
    (job / "schema-elasticache" / "v2" / "schema_output.json").write_text("{}")
    (job / "schema-elasticache" / "v2" / "design_trace.json").write_text('{"pe_review": {}}')


@pytest.mark.parametrize(
    "running",
    [
        "Aurora PostgreSQL design between two --finalize attempts",
        "a retry dispatched that has not run its prepare step",
    ],
)
def test_a_design_the_artifacts_call_idle_keeps_the_wait(monkeypatch, capsys, tmp_path, running):
    job = _job(tmp_path)
    _everything_else_done(job)
    _ledger(job, dispatched=["part1", "part2", "pg"], reported=["part1", "part2"])
    _fake_time(monkeypatch)
    line = _wait(monkeypatch, capsys, tmp_path)
    assert line["status"] == "no_change" and line["waited_s"] == 110.0, running
    assert line["outstanding"] == ["pg"]


def test_a_wave_round_synthesis_is_outstanding_until_it_reports(monkeypatch, capsys, tmp_path):
    """7b: the round's design and review are recorded and synthesis was dispatched again.
    The wait must not send the run to a Phase 6 check (and from there into 6e's reroute)
    while synthesis runs: synthesis is outstanding until its report is recorded."""
    job = _job(tmp_path)
    _everything_else_done(job)
    (job / "wave-redesign" / "v2").mkdir(parents=True)
    (job / "wave-redesign" / "v2" / "rounds.json").write_text("{}")
    _ledger(job, dispatched=["pg-round", "pg-review", "synth"], reported=["pg-round", "pg-review"])
    _fake_time(monkeypatch)
    line = _wait(monkeypatch, capsys, tmp_path)
    assert line["status"] == "no_change" and line["outstanding"] == ["synth"]
    assert "run_pe_review" not in line["next"] and "post_schema" not in line["next"]
    _run(monkeypatch, capsys, tmp_path, "--mark-reported", "synth")
    line = _wait(monkeypatch, capsys, tmp_path)
    assert line["status"] == "nothing_pending"
    assert "Phase 7 and 7b after synthesis: run_wave_redesign.py" in line["next"]


def test_a_redone_dynamodb_part_keeps_the_wait_until_its_report(monkeypatch, capsys, tmp_path):
    job = _job(tmp_path)
    _ledger(job, dispatched=["part1", "part2"], reported=["part1", "part2"])
    dispatch_ledger.mark_dispatched(job, ["part2-redo"], NOW, session=SESSION)

    def land(polls: int) -> None:
        if polls == 2:
            (job / "schema-dynamodb" / "v2" / "schema_draft_group_0.json").write_text("{}")

    _fake_time(monkeypatch, land)
    line = _wait(monkeypatch, capsys, tmp_path)
    assert line["status"] == "landed" and line["outstanding"] == ["part2-redo"]
    assert "--mark-reported" in line["next"] and _says_wait_again(line)
    _run(monkeypatch, capsys, tmp_path, "--mark-reported", "part2-redo")
    assert _wait(monkeypatch, capsys, tmp_path)["status"] == "nothing_pending"


def test_a_merge_fix_running_waits_its_whole_bound(monkeypatch, capsys, tmp_path):
    """A merge-fix subagent edits drafts and adds no mark: while it is outstanding the
    wait runs its bound, as on main."""
    job = _job(tmp_path)
    _ledger(job, dispatched=["part1", "part2", "fix1"], reported=["part1", "part2"])

    def edit(polls: int) -> None:
        (job / "schema-dynamodb" / "v2" / "schema_draft_group_1.json").write_text('{"x": 1}')

    _fake_time(monkeypatch, edit)
    line = _wait(monkeypatch, capsys, tmp_path)
    assert line["status"] == "no_change" and line["waited_s"] == 110.0
    assert line["outstanding"] == ["fix1"]


def test_a_stale_review_request_does_not_hold_an_empty_ledger(monkeypatch, capsys, tmp_path):
    """The artifacts no longer decide: a review request older than its design (a round
    before's) changes nothing once every subagent has reported."""
    job = _job(tmp_path)
    (job / "llm_requests").mkdir()
    (job / "llm_requests" / "pe_review_elasticache.json").write_text('{"assignment_version": 2}')
    _ledger(job, dispatched=["ec"], reported=["ec"])
    _fake_time(monkeypatch)
    assert _wait(monkeypatch, capsys, tmp_path)["status"] == "nothing_pending"


@pytest.mark.passes_on_main  # main had no ledger and always waited its bound
@pytest.mark.parametrize("content", [None, "not json", '{"subagents": []}'])
def test_a_missing_or_unreadable_ledger_keeps_the_bounded_wait(
    monkeypatch, capsys, tmp_path, content
):
    job = _job(tmp_path)
    if content is not None:
        (job / "_logs").mkdir()
        (job / dispatch_ledger.LEDGER).write_text(content)
    clock = _fake_time(monkeypatch)
    _, lines = _run(monkeypatch, capsys, tmp_path, "--wait-for-report", "110")
    line = json.loads(lines[0])
    assert line["status"] == "no_change" and sum(clock.sleeps) == 110.0


def test_another_sessions_old_entry_is_ignored(monkeypatch, capsys, tmp_path):
    """A session that crashed two hours ago left a dispatch with no report: it never holds
    this session's wait, and it is never called hung here."""
    job = _job(tmp_path)
    _ledger(job, dispatched=["old1"], at=NOW - 2 * 3600, session="session-crashed")
    _ledger(job, dispatched=["part1"], reported=["part1"])
    clock = _fake_time(monkeypatch)
    line = _wait(monkeypatch, capsys, tmp_path)
    assert line["status"] == "nothing_pending" and clock.sleeps == []
    assert "old1" not in json.dumps(line)


def test_an_unreported_entry_of_this_session_is_never_called_hung(monkeypatch, capsys, tmp_path):
    """Two hours out, no report, and nothing says it is running (only the Stop hook sees
    the running tasks): the wait keeps its bounded wait and calls nothing hung."""
    job = _job(tmp_path)
    _ledger(job, dispatched=["docdb"], at=NOW - 2 * 3600)
    _fake_time(monkeypatch)
    line = _wait(monkeypatch, capsys, tmp_path)
    assert line["status"] == "no_change" and line["waited_s"] == 110.0
    assert line["outstanding"] == ["docdb"]
    assert "hung" not in line and "TaskStop" not in line["next"]


@pytest.mark.passes_on_main  # main had no ledger and always waited its bound
def test_with_no_known_session_the_wait_keeps_its_bound(monkeypatch, capsys, tmp_path):
    job = _job(tmp_path)
    _ledger(job, dispatched=["part1"], reported=["part1"])
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))  # no job cursor there
    clock = _fake_time(monkeypatch)
    _, lines = _run(monkeypatch, capsys, tmp_path, "--wait-for-report", "110")
    line = json.loads(lines[0])
    assert line["status"] == "no_change" and sum(clock.sleeps) == 110.0


def test_with_no_session_in_the_environment_the_cursors_session_is_used(
    monkeypatch, capsys, tmp_path
):
    job = _job(tmp_path)
    _ledger(job, dispatched=["part1"], reported=["part1"])
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(tmp_path))
    (tmp_path / ".modernizer-state.json").write_text(json.dumps({"session_id": SESSION}))
    _fake_time(monkeypatch)
    assert _wait(monkeypatch, capsys, tmp_path)["status"] == "nothing_pending"


def _parallel_mark(args: tuple[str, str]) -> None:
    job, agent = args
    dispatch_ledger.mark_dispatched(Path(job), [agent], session=SESSION)


@pytest.mark.parametrize("writers", [8, 16])
def test_parallel_writers_lose_no_entry(tmp_path, writers):
    """6b's second message launches the remaining parts together, so the dispatch hooks
    write at the same moment: every entry must survive."""
    import multiprocessing

    job = _job(tmp_path)
    ids = [f"part{i}" for i in range(writers)]
    ctx = multiprocessing.get_context("spawn")
    with ctx.Pool(writers) as pool:
        pool.map(_parallel_mark, [(str(job), i) for i in ids])
    entries = dispatch_ledger.read(job) or {}
    assert sorted(dispatch_ledger.outstanding(entries, SESSION)) == sorted(ids)
    assert not list((job / "_logs").glob("*.tmp"))


def test_marks_are_recorded_in_any_order_and_atomically(monkeypatch, capsys, tmp_path):
    job = _job(tmp_path)
    monkeypatch.setattr("time.sleep", lambda _s: pytest.fail("slept"))
    code, lines = _run(monkeypatch, capsys, tmp_path, "--mark-reported", "b2")  # report first
    assert code == 0 and json.loads(lines[0]) == {"status": "recorded", "outstanding": []}
    code, lines = _run(monkeypatch, capsys, tmp_path, "--mark-dispatched", "a1", "b2", "c3")
    assert json.loads(lines[0])["outstanding"] == ["a1", "c3"]  # b2 stays settled
    code, lines = _run(monkeypatch, capsys, tmp_path, "--mark-reported", "a1", "c3")
    assert json.loads(lines[0])["outstanding"] == []
    assert sorted(p.name for p in (job / "_logs").iterdir()) == [
        "dispatch_ledger.json",
        "dispatch_ledger.lock",
    ]
    entries = dispatch_ledger.read(job) or {}
    assert {e["session"] for e in entries.values()} == {SESSION}


@pytest.mark.parametrize(
    "extra",
    [
        ("--mark-dispatched", "../etc"),
        ("--mark-reported", "a b"),
        ("--mark-dispatched", "a1", "--engine", "dynamodb"),
        ("--mark-reported", "a1", "--status"),
    ],
)
def test_a_bad_mark_is_refused(monkeypatch, capsys, tmp_path, extra):
    job = _job(tmp_path)
    code, lines = _run(monkeypatch, capsys, tmp_path, *extra)
    assert code != 0 and json.loads(lines[-1])["status"] == "error"
    assert not (job / dispatch_ledger.LEDGER).exists()
