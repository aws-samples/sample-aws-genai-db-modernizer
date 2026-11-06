# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ci/hunk_revert.py: revert each changed hunk, report the ones no test notices (#1852).

The end-to-end tests build a scratch git repository with a ``main`` branch and a
feature branch, run the real script against it in a subprocess (no network, no model),
and read its report.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from ci import hunk_revert
from ci.hunk_revert import (
    Hunk,
    Runner,
    main,
    parse_hunk_headers,
    parse_name_status_z,
    parser,
    revert,
    skip_reason,
    split_lines,
    sweep,
)

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "ci" / "hunk_revert.py"
PREFIX = "hunk-revert-"


def _env(**extra: str) -> dict[str, str]:
    """Scratch repositories never read the user's git config (hooks, signing, aliases);
    ``TMPDIR`` is the test's own folder (the fixture below)."""
    return {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1", **extra}


@pytest.fixture(autouse=True)
def own_tmpdir(tmp_path, monkeypatch):
    """Every run's temporary folders go under this test's folder, and the test fails if
    any is left there, or if a new one appears in the real temporary folder."""
    real = Path(tempfile.gettempdir())
    before = set(real.glob(PREFIX + "*"))
    tmp = tmp_path / "tmp"
    tmp.mkdir()
    monkeypatch.setenv("TMPDIR", str(tmp))
    monkeypatch.setattr(tempfile, "tempdir", str(tmp))
    yield tmp
    assert list(tmp.glob(PREFIX + "*")) == [], "a hunk-revert folder was left behind"
    assert set(real.glob(PREFIX + "*")) <= before, "a hunk-revert folder escaped TMPDIR"


BASE_CALC = '''"""Calculator."""


def add(a, b):
    return a + b


def scale(x):
    # Double it.
    return x * 2


def mul(a, b):
    return a * b


def shout(s):
    """Say it."""
    return s.upper()


def pad(s):
    return s + "!"
'''

# Five hunks: add (guarded), scale's comment (skip), mul (unguarded), shout's
# docstring (guarded: a docstring is behaviour), pad's trailing whitespace (skip).
HEAD_CALC = '''"""Calculator."""


def add(a, b):
    return a + b + 1


def scale(x):
    # Double the value.
    return x * 2


def mul(a, b):
    return a * b * 1 + 0


def shout(s):
    """Say it loudly."""
    return s.upper()


def pad(s):
    return s + "!"\x20\x20
'''

BASE_HELPER = "def greet(name):\n    return 'hi ' + name\n"
HEAD_HELPER = "def greet(name):\n    return 'hello ' + name\n"

TEST_CALC = """from scripts.helper import greet
from src.calc import add, shout


def test_add():
    assert add(1, 1) == 3


def test_shout_documents_itself():
    assert shout.__doc__ == 'Say it loudly.'


def test_greet():
    assert greet('a') == 'hello a'
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, env=_env(), capture_output=True, text=True, check=True
    ).stdout


def _write(repo: Path, rel: str, text: str) -> None:
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _scratch(tmp_path: Path, base: dict[str, str], head: dict[str, str]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    for rel, text in {"src/__init__.py": "", "scripts/__init__.py": "", **base}.items():
        _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feat")
    for rel, text in head.items():
        _write(repo, rel, text)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "feat")
    return repo


def _calc_repo(tmp_path: Path) -> Path:
    return _scratch(
        tmp_path,
        {"src/calc.py": BASE_CALC, "scripts/helper.py": BASE_HELPER, "docs/x.md": "a\n"},
        {
            "src/calc.py": HEAD_CALC,
            "scripts/helper.py": HEAD_HELPER,
            "docs/x.md": "b\n",
            "tests/test_calc.py": TEST_CALC,
        },
    )


def _run(repo: Path, *args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--base", "main", *args],
        cwd=repo,
        env=_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _lines(out: str, prefix: str) -> list[str]:
    return [line.split()[1] for line in out.splitlines() if line.startswith(prefix)]


def _worktrees(repo: Path) -> list[str]:
    return [
        line
        for line in _git(repo, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]


# ---- end to end, on a scratch repository ------------------------------------------


def test_the_report_names_exactly_the_unguarded_hunk(tmp_path):
    repo = _calc_repo(tmp_path)
    proc = _run(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    assert _lines(out, "unnoticed ") == ["src/calc.py:14"], out
    assert sorted(_lines(out, "noticed ")) == [
        "scripts/helper.py:2",
        "src/calc.py:18",
        "src/calc.py:5",
    ], out
    # docs/ is outside src/ and scripts/: never listed.
    assert "docs/x.md" not in out
    assert "summary: 4 checked: 3 noticed, 1 unnoticed, 0 timed out; 2 skipped" in out


def test_python_comment_and_whitespace_hunks_are_skipped_and_reported(tmp_path):
    """The docstring hunk (line 18) is not skipped: it runs, and its test notices it."""
    out = _run(_calc_repo(tmp_path)).stdout
    skipped = {line.split()[1]: line for line in out.splitlines() if line.startswith("skip ")}
    assert set(skipped) == {"src/calc.py:9", "src/calc.py:23"}, out
    assert "comment or formatting only" in skipped["src/calc.py:9"]
    assert "whitespace only" in skipped["src/calc.py:23"]
    assert "noticed src/calc.py:18" in out


def test_strict_fails_when_a_hunk_goes_unnoticed(tmp_path):
    repo = _calc_repo(tmp_path)
    assert _run(repo, "--strict").returncode == 1


def test_strict_passes_when_every_hunk_is_noticed(tmp_path):
    repo = _scratch(
        tmp_path,
        {"src/calc.py": BASE_CALC, "scripts/helper.py": BASE_HELPER},
        {
            "src/calc.py": BASE_CALC.replace("a + b", "a + b + 1"),
            "tests/test_calc.py": TEST_CALC.replace("hello", "hi").replace(" loudly", ""),
        },
    )
    proc = _run(repo, "--strict")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "summary: 1 checked: 1 noticed, 0 unnoticed" in proc.stdout


def test_the_users_working_tree_is_untouched_and_the_worktrees_are_removed(tmp_path):
    repo = _calc_repo(tmp_path)
    _write(repo, "src/calc.py", HEAD_CALC + "\n# an uncommitted edit\n")
    proc = _run(repo, "--jobs", "2")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert (repo / "src/calc.py").read_text() == HEAD_CALC + "\n# an uncommitted edit\n"
    assert _git(repo, "status", "--porcelain") == " M src/calc.py\n"
    assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "feat"
    assert len(_worktrees(repo)) == 1
    roots = [
        line.split(" ", 1)[1] for line in proc.stdout.splitlines() if line.startswith("worktrees: ")
    ]
    assert roots and not Path(roots[0]).exists()
    assert "uncommitted edits are not checked" in proc.stdout


def test_parallel_jobs_give_the_same_report(tmp_path):
    repo = _calc_repo(tmp_path)
    out = _run(repo, "--jobs", "3").stdout
    assert _lines(out, "unnoticed ") == ["src/calc.py:14"], out
    assert sorted(_lines(out, "noticed ")) == [
        "scripts/helper.py:2",
        "src/calc.py:18",
        "src/calc.py:5",
    ]


def test_progress_lines_count_the_runs(tmp_path):
    out = _run(_calc_repo(tmp_path)).stdout
    progress = [line for line in out.splitlines() if line.startswith("[")]
    assert [p.split("]")[0] for p in progress] == ["[1/4", "[2/4", "[3/4", "[4/4"], out


def test_the_hunk_cap_leaves_the_rest_unchecked_and_says_so(tmp_path):
    out = _run(_calc_repo(tmp_path), "--max-hunks", "1").stdout
    assert "summary: 1 checked:" in out
    assert "3 over the cap (--max-hunks 1)" in out
    assert len([line for line in out.splitlines() if line.startswith("capped ")]) == 3


# The reverted code never returns: it blocks until its process group is killed, so its
# run is over any limit whatever the machine load (#1999).
BLOCKING = "import threading\n\n\ndef slow():\n    threading.Event().wait()\n    return 1\n"
UNBLOCKED = "import threading\n\n\ndef slow():\n    return 1\n"
TEST_SLOW = "from src.slow import slow\n\n\ndef test_slow():\n    assert slow() == 1\n"
# The reverted runs' limit: far under pytest's start-up, so they are always over it.
TINY_TIMEOUT = 0.05


@pytest.mark.passes_on_main  # #1999: the behaviour is main's; the old test was load-bound
def test_a_run_over_the_timeout_is_reported_as_timed_out_not_unnoticed(
    tmp_path, monkeypatch, saved_signals, capsys
):
    """The baseline run on HEAD keeps --timeout (generous: it only has to start pytest),
    the reverted runs get a tiny one; the test reads the reported state, never a clock."""
    repo = _scratch(
        tmp_path,
        {"src/slow.py": BLOCKING},
        {"src/slow.py": UNBLOCKED, "tests/test_slow.py": TEST_SLOW},
    )
    pgids: list[tuple[int, str | None]] = []
    check = Runner.check

    def tiny_limit(self, item):
        self.timeout = TINY_TIMEOUT
        try:
            return check(self, item)
        finally:
            pgids.extend(hunk_revert._recorded(self.root / "pgids"))

    monkeypatch.setattr(Runner, "check", tiny_limit)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.chdir(repo)
    assert main(["--base", "main", "--timeout", "600"]) == 0
    out = capsys.readouterr().out
    assert _lines(out, "timeout ") == ["src/slow.py:5"], out
    assert _lines(out, "unnoticed ") == [], out
    assert "summary: 1 checked: 0 noticed, 0 unnoticed, 1 timed out" in out, out
    # Both runs were recorded (baseline, then the reverted one), and the reverted run,
    # which would block for ever, is gone: it was killed at the limit.
    assert len(pgids) == 2, pgids
    assert all(_gone(pid, start) for pid, start in pgids), pgids
    # A timed-out hunk is undecided, never unnoticed: --strict does not fail on it.
    assert main(["--base", "main", "--timeout", "600", "--strict"]) == 0
    assert _lines(capsys.readouterr().out, "timeout ") == ["src/slow.py:5"]


def test_no_changed_test_means_every_hunk_goes_unnoticed_without_a_run(tmp_path):
    repo = _scratch(tmp_path, {"src/calc.py": BASE_CALC}, {"src/calc.py": HEAD_CALC})
    proc = _run(repo, "--strict")
    assert proc.returncode == 1
    assert sorted(_lines(proc.stdout, "unnoticed ")) == [
        "src/calc.py:14",
        "src/calc.py:18",
        "src/calc.py:5",
    ]
    assert "no new or changed test" in proc.stdout
    assert "worktrees: " not in proc.stdout


def test_a_baseline_that_fails_on_the_branch_stops_with_exit_2(tmp_path):
    bad = TEST_CALC.replace("== 3", "== 99")
    repo = _scratch(
        tmp_path,
        {"src/calc.py": BASE_CALC, "scripts/helper.py": BASE_HELPER},
        {"src/calc.py": HEAD_CALC, "scripts/helper.py": HEAD_HELPER, "tests/test_calc.py": bad},
    )
    proc = _run(repo)
    assert proc.returncode == 2
    assert "fail on the branch itself" in proc.stdout + proc.stderr
    assert len(_worktrees(repo)) == 1


# The test sleeps in the baseline run, or only once a hunk is reverted. Before it
# sleeps it writes its pid to $HR_MARK, so a test knows exactly which run sleeps.
_MARK = "    open(os.environ['HR_MARK'], 'w').write(str(os.getpid()))\n"
SLOW_ALWAYS = "import os\nimport time\n\n\ndef test_wait():\n" + _MARK + "    time.sleep(60)\n"
SLOW_ON_REVERT = (
    "import os\nimport time\n\nfrom src.calc import add\n\n\ndef test_wait():\n"
    "    if add(1, 1) != 3:\n" + _MARK.replace("    ", "        ", 1) + "        time.sleep(60)\n"
)


@pytest.mark.parametrize(
    ("sig", "test", "wait_for"),
    [
        (signal.SIGINT, SLOW_ALWAYS, "worktrees: "),
        (signal.SIGINT, SLOW_ON_REVERT, "baseline: "),
        (signal.SIGTERM, SLOW_ON_REVERT, "baseline: "),
        (signal.SIGHUP, SLOW_ALWAYS, "worktrees: "),
    ],
)
def test_ctrl_c_removes_the_worktrees(tmp_path, sig, test, wait_for):
    """Ctrl-C (and SIGTERM or SIGHUP) in the baseline run or in a hunk's run."""
    repo = _scratch(
        tmp_path,
        {"src/calc.py": BASE_CALC},
        {"src/calc.py": HEAD_CALC, "tests/test_wait.py": test},
    )
    proc, run_dir, _ = _start(repo, wait_for, tmp_path / "sleeping")
    proc.send_signal(sig)
    rest = proc.communicate(timeout=30)[0]
    assert proc.returncode == 130, rest
    assert "interrupted" in rest
    assert not run_dir.exists()
    assert len(_worktrees(repo)) == 1
    assert _git(repo, "status", "--porcelain") == ""


def test_binary_and_non_utf8_files_are_skipped_and_reported(tmp_path):
    repo = _scratch(tmp_path, {"src/calc.py": BASE_CALC}, {})
    (repo / "src/blob.bin").write_bytes(b"\x00\x01old")
    (repo / "src/latin.txt").write_bytes(b"caf\xe9 old\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "more base")
    _git(repo, "branch", "-f", "main")
    (repo / "src/blob.bin").write_bytes(b"\x00\x01new")
    (repo / "src/latin.txt").write_bytes(b"caf\xe9 new\n")
    _write(repo, "src/calc.py", HEAD_CALC)
    _write(
        repo,
        "tests/test_calc.py",
        "from src.calc import add\n\n\ndef test_add():\n    assert add(1, 1) == 3\n",
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "binary")
    out = _run(repo).stdout
    assert "skip src/blob.bin (binary file)" in out, out
    assert "skip src/latin.txt (not UTF-8 text)" in out, out


def test_src_must_import_from_the_worktree(tmp_path):
    """The import check: a ``src`` that resolves elsewhere stops the run (exit 2)."""
    repo = _scratch(
        tmp_path,
        {"src/calc.py": BASE_CALC, "src/__init__.py": '__path__ = ["/nonexistent-src"]\n'},
        {"src/calc.py": HEAD_CALC, "tests/test_calc.py": TEST_CALC},
    )
    proc = _run(repo)
    assert proc.returncode == 2, proc.stdout
    assert "src/ did not import from the worktree (got /nonexistent-src)" in proc.stdout
    assert len(_worktrees(repo)) == 1


def test_fail_on_skip_is_passed_when_the_conftest_defines_it(tmp_path):
    conftest = (
        "import pytest\n\n\ndef pytest_addoption(parser):\n"
        '    parser.addoption("--fail-on-skip", action="store_true")\n\n\n'
        "@pytest.hookimpl(hookwrapper=True)\ndef pytest_runtest_makereport(item, call):\n"
        "    outcome = yield\n    report = outcome.get_result()\n"
        '    if item.config.getoption("--fail-on-skip") and report.skipped:\n'
        '        report.outcome = "failed"\n'
    )
    skipping = TEST_CALC + "\n\ndef test_skipped():\n    import pytest\n    pytest.skip('no SDK')\n"
    head = {
        "src/calc.py": HEAD_CALC,
        "scripts/helper.py": HEAD_HELPER,
        "tests/test_calc.py": skipping,
    }
    base = {"src/calc.py": BASE_CALC, "scripts/helper.py": BASE_HELPER}
    with_option = _scratch(tmp_path / "a", {**base, "tests/conftest.py": conftest}, head)
    proc = _run(with_option)
    assert proc.returncode == 2, proc.stdout  # the skip fails the baseline, as in ci/test.sh
    assert "fail on the branch itself" in proc.stdout
    assert "FAILED tests/test_calc.py::test_skipped" in proc.stdout
    without = _scratch(tmp_path / "b", base, head)
    proc = _run(without)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "1 skipped: a skipped test notices nothing" in proc.stdout


def test_bytecode_is_never_reused_across_hunks(tmp_path):
    """``PYTHONDONTWRITEBYTECODE`` for the runs, and every ``__pycache__`` removed after
    each restore (a test can start Python with a fresh environment)."""
    tree = tmp_path / "tree"
    (tree / "src").mkdir(parents=True)
    (tree / "src/__init__.py").write_text("")
    (tree / "src/m.py").write_text("X = 1\n")
    (tree / "tests").mkdir()
    (tree / "tests/test_m.py").write_text(
        "import subprocess, sys\n\n\ndef test_fresh_env():\n"
        "    subprocess.run([sys.executable, '-c', 'import src.m'], cwd='.', env={}, check=True)\n"
    )
    runner = Runner(tmp_path, 1, 60, ["tests/test_m.py::test_fresh_env"])
    try:
        assert runner._env(tree)["PYTHONDONTWRITEBYTECODE"] == "1"
        runner.free.put(tree)
        item = hunk_revert.Item(Hunk("src/m.py", "src/m.py", 1, 1, 1, 1), "X = 1\n", "X = 2\n", "")
        assert runner.check(item)[0] == "unnoticed"
        assert (tree / "src/m.py").read_text() == "X = 1\n"
        assert list(tree.rglob("__pycache__")) == []
    finally:
        runner.cleanup()


def test_no_run_starts_once_stopping(tmp_path):
    runner = Runner(tmp_path, 1, 60, ["tests/test_wait.py::test_wait"])
    try:
        runner.stop()
        assert runner.pytest(tmp_path)[0] == "fail"
        assert not (runner.root / "pgids").exists()  # no process was started
    finally:
        runner.cleanup()


def _start(
    repo: Path, test_wait_for: str, mark: Path, via_uv: bool = False
) -> tuple[subprocess.Popen, Path, tuple[int, str]]:
    """Start the tool in its own process group, wait for a line, then for the sleeping
    pytest run: its pid in ``mark`` and in the tool's ``pgids`` record. Returns the
    process, the run folder and that run's ``(pid, start time)``. ``via_uv`` runs the
    tool the way ``pre_merge.sh`` does, ``uv run python ci/hunk_revert.py ... | tee``."""
    if via_uv:
        uv = shutil.which("uv")
        assert uv, "uv is on PATH wherever this suite runs"
        argv = [
            "bash", "-c",
            f'"{uv}" run --project "{REPO}" --no-sync python "{SCRIPT}" --base main --jobs 2 2>&1'
            f' | tee "{repo.parent / "tee.log"}"',
        ]  # fmt: skip
    else:
        argv = [sys.executable, str(SCRIPT), "--base", "main", "--jobs", "2"]
    proc = subprocess.Popen(
        argv,
        cwd=repo,
        env=_env(HR_MARK=str(mark)),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    assert proc.stdout is not None
    run_dir = None
    for line in proc.stdout:
        if line.startswith("worktrees: "):
            run_dir = Path(line.split(" ", 1)[1].strip())
        if line.startswith(test_wait_for):
            break
    assert run_dir is not None and run_dir.exists()
    sleeping = _wait_for_sleeper(mark, run_dir)
    assert sleeping is not None, "the sleeping run never started, or was never recorded"
    return proc, run_dir, sleeping


def _wait_for_sleeper(mark: Path, run_dir: Path) -> tuple[int, str] | None:
    """The sleeping run's ``(pid, start time)`` once both the test and the tool have
    written it (polled, at most 300 times 0.1 s)."""
    for _ in range(300):
        text = mark.read_text().strip() if mark.is_file() else ""
        recorded = dict(hunk_revert._recorded(run_dir / "pgids"))
        if text.isdigit() and recorded.get(int(text)):
            return int(text), str(recorded[int(text)])
        time.sleep(0.1)
    return None


def _ps_row(pid: int) -> str:
    """What ``ps`` says about ``pid``, for a failure message."""
    return subprocess.run(
        ["ps", "-ww", "-o", "pid,ppid,pgid,sess,stat,lstart,command", "-p", str(pid)],
        capture_output=True,
        text=True,
    ).stdout


def _recorded_runs(run_dir: Path) -> list[tuple[int, str]]:
    return [(pid, start) for pid, start in hunk_revert._recorded(run_dir / "pgids") if start]


def _gone(pid: int, start: str | None) -> bool:
    """Whether the recorded run is gone (a zombie or a reused pid counts as gone), polled
    at most 100 times 0.1 s: a killed process may wait a moment to be reaped."""
    for _ in range(100):
        if not hunk_revert._same_process(pid, start):
            return True
        time.sleep(0.1)
    return False


@pytest.mark.parametrize(
    ("test", "wait_for"), [(SLOW_ALWAYS, "worktrees: "), (SLOW_ON_REVERT, "baseline: ")]
)
@pytest.mark.parametrize("gap", [0.05, 0.3])
def test_a_second_ctrl_c_through_uv_and_tee_does_not_cut_the_cleanup_short(
    tmp_path, test, wait_for, gap
):
    """SIGINT to the whole process group twice, the way a terminal sends a double Ctrl-C
    to ``pre_merge.sh``'s ``uv run ... | tee``: every worktree, the folder and every
    pytest process group are gone."""
    repo = _scratch(
        tmp_path, {"src/calc.py": BASE_CALC}, {"src/calc.py": HEAD_CALC, "tests/test_wait.py": test}
    )
    proc, run_dir, _ = _start(repo, wait_for, tmp_path / "sleeping", via_uv=True)
    runs = _recorded_runs(run_dir)
    os.killpg(proc.pid, signal.SIGINT)
    time.sleep(gap)
    try:
        os.killpg(proc.pid, signal.SIGINT)
    except (ProcessLookupError, PermissionError):
        pass  # it had already finished (macOS answers EPERM for a group of zombies)
    proc.communicate(timeout=60)  # tee is gone too, so the last lines have no reader
    assert not run_dir.exists()
    assert len(_worktrees(repo)) == 1, _worktrees(repo)
    assert runs
    for pid, start in runs:
        assert _gone(pid, start), _ps_row(pid)
    assert _git(repo, "status", "--porcelain") == ""


@pytest.fixture
def saved_signals():
    saved = {sig: signal.getsignal(sig) for sig in hunk_revert.SIGNALS}
    yield
    for sig, handler in saved.items():
        signal.signal(sig, handler)


def test_the_handler_ignores_further_signals_before_it_raises(saved_signals):
    with pytest.raises(KeyboardInterrupt):
        hunk_revert._interrupt(signal.SIGINT, None)
    assert all(signal.getsignal(sig) == signal.SIG_IGN for sig in hunk_revert.SIGNALS)


def test_signals_are_ignored_during_a_normal_cleanup(tmp_path, monkeypatch, saved_signals):
    """A first Ctrl-C that lands while a finished run removes its worktrees must not
    cut that short: the signals are ignored before the cleanup starts."""
    repo = _scratch(
        tmp_path,
        {"src/calc.py": BASE_CALC, "scripts/helper.py": BASE_HELPER},
        {
            "src/calc.py": HEAD_CALC,
            "scripts/helper.py": HEAD_HELPER,
            "tests/test_calc.py": TEST_CALC,
        },
    )
    seen = []
    cleanup = Runner.cleanup

    def watching(self):
        seen.append([signal.getsignal(sig) for sig in hunk_revert.SIGNALS])
        cleanup(self)

    monkeypatch.setattr(Runner, "cleanup", watching)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.chdir(repo)
    assert main(["--base", "main"]) == 0
    assert seen == [[signal.SIG_IGN] * len(hunk_revert.SIGNALS)]


def test_a_killed_run_is_swept_by_the_next_one(tmp_path):
    """SIGKILL leaves locked worktrees, the folder and the sleeping pytest behind; the
    next run removes the first two and kills the third."""
    repo = _scratch(
        tmp_path,
        {"src/calc.py": BASE_CALC},
        {"src/calc.py": HEAD_CALC, "tests/test_wait.py": SLOW_ON_REVERT},
    )
    proc, run_dir, (pid, start) = _start(repo, "baseline: ", tmp_path / "sleeping")
    listed = _git(repo, "worktree", "list", "--porcelain")
    assert listed.count(f"locked hunk-revert pid {proc.pid}") == 2, listed
    os.killpg(proc.pid, signal.SIGKILL)
    proc.communicate(timeout=30)  # reaps the killed tool, so its pid is really gone
    assert run_dir.exists() and len(_worktrees(repo)) == 3  # what SIGKILL leaves
    assert hunk_revert._same_process(pid, start)  # the orphaned reverted run still sleeps
    nxt = _run(repo, "--timeout", "2")
    assert "sweep: removed 2 worktree(s) and 0 folder(s) and killed " in nxt.stdout, nxt.stdout
    assert not run_dir.exists()
    assert len(_worktrees(repo)) == 1
    assert _gone(pid, start), _ps_row(pid) + nxt.stdout


def test_ps_output_is_never_cut_to_a_terminal_width(monkeypatch):
    """CI sets ``COLUMNS``; ``ps`` cut its lines to it, and a long interpreter path hid
    the rest of the command line (#1888's first Linux failure)."""
    monkeypatch.setenv("COLUMNS", "20")
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    row = hunk_revert._ps("-o", "command=", "-p", str(os.getpid()))
    assert "pytest" in row or "python" in row
    assert len(row.strip()) > 20, row
    # Start times do not depend on the caller's time zone.
    here = hunk_revert.identity(os.getpid())
    monkeypatch.setenv("TZ", "Asia/Tokyo")
    assert hunk_revert.identity(os.getpid()) == here


def test_a_run_and_a_child_in_its_own_session_are_killed_together(tmp_path):
    """``kill_tree``: the pytest run's group and every descendant, even one that left
    the group with a session of its own."""
    mark = tmp_path / "child"
    code = (
        "import subprocess, sys, time\n"
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
        " start_new_session=True)\n"
        f"open({str(mark)!r}, 'w').write(str(c.pid))\n"
        "time.sleep(60)\n"
    )
    parent = subprocess.Popen([sys.executable, "-c", code], start_new_session=True)
    try:
        waits = 0
        while not mark.is_file() and waits < 200:
            time.sleep(0.1)
            waits += 1
        child = int(mark.read_text())
        child_start = hunk_revert.identity(child)
        assert child_start and hunk_revert._same_process(child, child_start)
        hunk_revert.kill_tree(parent.pid)
        parent.wait(timeout=10)
        assert _gone(child, child_start), _ps_row(child)
    finally:
        if parent.poll() is None:
            parent.kill()
            parent.wait()


def test_a_zombie_or_a_reused_pid_is_not_the_recorded_process():
    """``os.kill(pid, 0)`` succeeds on a zombie and on a reused pid; the sweep's check
    does not count either as the recorded process."""
    me = hunk_revert.identity(os.getpid())
    assert me and hunk_revert._same_process(os.getpid(), me)
    assert not hunk_revert._same_process(os.getpid(), "Mon Jan  1 00:00:00 1990")
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    try:
        for _ in range(100):  # until it has exited and is a zombie (it is never waited)
            state = subprocess.run(
                ["ps", "-o", "stat=", "-p", str(child.pid)], capture_output=True, text=True
            ).stdout.strip()
            if state.startswith("Z"):
                break
            time.sleep(0.1)
        assert state.startswith("Z"), state
        os.kill(child.pid, 0)  # no error: a zombie still has its pid
        assert hunk_revert.identity(child.pid) is None
    finally:
        child.wait()


def test_the_sweep_leaves_a_live_run_and_young_folders_alone(tmp_path, own_tmpdir):
    repo = _scratch(tmp_path, {"src/calc.py": BASE_CALC}, {})
    live = own_tmpdir / (PREFIX + "live")
    live.mkdir()
    (live / "owner").write_text(f"{os.getppid()}\n")  # a live process, not the sweeper
    young = own_tmpdir / (PREFIX + "young")  # no owner file yet: a run just starting
    young.mkdir()
    dead = own_tmpdir / (PREFIX + "dead")
    dead.mkdir()
    gone = subprocess.Popen(["true"])
    gone.wait()
    (dead / "owner").write_text(f"{gone.pid}\n")
    assert sweep(repo, own_tmpdir) == (0, 1, 0)
    assert live.exists() and young.exists() and not dead.exists()
    old = young.stat().st_mtime - 120  # older than the sweep's one-minute grace
    os.utime(young, (old, old))
    assert sweep(repo, own_tmpdir) == (0, 1, 0)
    assert not young.exists()
    shutil.rmtree(live)


def test_the_sweep_removes_a_locked_registration_whose_folder_is_gone(tmp_path, own_tmpdir):
    """``git worktree prune`` keeps a locked entry; the sweep removes it by its path."""
    repo = _scratch(tmp_path, {"src/calc.py": BASE_CALC}, {})
    folder = own_tmpdir / (PREFIX + "gone")
    _git(repo, "worktree", "add", "-q", "--detach", "--lock", str(folder / "wt0"), "HEAD")
    shutil.rmtree(folder)
    assert len(_worktrees(repo)) == 2
    assert sweep(repo, own_tmpdir) == (1, 0, 0)
    assert len(_worktrees(repo)) == 1


def test_the_sweep_prunes_a_registration_whose_folder_is_gone(tmp_path, own_tmpdir):
    repo = _scratch(tmp_path, {"src/calc.py": BASE_CALC}, {})
    stale = tmp_path / "elsewhere"
    _git(repo, "worktree", "add", "-q", "--detach", str(stale), "HEAD")
    shutil.rmtree(stale)
    assert len(_worktrees(repo)) == 2
    sweep(repo, own_tmpdir)
    assert len(_worktrees(repo)) == 1


# ---- the pieces --------------------------------------------------------------------


def test_hunk_headers_default_a_missing_count_to_one():
    patch = "diff --git a/x b/x\n@@ -3 +3,2 @@ def f():\n-a\n+b\n+c\n@@ -10,2 +11,0 @@\n-x\n-y\n"
    assert parse_hunk_headers(patch) == [(3, 1, 3, 2), (10, 2, 11, 0)]


def test_name_status_z_reads_adds_modifications_and_renames():
    out = "M\0src/a.py\0A\0src/new file.py\0R087\0src/old.py\0src/moved.py\0"
    assert parse_name_status_z(out) == [
        ("src/a.py", "src/a.py"),
        ("src/new file.py", None),
        ("src/moved.py", "src/old.py"),
    ]


@pytest.mark.parametrize(
    ("base", "head", "hunk", "want"),
    [
        # a changed line
        ("a\nb\nc\n", "a\nB\nc\n", (2, 1, 2, 1), "a\nb\nc\n"),
        # an added line (old count 0): it goes away
        ("a\nc\n", "a\nb\nc\n", (1, 0, 2, 1), "a\nc\n"),
        # a deleted line (new count 0): it comes back after line 1
        ("a\nb\nc\n", "a\nc\n", (2, 1, 1, 0), "a\nb\nc\n"),
        # a deleted first line comes back at the top
        ("a\nb\n", "b\n", (1, 1, 0, 0), "a\nb\n"),
        # a new file empties
        ("", "x\ny\n", (0, 0, 1, 2), ""),
        # no newline at the end of the old file
        ("a\nb", "a\nb\nc\n", (2, 1, 2, 2), "a\nb"),
    ],
)
def test_revert_puts_back_only_that_hunk(base, head, hunk, want):
    assert revert(head, base, Hunk("f", "f", *hunk)) == want


def test_revert_leaves_the_other_hunks_in_place():
    base = "a\nb\nc\nd\n"
    head = "a\nB\nc\nD\n"
    assert revert(head, base, Hunk("f", "f", 2, 1, 2, 1)) == "a\nb\nc\nD\n"
    assert revert(head, base, Hunk("f", "f", 4, 1, 4, 1)) == "a\nB\nc\nd\n"


def test_skip_reason_reads_python_code_not_text():
    head = "def f():\n    # new comment\n    return 1\n"
    assert skip_reason("src/a.py", head, "def f():\n    # old\n    return 1\n") == (
        "comment or formatting only"
    )
    assert skip_reason("src/a.py", "x = 1  \n", "x = 1\n") == "whitespace only"
    # A real change, and an indentation change that moves a statement, are checked.
    assert skip_reason("src/a.py", "x = 2\n", "x = 1\n") is None
    moved = "if a:\n    b()\nc()\n"
    assert skip_reason("src/a.py", moved, "if a:\n    b()\n    c()\n") is None
    # A string that is not a docstring is code.
    assert (
        skip_reason("src/a.py", "def f():\n    x = 1\n    'b'\n", "def f():\n    x = 1\n    'a'\n")
        is None
    )


@pytest.mark.parametrize(
    "path",
    [
        "src/contracts/m.py",  # a model docstring is the JSON schema's description
        "src/tools/schema/t.py",  # a @tool docstring is the tool spec the model reads
        "scripts/cli.py",  # a module docstring is the --help text
    ],
)
def test_no_docstring_hunk_is_skipped(path):
    """Docstrings are behaviour in this repository, so no docstring hunk is skipped."""
    head = '"""Usage: cli NEW."""\n\n\nclass M:\n    """New."""\n'
    assert skip_reason(path, head, '"""Usage: cli OLD."""\n\n\nclass M:\n    """New."""\n') is None
    assert skip_reason(path, head, '"""Usage: cli NEW."""\n\n\nclass M:\n    """Old."""\n') is None


@pytest.mark.parametrize(
    ("path", "head", "reverted"),
    [
        ("src/a.toml", "a = 1  \n\n", "a = 1\n"),  # trailing space, a blank line
        ("src/a.yaml", "  a: 1\n", "a: 1\n"),  # indentation
        ("src/a.yaml", "x: |\n  # data\n", "x: |\n  # other\n"),  # a block scalar
        ("src/a.toml", 'x = """\n# data\n"""\n', 'x = """\n# old\n"""\n'),  # a string
        ("scripts/a.sh", "cat <<EOF\n# data\nEOF\n", "cat <<EOF\nEOF\n"),  # a heredoc
        ("scripts/a.sh", "a \\\n# c\n  b\n", "a \\\n  b\n"),  # a continuation
        ("src/skills/a.md", "a\n\nb\n", "a\nb\n"),  # a blank line in a prompt
        ("scripts/a.sh", "# new\nx=1\n", "# old\nx=1\n"),  # even a plain comment
    ],
)
def test_other_files_are_never_skipped(path, head, reverted):
    """Outside Python a "#" line or a blank line can be data, so nothing is skipped."""
    assert skip_reason(path, head, reverted) is None


def test_lines_split_on_newline_only_as_git_counts_them():
    text = "# section\x0c marker\nx = 1\rstill line 2\nlast"
    assert split_lines(text) == ["# section\x0c marker\n", "x = 1\rstill line 2\n", "last"]
    assert split_lines("") == [] and split_lines("a\n") == ["a\n"]
    # The revert lands on git's line 3, not one line early.
    head = "# a\x0cb\nx\ny = 3\n"
    assert revert(head, "# a\x0cb\nx\ny = 2\n", Hunk("f", "f", 3, 1, 3, 1)) == (
        "# a\x0cb\nx\ny = 2\n"
    )


@pytest.mark.parametrize(
    "first",
    [
        "# section\x0c marker",  # the reviewer's repro: a form feed in a comment
        "# section\x1c marker",
        "# section\x85 marker",
        "# section\u2028 marker",
        '"""Section\r marker."""',  # Python reads a lone CR in source as a line break
    ],
)
def test_an_odd_line_break_before_a_hunk_does_not_hide_it(tmp_path, first):
    """A character ``str.splitlines`` splits on but git does not, before a guarded
    change, once shifted the revert by a line and turned it into a skipped hunk."""
    base = f"{first}\n\n\ndef f():\n    x = 1\n    return 2\n"
    test = "from src.m import f\n\n\ndef test_f():\n    assert f() == 3\n"
    repo = _scratch(
        tmp_path,
        {"src/m.py": base},
        {"src/m.py": base.replace("return 2", "return 3"), "tests/test_m.py": test},
    )
    proc = _run(repo)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert _lines(proc.stdout, "noticed ") == ["src/m.py:6"], proc.stdout
    assert "skip " not in proc.stdout


def test_skip_reason_leaves_ui_code_to_its_own_tests():
    assert skip_reason("src/ui/src/App.js", "a\n", "b\n") == "UI code: this step runs pytest only"


def test_a_python_file_that_does_not_parse_is_checked():
    assert skip_reason("src/a.py", "def (:\n", "def f(): pass\n") is None


def test_the_defaults():
    args = parser().parse_args([])
    assert (args.base, args.max_hunks, args.timeout, args.jobs, args.strict) == (
        "origin/main",
        40,
        300,
        1,
        False,
    )


@pytest.mark.parametrize("bad", [["--jobs", "0"], ["--timeout", "0"], ["--max-hunks", "-1"]])
def test_bounds_that_make_no_sense_are_refused(bad):
    with pytest.raises(SystemExit) as exc:
        main(bad)
    assert exc.value.code == 2


# ---- pre_merge.sh --mutation ---------------------------------------------------------


def _mutation_step(mutation: int, printed: str, code: int) -> str:
    sh = (REPO / "ci" / "pre_merge.sh").read_text(encoding="utf-8")
    step = "# f2. " + sh.split("# f2. ")[1].split("# g. ")[0]
    script = (
        "set -euo pipefail\n"
        f"mutation={mutation}\nbase=origin/main\n"
        f"uv() {{ printf '%s\\n' \"$*\" > \"$ARGS_FILE\"; printf '%s\\n' {printed!r}; return {code}; }}\n"
        'note() { printf "%s\\n" "$*"; }\nfail() { note "FAIL $*"; }\n' + step
    )
    return script


@pytest.mark.parametrize(
    ("mutation", "code", "want"),
    [
        (0, 0, "skip mutation: off (pass --mutation to revert each changed hunk)"),
        (1, 0, "ok   mutation: hunk-revert: summary: 2 checked: 2 noticed"),
        (1, 1, "warn mutation: hunk-revert: summary: 2 checked: 2 noticed"),
        (1, 2, "warn mutation: could not run (output above)"),
    ],
)
def test_pre_merge_mutation_step_is_opt_in_and_never_fails(tmp_path, mutation, code, want):
    args_file = tmp_path / "args"
    script = _mutation_step(mutation, "hunk-revert: summary: 2 checked: 2 noticed", code)
    proc = subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "ARGS_FILE": str(args_file)},
    )
    assert proc.stdout.strip().splitlines()[-1] == want
    assert "FAIL" not in proc.stdout
    if mutation:
        assert args_file.read_text().split() == [
            "run",
            "python",
            "ci/hunk_revert.py",
            "--base",
            "origin/main",
            "--strict",
        ]
    else:
        assert not args_file.exists()


@pytest.mark.parametrize(("args", "want"), [([], "0"), (["--quick", "--mutation"], "1")])
def test_pre_merge_sh_mutation_is_off_unless_asked(args, want):
    """The argument loop, under ``set -u``: --mutation turns the step on, and the
    default is set (an unset flag would stop the whole script)."""
    sh = (REPO / "ci" / "pre_merge.sh").read_text(encoding="utf-8")
    loop = "quick=0\n" + sh.split("\nquick=0\n")[1].split("\ndone\n")[0] + "\ndone\n"
    proc = subprocess.run(
        ["bash", "-c", "set -euo pipefail\n" + loop + 'echo "$mutation"', "pre_merge", *args],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == want


def test_pre_merge_sh_takes_the_mutation_flag():
    sh = (REPO / "ci" / "pre_merge.sh").read_text(encoding="utf-8")
    assert "--mutation) mutation=1 ;;" in sh
    assert sh.index("# f. ") < sh.index("# f2. ") < sh.index("# g. ")
    usage = subprocess.run(
        ["bash", str(REPO / "ci" / "pre_merge.sh"), "--help"], capture_output=True, text=True
    )
    assert "--mutation" in usage.stdout
