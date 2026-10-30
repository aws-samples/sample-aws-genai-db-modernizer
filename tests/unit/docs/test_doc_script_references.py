# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every script, CI entry point and CLI flag the docs name exists (#1162).

``scripts/validate_skills.py`` already checks the ``uv run python scripts/...``
paths in ``.claude/commands/*.md``. This test covers the rest of the prose:
``docs/**/*.md``, README.md, CONTRIBUTING.md, AGENTS.md, ci/README.md,
benchmarks/README.md, ``src/skills/*.md`` and the commands.

* A mention of ``scripts/<path>.py|sh`` or ``ci/<path>.py|sh`` (also written
  ``./ci/...``) must name an existing file.
* A ``--flag`` written after a Python script (a full path, or a bare name such as
  ``run_assessment.py``) must be defined by that script's ``add_argument`` calls.
  The calls are read from the AST; nothing is run. A bare name resolves to the one
  file of that name under ``scripts/`` or ``ci/``; none or several is an error.
* The arguments of a command end at a shell ``#`` comment, at ``` ` ```, ``|``,
  ``;``, ``&``, at the next script mention and at another tool's name (``pytest
  --lf`` is not a flag of the script before it).
* Shell scripts have no parser to read, so only their paths are checked.
* A script with ``add_subparsers`` has its flags scoped: the flags of one command
  must belong to the main parser plus one subparser. A script that builds parsers
  with ``parents=`` is checked against the union of all its flags.
* The path check for commands stays in ``validate_skills.py``; this test adds the
  flags there.
"""

from __future__ import annotations

import ast
import re
from functools import cache
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]

DOC_GLOBS = (
    "docs/**/*.md",
    "README.md",
    "CONTRIBUTING.md",
    "AGENTS.md",
    "ci/README.md",
    "benchmarks/README.md",
    "src/skills/*.md",
    ".claude/commands/*.md",
)
# Planning notes that quote old paths on purpose.
SKIPPED_DOCS = {"docs/superpowers"}

# `scripts/<path>.py`, `ci/<path>.sh` (optionally `./`-prefixed) or a bare `name.py`.
MENTION_RE = re.compile(r"(?<![\w/.\-])(?:\./)?((?:scripts|ci)/[\w\-/]+\.(?:py|sh)|[\w\-]+\.py)\b")
PATH_RE = re.compile(r"(?<![\w/.\-])(?:\./)?((?:scripts|ci)/[\w\-/]+\.(?:py|sh))\b")
FLAG_RE = re.compile(r"(?<![\w\-])(--[a-z][\w\-]*)")
COMMENT_RE = re.compile(r"\s#(\s|$)")
DELIMITER_RE = re.compile(r"[`|;&<>]")
# A flag after one of these names belongs to that tool, not to the script before it.
OTHER_TOOLS = {
    "pytest", "uv", "npm", "npx", "git", "gh", "pip", "docker", "aws", "make", "mkdocs",
    "ruff", "black", "mypy", "brazil", "python", "python3", "node", "bash", "sh", "curl",
}  # fmt: skip
ALWAYS_OK = {"--help"}


def _join_continuations(text: str) -> str:
    """Join shell lines that end in a backslash so a command is one line."""
    return re.sub(r"\\[ \t]*\n[ \t]*", " ", text)


def paths_named(text: str) -> list[str]:
    """Every ``scripts/...`` and ``ci/...`` file path the text names."""
    return sorted(set(PATH_RE.findall(text)))


def _arguments(line: str, start: int, stop: int) -> str:
    """The command's arguments: from ``start`` up to its delimiters and the next tool."""
    segment = re.sub(r"<[\w\-.]+>", "X", line[start:stop])  # a <placeholder> is one argument
    comment = COMMENT_RE.search(segment)
    if comment:
        segment = segment[: comment.start()]
    delimiter = DELIMITER_RE.search(segment)
    if delimiter:
        segment = segment[: delimiter.start()]
    kept = []
    for token in segment.split():
        if token in OTHER_TOOLS:
            break
        kept.append(token)
    return " ".join(kept)


def flags_named(text: str) -> list[tuple[str, tuple[str, ...]]]:
    """(script, flags) for each command: the script as written, the flags after it."""
    commands = set()
    for line in _join_continuations(text).splitlines():
        comment = COMMENT_RE.search(line)
        if comment:
            line = line[: comment.start()]
        mentions = list(MENTION_RE.finditer(line))
        for i, match in enumerate(mentions):
            stop = mentions[i + 1].start() if i + 1 < len(mentions) else len(line)
            flags = tuple(dict.fromkeys(FLAG_RE.findall(_arguments(line, match.end(), stop))))
            if flags:
                commands.add((match.group(1), flags))
    return sorted(commands)


