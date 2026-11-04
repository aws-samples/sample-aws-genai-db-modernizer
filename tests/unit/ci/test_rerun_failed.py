# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ci/rerun_failed.py and ci/rerun_failed.sh: re-run the last CI run's failed tests (#1849).

``gh`` is a fake on PATH that answers from files, so the tests need no network."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ci import rerun_failed as rf

ROOT = Path(__file__).resolve().parents[3]

LOG = (
    "test-shard (1/6)\tUNKNOWN STEP\t2026-11-04T16:48:47.0617681Z [gw1] [ 58%] FAILED "
    "tests/unit/a/test_x.py::test_one \n"
    "test-shard (1/6)\tUNKNOWN STEP\t2026-11-04T16:52:28.2057603Z FAILED "
    "tests/unit/a/test_x.py::test_one - AssertionError: no\n"
    "test-shard (2/6)\tUNKNOWN STEP\t2026-11-04T16:52:29Z FAILED "
    "tests/unit/b/test_y.py::test_p[a b-1] - assert 1 == 2\n"
    "test-shard (3/6)\tUNKNOWN STEP\t2026-11-04T16:52:30Z ERROR tests/unit/c/test_z.py - "
    "ImportError: no module named q\n"
    "lint\tLint\t2026-11-04T16:52:31Z Black...FAILED\n"
    "lint\tLint\t2026-11-04T16:52:31Z 3 FAILED, 10 passed\n"
)
PR = {
    "number": 77,
    "headRefName": "feat/x",
    "headRefOid": "abc",
    "commits": [{"oid": "old"}, {"oid": "abc"}],
}


def _run(status: str, conclusion: str | None, created: str, sha: str = "abc", rid: int = 1):
    return {
        "databaseId": rid,
        "status": status,
        "conclusion": conclusion,
        "createdAt": created,
        "headSha": sha,
        "url": f"https://example.com/runs/{rid}",
    }


FAKE_GH = """#!/bin/sh
d="$FAKE_GH"
echo "$*" >> "$d/calls"
case "$1 $2" in
  "auth status") exit "$(cat "$d/auth_rc" 2>/dev/null || echo 0)" ;;
  "pr view")
    if [ -f "$d/pr.json" ]; then cat "$d/pr.json"; else
      echo 'no pull requests found for branch "feat/x"' >&2; exit 1; fi ;;
  "run list") cat "$d/runs.json" ;;
  "run view")
    case " $* " in
      *" --log-failed "*) cat "$d/log.txt" ;;
      *) cat "$d/jobs.json" ;;
    esac ;;
esac
"""


@pytest.fixture
def gh(tmp_path, monkeypatch):
    """A fake gh first on PATH; returns its answer folder."""
    bin_dir, answers = tmp_path / "bin", tmp_path / "gh"
    bin_dir.mkdir()
    answers.mkdir()
    (bin_dir / "gh").write_text(FAKE_GH)
    (bin_dir / "gh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH", str(answers))
    return answers


def _answer(folder: Path, pr=PR, runs=(), log: str = LOG, jobs=(), auth_rc: int = 0) -> None:
    if pr is not None:
        (folder / "pr.json").write_text(json.dumps(pr))
    (folder / "runs.json").write_text(json.dumps(list(runs)))
    (folder / "log.txt").write_text(log)
    (folder / "jobs.json").write_text(json.dumps({"jobs": list(jobs)}))
    (folder / "auth_rc").write_text(str(auth_rc))


def _calls(folder: Path) -> list[str]:
    path = folder / "calls"
    return path.read_text().splitlines() if path.exists() else []


# ---- parsing and picking ----


def test_parse_failed_ids_reads_pytest_failed_and_error_lines_once_each():
    assert rf.parse_failed_ids(LOG) == [
        "tests/unit/a/test_x.py::test_one",
        "tests/unit/b/test_y.py::test_p[a b-1]",
        "tests/unit/c/test_z.py",
    ]


def test_parse_failed_ids_ignores_failed_words_that_are_not_test_ids():
    assert rf.parse_failed_ids("Black...FAILED\n3 FAILED, 1 passed\nFAILED src/x.py\n") == []
    assert rf.parse_failed_ids("") == []


def test_pick_run_takes_the_newest_completed_run():
    runs = [
        _run("in_progress", None, "2026-11-04T12:00:00Z", rid=3),
        _run("completed", "failure", "2026-11-04T11:00:00Z", rid=2),
        _run("completed", "success", "2026-11-04T10:00:00Z", rid=1),
    ]
    assert rf.pick_run(runs)["databaseId"] == 2
    assert rf.pick_run(list(reversed(runs)))["databaseId"] == 2
    assert rf.pick_run([runs[0]]) is None
    assert rf.pick_run([]) is None


