"""Hook 3: a reminder, never a block."""

from __future__ import annotations

import io
import json
import os
import subprocess  # nosec B404 -- builds a throwaway git repo with fixed argv
from pathlib import Path

import pytest

from scripts.hooks import remind_llm_run as hook
from tests.unit.hooks.conftest import REPO_ROOT


def test_reminder_for_model_facing_paths() -> None:
    message = hook.reminder([".claude/commands/modernize.md", "src/skills/x.md"], str(REPO_ROOT))
    assert message and "e2e-llm: required" in message


def test_no_reminder_when_nothing_required() -> None:
    assert hook.reminder(["docs/index.md"], str(REPO_ROOT)) is None
    assert hook.reminder([], str(REPO_ROOT)) is None


def _stop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, paths: list[str], event: dict) -> None:
    monkeypatch.setattr(hook, "changed_paths", lambda: paths)
    monkeypatch.setattr(hook, "STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(event)))
    monkeypatch.delenv("MODERNIZER_CI_SANDBOX", raising=False)


def test_stop_prints_the_system_message_and_exits_zero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _stop(monkeypatch, tmp_path, ["AGENTS.md"], {"session_id": "s1"})
    assert hook.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert "e2e-llm: required" in out["systemMessage"]
    assert "decision" not in out


def test_once_per_session_and_again_when_the_verdict_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def run(paths: list[str], session: str) -> str:
        _stop(monkeypatch, tmp_path, paths, {"session_id": session})
        assert hook.main() == 0
        return str(capsys.readouterr().out)

    assert run(["AGENTS.md"], "s1")
    assert run(["AGENTS.md"], "s1") == ""  # same session, same verdict
    assert run(["AGENTS.md", "src/shared/models.py"], "s1")  # verdict changed (bedrock-smoke)
    assert run(["AGENTS.md"], "s2")  # a new session is reminded again


def test_nothing_required_prints_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _stop(monkeypatch, tmp_path, ["docs/index.md"], {"session_id": "s1"})
    assert hook.main() == 0
    assert capsys.readouterr().out == ""


def test_silent_in_headless_and_on_continuation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _stop(monkeypatch, tmp_path, ["AGENTS.md"], {"session_id": "s1"})
    monkeypatch.setenv("MODERNIZER_CI_SANDBOX", "1")
    assert hook.main() == 0
    _stop(monkeypatch, tmp_path, ["AGENTS.md"], {"session_id": "s1", "stop_hook_active": True})
    assert hook.main() == 0
    assert capsys.readouterr().out == ""


def _git(repo: Path, *args: str) -> str:
    res = subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def test_changed_paths_uses_the_merge_base(tmp_path: Path) -> None:
    """A branch behind main must not be credited with files only main changed."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "feature")
    (repo / "base.txt").write_text("a")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-b", "main-side", base)
    (repo / "main_only.txt").write_text("m")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "main moves on")
    _git(repo, "update-ref", "refs/remotes/origin/main", _git(repo, "rev-parse", "HEAD"))
    _git(repo, "checkout", "-q", "feature")
    (repo / "committed.txt").write_text("c")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "mine")
    (repo / "uncommitted.txt").write_text("u")
    (repo / "base.txt").write_text("changed")
    assert hook.changed_paths(str(repo)) == ["base.txt", "committed.txt", "uncommitted.txt"]


def test_changed_paths_without_origin_main_is_empty(tmp_path: Path) -> None:
    repo = tmp_path / "r"
    repo.mkdir()
    _git(repo, "init", "-q")
    assert hook.changed_paths(str(repo)) == []


def test_real_run_exits_zero_with_valid_json_or_nothing(run_hook) -> None:
    env = {k: v for k, v in os.environ.items() if k != "MODERNIZER_CI_SANDBOX"}
    res = run_hook("remind_llm_run.py", {"hook_event_name": "Stop", "session_id": "t"}, env=env)
    assert res.code == 0
    if res.stdout:
        assert "systemMessage" in json.loads(res.stdout)


def test_bad_input_exits_zero(run_hook) -> None:
    assert run_hook("remind_llm_run.py", "garbage").code == 0
