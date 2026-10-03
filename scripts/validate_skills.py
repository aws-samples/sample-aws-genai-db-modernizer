"""Validate Claude Code commands reference valid scripts and prompts.

Checks:
1. Script references (uv run python scripts/...) point to existing files
2. Skill prompt references (src/skills/*.md) point to existing files

Exit codes:
    0 — all commands valid
    1 — validation errors found
"""

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def validate(repo_root: Path) -> list[str]:
    """Validate every shipped command/skill prompt under ``repo_root``.

    Scans ``.claude/commands/*.md`` (the commands actually shipped by this
    repo) and, if present, ``.claude/skills/*/SKILL.md`` (legacy location).
    Returns a list of human-readable error strings; empty means everything
    checked out.
    """
    errors: list[str] = []

    command_files = sorted((repo_root / ".claude" / "commands").glob("*.md"))
    for command_file in command_files:
        content = command_file.read_text()
        name = f".claude/commands/{command_file.name}"
        errors.extend(_check_script_references(name, content, repo_root))
        errors.extend(_check_skill_prompt_references(name, content, repo_root))
        errors.extend(_check_dot_artifacts_references(name, content))

    skills_dir = repo_root / ".claude" / "skills"
    if skills_dir.exists():
        for skill_dir in sorted(skills_dir.iterdir()):
            if not skill_dir.is_dir():
                continue
            skill_file = skill_dir / "SKILL.md"
            if not skill_file.exists():
                errors.append(f"{skill_dir.name}: missing SKILL.md")
                continue

            content = skill_file.read_text()
            errors.extend(_check_script_references(skill_dir.name, content, repo_root))
            errors.extend(_check_skill_prompt_references(skill_dir.name, content, repo_root))

    return errors


def _check_script_references(name: str, content: str, repo_root: Path) -> list[str]:
    """Check that script paths in 'uv run python scripts/...' exist."""
    errors = []
    pattern = r"uv run python (scripts/[\w\-/]+\.py)"
    for match in re.finditer(pattern, content):
        script_path = repo_root / match.group(1)
        if not script_path.exists():
            errors.append(f"{name}: references non-existent script '{match.group(1)}'")
    return errors


def _check_dot_artifacts_references(name: str, content: str) -> list[str]:
    """Flag `.artifacts/` references — scripts write to `./artifacts/`."""
    pattern = r"(?<![\w/])\.artifacts/"
    if re.search(pattern, content):
        return [f"{name}: uses .artifacts/ — scripts write ./artifacts/"]
    return []


def _check_skill_prompt_references(name: str, content: str, repo_root: Path) -> list[str]:
    """Check that src/skills/*.md references exist."""
    errors = []
    pattern = r"(src/skills/[\w\-]+\.md)"
    for match in re.finditer(pattern, content):
        prompt_path = repo_root / match.group(1)
        if not prompt_path.exists():
            errors.append(f"{name}: references non-existent prompt '{match.group(1)}'")
    return errors


def main() -> None:
    errors = validate(REPO_ROOT)

    files_checked = len(list((REPO_ROOT / ".claude" / "commands").glob("*.md")))
    skills_dir = REPO_ROOT / ".claude" / "skills"
    if skills_dir.exists():
        files_checked += len([d for d in skills_dir.iterdir() if d.is_dir()])

    if errors:
        print(f"Skill validation failed ({len(errors)} errors):\n")
        for error in errors:
            print(f"  - {error}")
        sys.exit(1)
    else:
        print(f"{files_checked} files checked")
        sys.exit(0)


if __name__ == "__main__":
    main()