# ---- ids and status through a fake gh ----


def test_ids_of_a_red_last_run(gh, capsys):
    _answer(gh, runs=[_run("completed", "failure", "2026-11-04T11:00:00Z", rid=9)])
    assert rf.main(["ids"]) == 0
    out = capsys.readouterr()
    assert out.out.splitlines() == rf.parse_failed_ids(LOG)
    assert "PR #77" in out.err and "3 test(s)" in out.err
    (listing,) = [c for c in _calls(gh) if c.startswith("run list")]
    for part in ("--workflow ci.yml", "--branch feat/x", "--event pull_request"):
        assert part in listing
    assert "run view 9 --log-failed" in _calls(gh)


def test_ids_take_the_pr_number_given(gh, capsys):
    _answer(gh, runs=[_run("completed", "failure", "2026-11-04T11:00:00Z")])
    assert rf.main(["ids", "#77"]) == 0
    assert any(c.startswith("pr view 77 ") for c in _calls(gh))
    assert rf.main(["ids", "main"]) == 2


def test_ids_with_no_pull_request_say_so_and_exit_0(gh, capsys):
    _answer(gh, pr=None)
    assert rf.main(["ids"]) == 0
    out = capsys.readouterr()
    assert out.out == ""
    assert "no pull request for this branch" in out.err


def test_ids_when_gh_is_not_authenticated_exit_2(gh, capsys):
    _answer(gh, auth_rc=1)
    assert rf.main(["ids"]) == 2
    assert "gh auth login" in capsys.readouterr().err
    assert not any(c.startswith("pr view") for c in _calls(gh))


def test_ids_without_gh_exit_2(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))
    assert rf.main(["ids"]) == 2
    assert "gh is not installed" in capsys.readouterr().err


def test_ids_after_a_green_last_run_rerun_nothing_even_if_an_older_run_was_red(gh, capsys):
    _answer(
        gh,
        runs=[
            _run("completed", "success", "2026-11-04T12:00:00Z", rid=2),
            _run("completed", "failure", "2026-11-04T11:00:00Z", rid=1),
        ],
    )
    assert rf.main(["ids"]) == 0
    out = capsys.readouterr()
    assert out.out == "" and "ended success: nothing to re-run" in out.err
    assert not any("--log-failed" in c for c in _calls(gh))


def test_ids_with_no_completed_run(gh, capsys):
    _answer(gh, runs=[_run("in_progress", None, "2026-11-04T12:00:00Z")])
    assert rf.main(["ids"]) == 0
    assert "no completed CI run" in capsys.readouterr().err


def test_ids_skip_a_run_in_progress_for_the_last_completed_one(gh, capsys):
    _answer(
        gh,
        runs=[
            _run("in_progress", None, "2026-11-04T12:00:00Z", rid=2),
            _run("completed", "failure", "2026-11-04T11:00:00Z", sha="old", rid=1),
        ],
    )
    assert rf.main(["ids"]) == 0
    out = capsys.readouterr()
    assert out.out.splitlines() == rf.parse_failed_ids(LOG)
    assert "for an older commit" in out.err


def test_ids_of_a_red_run_with_no_test_id_name_the_failed_jobs(gh, capsys):
    _answer(
        gh,
        runs=[_run("completed", "failure", "2026-11-04T11:00:00Z")],
        log="lint\tLint\tz Black...FAILED\n",
        jobs=[{"name": "lint", "conclusion": "failure"}, {"name": "test", "conclusion": "success"}],
    )
    assert rf.main(["ids"]) == 0
    out = capsys.readouterr()
    assert out.out == ""
    assert "no failed test id (failed jobs: lint)" in out.err


@pytest.mark.parametrize(
    ("answers", "first"),
    [
        ({"runs": [_run("completed", "failure", "2026-11-04T11:00:00Z")]}, "red 77 https://"),
        ({"runs": [_run("completed", "success", "2026-11-04T11:00:00Z")]}, "green 77 success"),
        ({"runs": [_run("completed", "timed_out", "2026-11-04T11:00:00Z")]}, "red 77 https://"),
        ({"runs": [_run("completed", "cancelled", "2026-11-04T11:00:00Z")]}, "other 77 cancelled"),
        (
            {"runs": [_run("completed", "failure", "2026-11-04T11:00:00Z", sha="old")]},
            "red 77 https://example.com/runs/1, for an older commit",
        ),
        ({"pr": None}, "none no pull request"),
        ({"runs": []}, "none PR #77 has no completed CI run"),
        ({"auth_rc": 1}, "unknown gh is not authenticated"),
    ],
)
def test_status_prints_one_line(gh, capsys, answers, first):
    _answer(gh, **answers)
    code = rf.main(["status"])
    line = capsys.readouterr().out
    assert line.startswith(first) and line.count("\n") == 1
    assert code == (2 if first.startswith("unknown") else 0)
    assert not any("--log-failed" in c for c in _calls(gh))  # status reads no logs


