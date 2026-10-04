"""``run_assessment.py`` keeps stdout short when it is not a terminal (#278).

On the discourse sample the script used to print ~108 KB of progress to
stdout. A headless Claude Code session persists a tool result that large to
a file outside the repository and returns only a 2 KB preview, so the
``{"phase": ...}`` status lines /modernize needs were out of reach. Now,
unless ``--verbose`` is given or stdout is a TTY, stdout carries only the
status lines plus one final ``{"log": ...}`` line, and the progress goes to
``artifacts/<db>/<job>/logs/run_assessment.log``.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts import run_assessment
from scripts._compact_output import CompactConsole

REPO_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPO_ROOT / "scripts" / "run_assessment.py"
DISCOURSE_ZIP = REPO_ROOT / "docs" / "examples" / "discourse" / "discourse.zip"

# Claude Code returns tool results inline only below roughly this size.
STDOUT_LIMIT = 20_000
ASSESSMENT_PHASES = ["collect", "triage", "analysis", "assignment", "reality_check"]


def _discourse(tmp_path: Path) -> Path:
    with zipfile.ZipFile(DISCOURSE_ZIP) as z:
        z.extractall(tmp_path / "input")
    return tmp_path / "input" / "discourse-collection.json"


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("MODERNIZER_CI_SANDBOX", None)
    env.setdefault("AWS_DEFAULT_REGION", "us-east-1")
    return env


def _run_script(tmp_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 - fixed argv
        [sys.executable, str(SCRIPT), *args],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=_env(),
        timeout=300,
    )


@pytest.fixture(scope="module")
def discourse_compact(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("compact")
    src = _discourse(tmp_path)
    proc = _run_script(tmp_path, "--file", str(src), "--db", "discourse", "--mode", "chat")
    return tmp_path, proc


def _json_lines(stdout: str) -> list[dict]:
    return [json.loads(line) for line in stdout.splitlines() if line.strip()]


def test_discourse_stdout_is_bounded(discourse_compact):
    _, proc = discourse_compact
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert len(proc.stdout.encode()) < STDOUT_LIMIT
    # Everything the session sees is bounded, not just stdout.
    assert len((proc.stdout + proc.stderr).encode()) < STDOUT_LIMIT


def test_discourse_stdout_is_status_lines_then_log_pointer(discourse_compact):
    tmp_path, proc = discourse_compact
    lines = _json_lines(proc.stdout)  # every line parses as JSON
    *status, pointer = lines
    assert [s["phase"] for s in status] == ASSESSMENT_PHASES
    assert status[-1]["status"] == "awaiting_llm"
    job_id = status[0]["job_id"]

    assert set(pointer) == {"log", "log_offset", "log_lines"}
    assert pointer["log"] == f"artifacts/discourse/{job_id}/logs/run_assessment.log"
    log = (tmp_path / pointer["log"]).read_text(encoding="utf-8").splitlines()
    assert pointer["log_offset"] == 1
    assert pointer["log_lines"] == len(log)


def test_discourse_log_has_the_progress(discourse_compact):
    tmp_path, proc = discourse_compact
    pointer = _json_lines(proc.stdout)[-1]
    log = (tmp_path / pointer["log"]).read_text(encoding="utf-8")
    assert "[triage] Starting triage for discourse" in log
    assert "Database Modernizer Assessment" in log  # the stderr header
    assert '{"phase": "assignment", "status": "complete"' in log
    assert "\x1b[" not in log  # no terminal colors in the file
    assert len(log.encode()) > 50_000  # nothing was dropped


def test_resume_appends_to_the_same_log(discourse_compact, tmp_path):
    root, proc = discourse_compact
    first = _json_lines(proc.stdout)
    job_id, pointer = first[0]["job_id"], first[-1]
    rerun = _run_script(root, "--job-id", job_id, "--db", "discourse", "--llm-mode", "none")
    assert rerun.returncode == 0, rerun.stdout + rerun.stderr
    *status, second = _json_lines(rerun.stdout)
    assert [s["phase"] for s in status] == ASSESSMENT_PHASES[1:]
    assert second["log"] == pointer["log"]
    assert second["log_offset"] == pointer["log_lines"] + 1
    total = len((root / second["log"]).read_text(encoding="utf-8").splitlines())
    assert second["log_offset"] + second["log_lines"] - 1 == total


def test_verbose_prints_progress_to_stdout_and_writes_no_log(tmp_path):
    src = _discourse(tmp_path)
    proc = _run_script(tmp_path, "--file", str(src), "--db", "discourse", "--verbose")
    assert proc.returncode == 0, proc.stderr[-2000:]
    out = proc.stdout
    assert "\x1b[36m[triage]\x1b[0m Starting triage for discourse" in out
    assert len(out.encode()) > 50_000
    phases = [json.loads(ln)["phase"] for ln in out.splitlines() if ln.startswith('{"phase"')]
    assert phases == ASSESSMENT_PHASES
    assert '"log"' not in out.splitlines()[-1]
    assert "Database Modernizer Assessment" in proc.stderr
    assert not list((tmp_path / "artifacts").glob("*/*/logs"))


def test_error_before_the_job_exists_is_reported_inline(tmp_path):
    proc = _run_script(tmp_path, "--file", "missing.json", "--db", "discourse")
    assert proc.returncode == 1
    *status, pointer = _json_lines(proc.stdout)
    assert status == [
        {"phase": "collect", "status": "error", "message": "File not found: missing.json"}
    ]
    assert pointer["log"] is None
    assert "Database Modernizer Assessment" in pointer["output_tail"]
    assert len(proc.stdout.encode()) < STDOUT_LIMIT


def test_uncaught_exception_becomes_an_error_status_line(monkeypatch, tmp_path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        run_assessment, "phase_collect", lambda f, db, store: ("job-1", "wordpress")
    )

    def boom(*args, **kwargs):
        run_assessment._log("triage", "about to fail")
        raise RuntimeError("triage exploded")

    monkeypatch.setattr(run_assessment, "phase_triage", boom)
    monkeypatch.setattr(sys, "argv", ["run_assessment.py", "--file", "c.json", "--db", "wordpress"])
    with pytest.raises(RuntimeError):
        run_assessment.main()

    out = capsys.readouterr()
    *status, pointer = _json_lines(out.out)
    assert status == [
        {
            "phase": "triage",
            "status": "error",
            "message": "RuntimeError: triage exploded",
            "log": "artifacts/wordpress/job-1/logs/run_assessment.log",
        }
    ]
    log = (tmp_path / pointer["log"]).read_text(encoding="utf-8")
    assert "about to fail" in log
    assert "Traceback" in log and "triage exploded" in log
    assert out.err == ""
    assert not isinstance(sys.stdout, CompactConsole)
    assert not isinstance(sys.stderr, CompactConsole)


class _Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_verbose_when_flag_or_tty():
    assert run_assessment._want_verbose(False, _Tty())
    assert run_assessment._want_verbose(True, io.StringIO())
    assert not run_assessment._want_verbose(False, io.StringIO())


def test_modernize_reads_the_status_lines_and_the_log_pointer():
    text = (REPO_ROOT / ".claude" / "commands" / "modernize.md").read_text(encoding="utf-8")
    flat = " ".join(text.split())
    assert '"log_offset"' in text and '"log_lines"' in text
    assert "logs/run_assessment.log" in text
    assert "do not add `--verbose`" in flat
    assert "`offset` = `log_offset` and `limit` = `log_lines`" in flat