def resolve(name: str, repo: Path = REPO) -> tuple[str | None, str]:
    """(relative path, problem) for a bare script name: it must be the one file of that name under scripts/ or ci/."""
    found = sorted(
        p.relative_to(repo).as_posix()
        for top in ("scripts", "ci")
        if (repo / top).is_dir()
        for p in (repo / top).rglob(name)
        if "__pycache__" not in p.parts
    )
    if not found:
        return None, f"names '{name}', which is not under scripts/ or ci/"
    if len(found) > 1:
        return None, f"names '{name}', which is ambiguous ({', '.join(found)})"
    return found[0], ""


def _option_strings(call: ast.Call) -> set[str]:
    flags = {a.value for a in call.args if isinstance(a, ast.Constant) and isinstance(a.value, str)}
    flags = {f for f in flags if f.startswith("--")}
    # BooleanOptionalAction also defines --no-<flag>, for this call's own flags only.
    if any(
        k.arg == "action" and "BooleanOptionalAction" in ast.unparse(k.value) for k in call.keywords
    ):
        flags |= {f"--no-{f[2:]}" for f in flags}
    return flags


@cache
def parser_scopes(
    script: str, repo: Path = REPO
) -> tuple[frozenset[str], tuple[frozenset[str], ...]]:
    """(main parser flags, one flag set per subparser) read from the script's AST.

    A script without subparsers has no subparser sets. A script that builds a parser
    with ``parents=`` is not scoped: every flag lands in the main set.
    """
    tree = ast.parse((repo / script).read_text())
    owner: dict[str, str] = {}  # variable -> "main" or the variable's own name for a subparser
    scoped = any(
        isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "add_subparsers"
        for n in ast.walk(tree)
    )
    if any(
        isinstance(n, ast.keyword) and n.arg == "parents" and not _is_mkdir(n, tree)
        for n in ast.walk(tree)
    ):
        scoped = False
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    if getattr(node.value.func, "attr", "") == "add_parser":
                        owner[target.id] = target.id
                    elif "ArgumentParser" in ast.unparse(node.value.func):
                        owner[target.id] = "main"
    main = set(ALWAYS_OK)
    subs: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "add_argument"
        ):
            var = node.func.value.id if isinstance(node.func.value, ast.Name) else ""
            where = owner.get(var, "main") if scoped else "main"
            (main if where == "main" else subs.setdefault(where, set())).update(
                _option_strings(node)
            )
    return frozenset(main), tuple(frozenset(s) for s in subs.values())


def _is_mkdir(keyword: ast.keyword, tree: ast.AST) -> bool:
    """``mkdir(parents=True)`` is the pathlib argument, not an argparse parent."""
    return isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, bool)


def unknown_flags(script: str, flags: tuple[str, ...], repo: Path = REPO) -> list[str]:
    """Flags the script's parser does not define (for one command, one subparser at most)."""
    main, subs = parser_scopes(script, repo)
    candidates = [main | s for s in subs] or [main]
    return min(([f for f in flags if f not in c] for c in candidates), key=len)


def doc_files() -> list[Path]:
    files: set[Path] = set()
    for pattern in DOC_GLOBS:
        files.update(REPO.glob(pattern))
    return sorted(
        f
        for f in files
        if not any(f.relative_to(REPO).as_posix().startswith(s) for s in SKIPPED_DOCS)
    )


def check(rel: str, text: str, repo: Path = REPO) -> list[str]:
    """Stale references in one document, as readable messages."""
    named = paths_named(text)
    missing = [path for path in named if not (repo / path).exists()]
    errors = [f"{rel}: names '{path}', which does not exist" for path in missing]
    for name, flags in flags_named(text):
        if not name.endswith(".py") or name in missing:
            continue
        path, problem = (name, "") if "/" in name else resolve(name, repo)
        if path is None:
            errors.append(f"{rel}: {problem} (it is given {' '.join(flags)})")
            continue
        errors.extend(
            f"{rel}: '{name} {flag}': the script defines no such flag"
            for flag in unknown_flags(path, flags, repo)
        )
    return errors


# --- the scanner itself ------------------------------------------------------
# These test helpers that live in this file, so they pass on main by construction;
# the guard against stale docs is the parametrized test below.


def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for name, body in files.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(body)
    return tmp_path


PARSER = "import argparse\np = argparse.ArgumentParser()\np.add_argument('--file')\n"


@pytest.mark.passes_on_main
def test_paths_named_finds_scripts_and_ci_files_only():
    text = "Run `scripts/run_triage.py` or ci/lint.sh; see tests/e2e/ci/x.sh, scripts/*.py and scripts/<name>.py."
    assert paths_named(text) == ["ci/lint.sh", "scripts/run_triage.py"]


@pytest.mark.passes_on_main
def test_paths_named_reads_the_dot_slash_form():
    assert paths_named("run ./ci/pre_merge.sh --quick, then ./scripts/setup_dev.sh") == [
        "ci/pre_merge.sh",
        "scripts/setup_dev.sh",
    ]


@pytest.mark.passes_on_main
def test_a_renamed_dot_slash_ci_script_fails():
    assert check("AGENTS.md", "Run `./ci/pre_merge.sh`.") == []
    errors = check("AGENTS.md", "Run `./ci/pre_merge_gone.sh`.")
    assert errors and "ci/pre_merge_gone.sh" in errors[0]


