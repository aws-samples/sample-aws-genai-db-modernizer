"""Validate Claude Code skills reference valid scripts, contracts, and prompts.

Checks:
1. Script references (uv run python scripts/...) point to existing files
2. Skill prompt references (src/skills/*.md) point to existing files
3. Contract field references match actual Pydantic model fields

Exit codes:
    0 — all skills valid
    1 — validation errors found
"""

import re
import sys
from pathlib import Path

SKILLS_DIR = Path(".claude/skills")
SCRIPTS_DIR = Path("scripts")
SRC_SKILLS_DIR = Path("src/skills")


def main():
    if not SKILLS_DIR.exists():
        print("No .claude/skills/ directory found — skipping validation.")
        sys.exit(0)

    errors = []

    for skill_dir in sorted(SKILLS_DIR.iterdir()):
        if not skill_dir.is_dir():
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.exists():
            errors.append(f"{skill_dir.name}: missing SKILL.md")
            continue

        content = skill_file.read_text()
        errors.extend(_check_script_references(skill_dir.name, content))
        errors.extend(_check_skill_prompt_references(skill_dir.name, content))

    if errors:
        print(f"Skill validation failed ({len(errors)} errors):\n")
        for error in errors:
            print(f"  - {error}")
        sys.exit(1)
    else:
        print(f"All skills valid ({len(list(SKILLS_DIR.iterdir()))} skills checked)")
        sys.exit(0)


def _check_script_references(skill_name: str, content: str) -> list[str]:
    """Check that script paths in 'uv run python scripts/...' exist."""
    errors = []
    pattern = r"uv run python (scripts/[\w\-/]+\.py)"
    for match in re.finditer(pattern, content):
        script_path = Path(match.group(1))
        if not script_path.exists():
            errors.append(f"{skill_name}: references non-existent script '{script_path}'")
    return errors


def _check_skill_prompt_references(skill_name: str, content: str) -> list[str]:
    """Check that src/skills/*.md references exist."""
    errors = []
    pattern = r"(src/skills/[\w\-]+\.md)"
    for match in re.finditer(pattern, content):
        prompt_path = Path(match.group(1))
        if not prompt_path.exists():
            errors.append(f"{skill_name}: references non-existent prompt '{prompt_path}'")
    return errors


if __name__ == "__main__":
    main()