# ---- the shell entry point and the pre_merge step ----


def _sh(args: list[str], env_extra: dict[str, str] | None = None):
    env = {**os.environ, "UV_NO_SYNC": "1", **(env_extra or {})}
    return subprocess.run(
        ["bash", "ci/rerun_failed.sh", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_rerun_failed_sh_list_prints_the_ids(gh):
    _answer(gh, runs=[_run("completed", "failure", "2026-11-04T11:00:00Z")])
    result = _sh(["--list", "77"])
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == rf.parse_failed_ids(LOG)


def test_rerun_failed_sh_runs_the_unit_ids_and_prints_the_e2e_command(gh):
    passing = "tests/unit/ci/test_rerun_failed.py::test_pick_run_takes_the_newest_completed_run"
    log = f"x\ty\tz FAILED {passing} - boom\nx\ty\tz FAILED tests/e2e/test_pipeline.py::test_a\n"
    _answer(gh, runs=[_run("completed", "failure", "2026-11-04T11:00:00Z")], log=log)
    result = _sh([])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert "uv run pytest -p no:xdist -q tests/e2e/test_pipeline.py::test_a" in result.stdout


def test_rerun_failed_sh_fails_when_a_rerun_test_fails(gh):
    log = "x\ty\tz FAILED tests/unit/ci/test_rerun_failed.py::test_does_not_exist - boom\n"
    _answer(gh, runs=[_run("completed", "failure", "2026-11-04T11:00:00Z")], log=log)
    assert _sh([]).returncode != 0


def test_rerun_failed_sh_passes_on_nothing_to_rerun_and_exits_2_without_auth(gh):
    _answer(gh, pr=None)
    result = _sh([])
    assert result.returncode == 0
    assert "no pull request for this branch" in result.stderr
    _answer(gh, auth_rc=1)
    assert _sh([]).returncode == 2


FAKE_UV = """#!/bin/sh
if [ "$1 $2" = "run python" ]; then shift 2; exec "$REAL_PYTHON" "$@"; fi
printf '%s\\n' "$@" > "$UV_ARGV"
echo "HYPOTHESIS_PROFILE=${HYPOTHESIS_PROFILE:-}" >> "$UV_ARGV"
"""


@pytest.mark.parametrize(("given", "want"), [(None, "ci"), ("default", "default")])
def test_rerun_failed_sh_runs_pytest_as_a_pull_request_run_does(
    gh, tmp_path, monkeypatch, given, want
):
    """Round 1: the Hypothesis profile and the per-test limit of a pull request's ci/test.sh
    run, both from ci/lib.sh, so a property test re-runs as CI ran it."""
    _answer(gh, runs=[_run("completed", "failure", "2026-11-04T11:00:00Z")])
    (tmp_path / "bin" / "uv").write_text(FAKE_UV)
    (tmp_path / "bin" / "uv").chmod(0o755)
    argv_file = tmp_path / "argv"
    env = {"REAL_PYTHON": sys.executable, "UV_ARGV": str(argv_file)}
    if given:
        env["HYPOTHESIS_PROFILE"] = given
    else:
        monkeypatch.delenv("HYPOTHESIS_PROFILE", raising=False)
    result = _sh([], env)
    assert result.returncode == 0, result.stderr
    argv = argv_file.read_text().splitlines()
    assert argv[:2] == ["run", "pytest"]
    assert "--timeout=300" in argv and "--timeout-method=thread" in argv
    assert f"HYPOTHESIS_PROFILE={want}" in argv


def test_ci_test_sh_and_rerun_failed_sh_share_the_lib_helpers():
    lib = (ROOT / "ci" / "lib.sh").read_text(encoding="utf-8")
    assert "PYTEST_TIME_LIMIT=(--timeout=300 --timeout-method=thread)" in lib
    assert 'HYPOTHESIS_PROFILE="${HYPOTHESIS_PROFILE:-ci}"' in lib
    for script in ("test.sh", "rerun_failed.sh"):
        text = (ROOT / "ci" / script).read_text(encoding="utf-8")
        assert '"${PYTEST_TIME_LIMIT[@]}"' in text, script
        assert "use_pull_request_hypothesis_profile" in text, script
        assert "--timeout=300" not in text, script  # one copy, in lib.sh


def test_parse_failed_ids_reads_only_the_log_message_start():
    """Round 1: a captured-output line that mentions FAILED mid-message is no id."""
    log = (
        "test-shard (1/6)\tRun tests\t2026-11-04T16:52:28Z ok   main-tests: FAILED "
        "tests/unit/zz/test_x.py::t\n"
        "test-shard (1/6)\tRun tests\t2026-11-04T16:52:28Z     FAILED tests/unit/zz/test_y.py::t\n"
        "test-shard (1/6)\tRun tests\t2026-11-04T16:52:28Z FAILED tests/unit/a.py::t_ok - x\n"
    )
    assert rf.parse_failed_ids(log) == ["tests/unit/a.py::t_ok"]


def test_parse_failed_ids_keeps_nested_brackets_and_a_dash_inside_them():
    log = (
        "s\tt\t2026-11-04T16:52:28Z FAILED tests/unit/a.py::test_three[a[1]] - boom\n"
        "s\tt\t2026-11-04T16:52:28Z [gw0] [ 12%] FAILED tests/unit/a.py::test_d[x - y] \n"
        "s\tt\t2026-11-04T16:52:28Z FAILED tests/unit/a.py::test_n[a[1] b] - boom\n"
        "FAILED tests/unit/b.py::test_raw - plain pytest output, no log prefix\n"
        "s\tt\t2026-11-04T16:52:28Z FAILED tests/unit/a.py::test_open[a - x\n"
    )
    assert rf.parse_failed_ids(log) == [
        "tests/unit/a.py::test_three[a[1]]",
        "tests/unit/a.py::test_d[x - y]",
        "tests/unit/a.py::test_n[a[1] b]",
        "tests/unit/b.py::test_raw",
    ]


def test_runs_of_another_pull_request_on_the_same_branch_name_are_ignored(gh, capsys):
    """Round 1: a fork's `main` or `patch-1` shares its name with others; only runs of
    this pull request's commits count."""
    _answer(
        gh,
        runs=[
            _run("completed", "failure", "2026-11-04T12:00:00Z", sha="someone-else", rid=2),
            _run("completed", "success", "2026-11-04T11:00:00Z", sha="abc", rid=1),
        ],
    )
    assert rf.main(["ids"]) == 0
    assert "ended success" in capsys.readouterr().err
    assert any("commits" in c for c in _calls(gh) if c.startswith("pr view"))


def test_the_readme_names_the_gh_calls_pre_merge_makes():
    """Round 1: pre_merge reads the PR and its last CI run through gh, not just the fetch."""
    readme = (ROOT / "ci" / "README.md").read_text(encoding="utf-8")
    assert "needs network only for the fetch" not in readme
    assert "gh run list" in readme and "gh pr view" in readme


def _pre_merge_step() -> str:
    sh = (ROOT / "ci" / "pre_merge.sh").read_text(encoding="utf-8")
    return "# j3. " + sh.split("# j3. ")[1].split('log "pre_merge summary"')[0]


@pytest.mark.parametrize(
    ("answers", "want"),
    [
        (
            {"runs": [_run("completed", "failure", "2026-11-04T11:00:00Z")]},
            "warn ci-rerun: PR #77's last CI run failed (https://example.com/runs/1); before "
            "pushing again, re-run its failed tests: ./ci/rerun_failed.sh 77",
        ),
        (
            {"runs": [_run("completed", "success", "2026-11-04T11:00:00Z")]},
            "ok   ci-rerun: PR #77's last CI run ended success",
        ),
        (
            {"runs": [_run("completed", "failure", "2026-11-04T11:00:00Z", sha="old")]},
            "warn ci-rerun: PR #77's last CI run failed (https://example.com/runs/1, for an older "
            "commit); before pushing again, re-run its failed tests: ./ci/rerun_failed.sh 77",
        ),
        (
            {"runs": [_run("completed", "cancelled", "2026-11-04T11:00:00Z")]},
            "info ci-rerun: PR #77's last CI run ended cancelled; nothing to re-run",
        ),
        ({"pr": None}, "skip ci-rerun: no pull request for this branch"),
        ({"auth_rc": 1}, "skip ci-rerun: cannot read the last CI run (gh is not authenticated"),
    ],
)
def test_pre_merge_prints_the_rerun_command_only_when_the_last_run_was_red(gh, answers, want):
    _answer(gh, **answers)
    script = 'set -euo pipefail\nnote() { printf "%s\\n" "$*"; }\n' + _pre_merge_step()
    result = subprocess.run(
        ["bash", "-c", script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PATH": f"{Path(sys.executable).parent}{os.pathsep}{os.environ['PATH']}",
        },
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith(want), result.stdout
    assert "fail " not in _pre_merge_step()  # informational: never fails pre_merge