@pytest.mark.passes_on_main
def test_flags_named_reads_a_command_across_continuations():
    text = "```bash\nuv run python scripts/run_assessment.py \\\n  --file f.json --all -y\n```\n"
    assert flags_named(text) == [("scripts/run_assessment.py", ("--file", "--all"))]


@pytest.mark.passes_on_main
def test_flags_named_stops_at_the_closing_backtick():
    text = "Use `scripts/run_triage.py --job-id x`, not --other."
    assert flags_named(text) == [("scripts/run_triage.py", ("--job-id",))]


@pytest.mark.passes_on_main
def test_flags_named_ignores_a_shell_comment_and_another_tools_flags():
    text = "scripts/run_assessment.py --file x  # npm --legacy-peer-deps\nscripts/run_assessment.py --file x and pytest --lf\n"
    assert flags_named(text) == [("scripts/run_assessment.py", ("--file",))]


@pytest.mark.passes_on_main
def test_flags_named_reads_bare_names_and_ci_python_scripts():
    text = "`run_assessment.py --file <f> --llm-mode bedrock` and uv run python ci/contract_major_gate.py --base main"
    assert flags_named(text) == [
        ("ci/contract_major_gate.py", ("--base",)),
        ("run_assessment.py", ("--file", "--llm-mode")),
    ]


@pytest.mark.passes_on_main
def test_a_misspelled_flag_on_a_bare_script_name_in_agents_md_fails():
    agents = (REPO / "AGENTS.md").read_text()
    assert check("AGENTS.md", agents) == []
    assert "`run_assessment.py --file <f> --all -y --llm-mode bedrock`" in agents
    broken = agents.replace("-y --llm-mode bedrock`", "-y --llm-modee bedrock`")
    errors = check("AGENTS.md", broken)
    assert len(errors) == 1 and "--llm-modee" in errors[0]


@pytest.mark.passes_on_main
def test_check_reports_a_missing_script_and_an_unknown_flag(tmp_path):
    repo = _repo(tmp_path, {"scripts/run_triage.py": PARSER})
    errors = check("x.md", "scripts/gone.py and scripts/run_triage.py --bogus", repo)
    assert any("scripts/gone.py" in e and "does not exist" in e for e in errors)
    assert any("--bogus" in e for e in errors)
    assert check("x.md", "scripts/run_triage.py --file x --help", repo) == []


@pytest.mark.passes_on_main
def test_bare_names_must_resolve_to_exactly_one_file(tmp_path):
    repo = _repo(
        tmp_path,
        {
            "scripts/a.py": PARSER,
            "ci/a.py": PARSER,
            "scripts/sub/b.py": PARSER,
            "scripts/c.py": PARSER,
        },
    )
    assert "ambiguous" in check("x.md", "a.py --file x", repo)[0]
    assert "not under" in check("x.md", "missing.py --file x", repo)[0]
    assert check("x.md", "b.py --file x", repo) == []
    assert "--nope" in check("x.md", "scripts/sub/b.py --nope", repo)[0]
    assert check("x.md", "mention of main.py without flags", repo) == []


@pytest.mark.passes_on_main
def test_defined_flags_read_add_argument_statically():
    main, subs = parser_scopes("scripts/run_assessment.py")
    assert "--file" in main and "--help" in main and "--no-such-flag" not in main and subs == ()


@pytest.mark.passes_on_main
def test_boolean_optional_action_negates_only_its_own_flags(tmp_path):
    body = (
        PARSER + "p.add_argument('--ui', action=argparse.BooleanOptionalAction)\n"
        "p.add_argument('--all', action='store_true')\n"
    )
    repo = _repo(tmp_path, {"s.py": body})
    main, _ = parser_scopes("s.py", repo)
    assert {"--ui", "--no-ui", "--file", "--all"} <= main
    assert not {"--no-file", "--no-all", "--no-help"} & main


@pytest.mark.passes_on_main
def test_flags_are_scoped_per_subparser(tmp_path):
    body = (
        "import argparse\np = argparse.ArgumentParser()\np.add_argument('--common')\n"
        "sub = p.add_subparsers()\na = sub.add_parser('a')\na.add_argument('--only-a')\n"
        "b = sub.add_parser('b')\nb.add_argument('--only-b')\n"
    )
    repo = _repo(tmp_path, {"scripts/s.py": body})
    assert unknown_flags("scripts/s.py", ("--common", "--only-a"), repo) == []
    assert unknown_flags("scripts/s.py", ("--only-b",), repo) == []
    assert unknown_flags("scripts/s.py", ("--only-a", "--only-b"), repo) != []


# --- the docs ----------------------------------------------------------------


@pytest.mark.parametrize("doc", doc_files(), ids=lambda p: p.relative_to(REPO).as_posix())
def test_docs_name_only_existing_scripts_and_flags(doc: Path):
    rel = doc.relative_to(REPO).as_posix()
    assert check(rel, doc.read_text()) == []
