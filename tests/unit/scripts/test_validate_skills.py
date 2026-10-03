"""validate_skills checks every shipped command's script and prompt references."""

from __future__ import annotations

from pathlib import Path

from scripts import validate_skills


def test_scans_claude_commands(tmp_path: Path) -> None:
    cmds = tmp_path / ".claude" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "good.md").write_text("Run `uv run python scripts/run_report.py --job-id x`\n")
    (cmds / "bad.md").write_text("Run `uv run python scripts/does_not_exist.py`\n")
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "run_report.py").write_text("")

    errors = validate_skills.validate(tmp_path)

    assert any("bad.md" in e and "does_not_exist.py" in e for e in errors)
    assert not any("good.md" in e for e in errors)


def test_real_repo_commands_are_valid() -> None:
    repo = Path(__file__).resolve().parents[3]
    assert list((repo / ".claude" / "commands").glob("*.md")), "no commands found"
    assert validate_skills.validate(repo) == []


def test_flags_dot_artifacts_paths(tmp_path: Path) -> None:
    cmds = tmp_path / ".claude" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "x.md").write_text("Read `.artifacts/{db}/{job}/llm_requests/a.json`\n")

    errors = validate_skills.validate(tmp_path)

    assert any("x.md" in e and ".artifacts/" in e for e in errors)
