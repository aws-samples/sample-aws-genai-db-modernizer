"""AGENTS.md / CLAUDE.md stay accurate and never contradict the headless commands (#300).

Claude Code loads CLAUDE.md (which imports AGENTS.md) in every session,
including headless ``/modernize --auto`` runs, so AGENTS.md must carry the
shared tool-use rule word for word, must not ask or wait for the user, and must
not suggest Bash tools the rule forbids. Every command, path, flag and name it
cites must exist in the repo, so the guide cannot silently go stale.
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.unit.scripts.test_modernize_command import (
    AUTO_EXEMPTION_PATTERN,
    CONFIRM_PATTERN,
    QUESTION_PATTERN,
    TOOL_USE_RULE,
    WAIT_PATTERN,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
AGENTS_MD = REPO_ROOT / "AGENTS.md"
CLAUDE_MD = REPO_ROOT / "CLAUDE.md"

CODE_SPAN = re.compile(r"`([^`\n]+)`")
PATH_PREFIXES = ("src/", "scripts/", "ci/", "tests/", "docs/", ".claude/", ".github/")
# Root files the guide links or names.
ROOT_FILES = ("CONTRIBUTING.md", "Makefile")
# Flags of tools outside this repo (git) that the guide names on purpose.
EXTERNAL_FLAGS = {"--no-verify"}


def _text() -> str:
    return AGENTS_MD.read_text()


def _code_spans() -> list[str]:
    text = _text().replace(TOOL_USE_RULE, "")
    spans = CODE_SPAN.findall(text)
    for block in re.findall(r"```[a-z]*\n(.*?)```", text, re.DOTALL):
        spans.extend(line.split("#", 1)[0].strip() for line in block.splitlines())
    return [s for s in spans if s]


def _corpus() -> str:
    """Source text the guide's names, flags and env vars must come from."""
    parts: list[str] = []
    for pattern in (
        "src/**/*.py",
        "scripts/*.py",
        "ci/*.sh",
        "ci/llm/*.py",
        ".claude/commands/*.md",
        "Makefile",
        "tests/e2e/*.py",
    ):
        parts.extend(p.read_text(errors="ignore") for p in REPO_ROOT.glob(pattern))
    return "\n".join(parts)


def _make_targets() -> set[str]:
    makefile = (REPO_ROOT / "Makefile").read_text()
    return set(re.findall(r"^([a-zA-Z][\w-]*):", makefile, re.MULTILINE))


def test_claude_md_imports_agents_md() -> None:
    assert CLAUDE_MD.read_text().strip() == "@AGENTS.md"


def test_claude_md_is_not_gitignored() -> None:
    lines = (REPO_ROOT / ".gitignore").read_text().splitlines()
    assert "CLAUDE.md" not in lines and "/CLAUDE.md" not in lines


def test_agents_md_carries_the_tool_use_rule_verbatim() -> None:
    assert TOOL_USE_RULE in " ".join(_text().split())


def test_agents_md_never_asks_or_waits_for_the_user() -> None:
    for line in _text().splitlines():
        for pattern in (QUESTION_PATTERN, WAIT_PATTERN, CONFIRM_PATTERN):
            if pattern.search(line):
                assert AUTO_EXEMPTION_PATTERN.search(line), f"unscoped prompt: {line!r}"


def test_agents_md_suggests_no_tool_the_rule_forbids() -> None:
    text = _text().replace(TOOL_USE_RULE, "")
    assert "Grep tool" not in text.replace("no Grep tool", "")
    # A forbidden tool used as a command (with arguments), not merely named.
    forbidden = re.compile(r"^(grep|rg|cat|jq|sed|ls|find|cd|python3 -c)\s+\S")
    for span in _code_spans():
        assert not forbidden.search(span), f"forbidden command in AGENTS.md: {span!r}"


def test_every_make_target_exists() -> None:
    targets = _make_targets()
    cited = {m for s in _code_spans() for m in re.findall(r"\bmake ([a-z][\w-]*)", s)}
    assert cited, "expected AGENTS.md to cite make targets"
    assert cited <= targets, f"unknown make targets: {sorted(cited - targets)}"


def test_every_cited_path_exists() -> None:
    cited: set[str] = set()
    for span in _code_spans():
        for token in re.split(r"[\s(),]+", span):
            token = token.strip("'\"").removeprefix("./")
            if token.startswith(PATH_PREFIXES) or token in ROOT_FILES:
                cited.add(token)
    assert any(c.startswith("scripts/") for c in cited)
    missing = []
    for path in sorted(cited):
        if "<" in path or path.endswith(("...", "…")):
            continue  # placeholder
        if path.startswith(".github/") and not (REPO_ROOT / ".github").is_dir():
            # GitHub-only paths (issue templates, workflows): the internal
            # validation mirror intentionally ships without .github/, so they
            # can only be checked where that directory exists.
            continue
        if "*" in path:
            if not list(REPO_ROOT.glob(path)):
                missing.append(path)
        elif not (REPO_ROOT / path.rstrip("/")).exists():
            missing.append(path)
    assert not missing, f"AGENTS.md cites paths that do not exist: {missing}"


def test_every_uv_run_script_exists() -> None:
    scripts = re.findall(r"uv run python (scripts/[\w./-]+\.py)", _text())
    assert scripts
    for script in scripts:
        assert (REPO_ROOT / script).is_file(), script


def test_every_cited_name_flag_and_env_var_exists() -> None:
    corpus = _corpus()
    spans = _code_spans()
    names = {s for s in spans if re.fullmatch(r"[a-z][a-z0-9]*(_[a-z0-9]+)+", s)}
    env_vars = {s.split("=")[0] for s in spans if re.match(r"(E2E_|MODERNIZER_|ARTIFACT_)", s)}
    env_vars |= {m for s in spans for m in re.findall(r"\b((?:E2E|MODERNIZER)_[A-Z_]+)=", s)}
    flags = {f for s in spans for f in re.findall(r"(?<![\w-])--[a-z][a-z-]+", s)}
    assert names and env_vars and flags
    for token in sorted(names | env_vars | (flags - EXTERNAL_FLAGS)):
        assert token in corpus, f"AGENTS.md cites {token!r}, not found in the repo"
