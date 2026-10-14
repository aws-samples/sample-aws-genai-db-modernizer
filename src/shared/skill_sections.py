"""Read a shared prompt from ``src/skills/`` and split it into its sections (#628).

Some decisions are made by a model in two modes: Claude Code reads a skill
file directly (a command names it), and the Bedrock path builds its prompt in
Python. When each mode carries its own copy of the instruction, the two drift
and the same workload gets different answers. A decision with one skill file
read by both modes cannot drift that way: the command tells Claude Code to
read the file, and the Bedrock code loads the same file through this module.

A skill is Markdown. ``## Heading`` starts a section, ``### Heading`` a
subsection inside it and ``#### Heading`` one level further down; each level
is returned keyed by its heading text, with the heading line itself removed
and the body stripped.
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"

_HEADING = re.compile(r"^(#{2,4}) (.+?)\s*$")


@cache
def skill_path(name: str) -> Path:
    """The path of ``src/skills/<name>``; raises when the file is missing."""
    path = SKILLS_DIR / name
    if not path.is_file():
        raise FileNotFoundError(f"shared skill not found: {path}")
    return path


@cache
def skill_sections(name: str) -> dict[str, str]:
    """``{heading: body}`` for every ``##`` section of ``src/skills/<name>``.

    A ``##`` section's body runs to the next ``##`` heading and includes its
    ``###`` subsections; :func:`skill_subsections` splits those out.
    """
    return _split(skill_path(name).read_text(encoding="utf-8"), level=2)


@cache
def skill_subsections(name: str, *path: str) -> dict[str, str]:
    """``{heading: body}`` one level below the section named by ``path``.

    ``skill_subsections(name, "A")`` splits ``## A`` into its ``###``
    subsections; ``skill_subsections(name, "A", "B")`` splits ``### B`` inside
    it into its ``####`` subsections.
    """
    if not path:
        raise ValueError("path names at least the ## section")
    body = skill_sections(name)[path[0]]
    for depth, heading in enumerate(path[1:], start=3):
        body = _split(body, level=depth)[heading]
    return _split(body, level=len(path) + 2)


def _split(text: str, level: int) -> dict[str, str]:
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        match = _HEADING.match(line)
        if match and len(match.group(1)) == level:
            current = sections.setdefault(match.group(2), [])
            continue
        if match and len(match.group(1)) < level:
            current = None
            continue
        if current is not None:
            current.append(line)
    return {heading: "\n".join(body).strip() for heading, body in sections.items()}
