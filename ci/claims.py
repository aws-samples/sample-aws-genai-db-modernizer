#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Claim markers: a behaviour sentence in the docs names the test that proves it (#1851).

Review's most common finding was a sentence in the docs, an ADR or a PR body that says
more than the code does. A behaviour sentence in a Markdown page can carry a marker, an
HTML comment right after the sentence, naming the pytest node id of its test::

    The check fails a contradicting verdict. <!-- claim: tests/unit/ci/test_pr_evidence.py::test_contradicting_verdict_fails -->

Several ids go in one marker, separated by commas or spaces. An HTML comment renders
as nothing on GitHub and on the help site, and a pytest node id is what ``pytest`` runs,
so ``--run`` passes the ids straight to it. A marker inside a fenced code block or an
inline code span is an example and is not read.

What ``check`` proves, statically (nothing is imported or run): every marker's node id
names a test pytest would collect and run. The file exists under the repo, is under one of
the ``testpaths`` in ``pyproject.toml`` and matches its ``python_files`` (pytest's defaults
when they are not set), and parses; each ``::`` part is a class (``Test*``) or a function
(``test*``) defined at that level, read from the AST, and the last part is one of them; and
neither the test, an enclosing class, a class's ``pytestmark`` nor the module's
(plain or annotated) carries a ``skip``, ``skipif`` or ``xfail`` mark (``pytest.mark.<m>``,
or ``mark.<m>`` after ``from pytest import mark``), since a test that does not run or is
expected to fail proves nothing. A UI test file with a ``describe.skip`` or ``xdescribe``
suite is refused. A parametrize id (``[...]``) is refused: it cannot be
checked without collecting, so name the test, which runs every case.

What it does not see: a test defined by a factory or inherited from a base class (name
the class that defines it), a ``pytest.skip()`` or ``pytest.xfail()`` call inside a test,
a skip added by a fixture or a ``conftest.py`` hook, a mark bound to another name
(``skip = pytest.mark.skip``), a skipped single Jest case (``it.skip``, ``xit``), and the
``python_classes`` and
``python_functions`` settings (the defaults, ``Test*`` and ``test*``, are assumed). A
comment that looks like a marker but is misspelt (``<!-- Claim: ...``, ``<!-- claims:``,
``<!--claim :``) is an error, so a typo cannot leave a sentence silently unproven. It does not prove that the test asserts what the sentence says: the reviewer
reads that, from the PR's Claims table. ``--run`` also runs the marked tests.

Subcommands (standard library only, so ``ci/pr_evidence.py`` can import it)::

    uv run python ci/claims.py check [--run] [FILE ...]   # default: every tracked *.md
    uv run python ci/claims.py list [FILE ...]            # page:line node id, one per line

``check`` exits 1 when a marker is malformed or names a missing test (``--run``: or a
marked test fails), 2 when the tracked files cannot be listed. ``ci/lint.sh`` and the
``claim-markers`` pre-commit hook run ``check``.
"""

from __future__ import annotations

import argparse
import ast
import fnmatch
import re
import subprocess  # nosec B404 -- runs git and pytest with fixed argv
import sys
from dataclasses import dataclass
from functools import cache
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[1]
MARKER = re.compile(r"<!--\s*claim:(.*?)-->", re.DOTALL)
_LOOSE_MARKER = re.compile(r"<!--\s*claims?\s*:", re.IGNORECASE)
_EXACT_START = re.compile(r"<!--\s*claim:")
_SKIP_MARKS = {"skip", "skipif", "xfail"}
_DEFAULT_PYTHON_FILES = ("test_*.py", "*_test.py")
_FENCE = re.compile(r"^[ \t]{0,3}(```+|~~~+)")
_CODE_SPAN = re.compile(r"(`+)(?!`).+?(?<!`)\1(?!`)")
_NODE_ID = re.compile(r"^([A-Za-z0-9_./-]+\.py)((?:::[A-Za-z_][A-Za-z0-9_]*)+)(\[[^\]]*\])?$")
_JS_TEST = re.compile(r"^[A-Za-z0-9_./-]+\.test\.(?:js|jsx|mjs|ts|tsx)$")
_JS_CASE = re.compile(r"\b(?:test|it)\s*\(")
_JS_SKIPPED_SUITE = re.compile(r"\b(?:describe\.skip|xdescribe)\s*\(")
_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


@dataclass(frozen=True)
class Marker:
    page: str
    line: int
    node_id: str


def _blank_code(text: str) -> str:
    """The text with fenced code blocks and inline code spans blanked, line count kept."""
    out: list[str] = []
    fence: str | None = None
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        m = _FENCE.match(line)
        if fence is not None:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence):
                fence = None
            out.append("")
            continue
        if m:
            fence = m.group(1)
            out.append("")
            continue
        out.append(_CODE_SPAN.sub(lambda s: " " * len(s.group(0)), line))
    return "\n".join(out)


def markers(text: str, page: str = "<text>") -> list[Marker]:
    """Every node id named by a claim marker in ``text`` (code blocks and spans skipped).

    A marker with no id yields a ``Marker`` whose ``node_id`` is empty, so ``check``
    reports it."""
    visible = _blank_code(text)
    found: list[Marker] = []
    for m in MARKER.finditer(visible):
        line = visible.count("\n", 0, m.start()) + 1
        ids = [p for p in re.split(r"[\s,]+", m.group(1)) if p]
        found += [Marker(page, line, i) for i in ids] or [Marker(page, line, "")]
    return found


def near_misses(text: str, page: str = "<text>") -> list[str]:
    """``page:line`` errors for comments that look like a claim marker but are not one."""
    visible = _blank_code(text)
    errors = []
    for m in _LOOSE_MARKER.finditer(visible):
        if not _EXACT_START.match(visible, m.start()):
            line = visible.count("\n", 0, m.start()) + 1
            errors.append(
                f"{page}:{line}: {m.group(0)!r} is not a claim marker; write '<!-- claim: ...'"
            )
    return errors


@cache
def _pytest_config(root: Path) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """``(testpaths, python_files)`` from ``root/pyproject.toml`` (pytest's defaults when
    unset: the root itself, and ``test_*.py`` / ``*_test.py``)."""
    try:
        import tomllib

        data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    ini = data.get("tool", {}).get("pytest", {}).get("ini_options", {})
    files = ini.get("python_files", list(_DEFAULT_PYTHON_FILES))
    files = files.split() if isinstance(files, str) else files
    return tuple(ini.get("testpaths", ["."])), tuple(files)


def _is_skip_mark(expr: ast.expr) -> str | None:
    """``skip``/``skipif``/``xfail`` for ``pytest.mark.<that>`` (called or not)."""
    if isinstance(expr, ast.Call):
        expr = expr.func
    if (
        isinstance(expr, ast.Attribute)
        and expr.attr in _SKIP_MARKS
        and (
            (isinstance(expr.value, ast.Attribute) and expr.value.attr == "mark")
            or (isinstance(expr.value, ast.Name) and expr.value.id == "mark")
        )
    ):
        return expr.attr  # pytest.mark.skip, or mark.skip after ``from pytest import mark``
    return None


def _pytestmark_skip(body: list[ast.stmt]) -> str | None:
    """A skip mark in a ``pytestmark = ...`` (or annotated ``pytestmark: T = ...``) of a
    module or class body."""
    for node in body:
        if isinstance(node, ast.Assign):
            named = any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets)
            value: ast.expr | None = node.value
        elif isinstance(node, ast.AnnAssign):
            named = isinstance(node.target, ast.Name) and node.target.id == "pytestmark"
            value = node.value
        else:
            continue
        if not named or value is None:
            continue
        values = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
        for v in values:
            if mark := _is_skip_mark(v):
                return mark
    return None


@cache
def _tree(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return None


def resolve(node_id: str, root: Path = REPO) -> str | None:
    """``None`` when ``node_id`` names a collectable test under ``root``, else why not."""
    if _JS_TEST.match(node_id):
        return _resolve_js(node_id, root)
    m = _NODE_ID.match(node_id)
    if not m:
        return "not a pytest node id (path/to/test_file.py::test_name)"
    rel = PurePosixPath(m.group(1))
    if rel.is_absolute() or ".." in rel.parts:
        return "the path must be repo-relative, with no '..'"
    if m.group(3):
        return (
            f"the parametrize id {m.group(3)} cannot be checked without running pytest; "
            "name the test, which runs every case"
        )
    path = root / rel
    if not path.is_file():
        return f"{rel} does not exist"
    testpaths, python_files = _pytest_config(root)
    if not any(
        p in (".", "") or rel.parts[: len(PurePosixPath(p).parts)] == PurePosixPath(p).parts
        for p in testpaths
    ):
        return f"{rel} is not under pytest's testpaths ({', '.join(testpaths)})"
    if not any(fnmatch.fnmatchcase(rel.name, pat) for pat in python_files):
        return f"pytest does not collect {rel.name} (python_files: {', '.join(python_files)})"
    tree = _tree(path)
    if tree is None:
        return f"{rel} does not parse"
    if mark := _pytestmark_skip(tree.body):
        return f"{rel}'s pytestmark carries pytest.mark.{mark}: a test that does not run proves nothing"
    parts = m.group(2).split("::")[1:]
    body: list[ast.stmt] = tree.body
    for i, name in enumerate(parts):
        node = next((n for n in body if isinstance(n, _DEFS) and n.name == name), None)
        if node is None:
            return f"{rel} defines no {name!r} there" if i else f"{rel} defines no {name!r}"
        last = i == len(parts) - 1
        mark = next((k for d in node.decorator_list if (k := _is_skip_mark(d))), None)
        if mark:
            return f"{name!r} carries pytest.mark.{mark}: a test that does not run proves nothing"
        if isinstance(node, ast.ClassDef):
            if not name.startswith("Test"):
                return f"{name!r} is not a test class (Test*)"
            if mark := _pytestmark_skip(node.body):
                return (
                    f"{name!r}'s pytestmark carries pytest.mark.{mark}: a test that does not run "
                    "proves nothing"
                )
            body = node.body
        elif not last:
            return f"{name!r} is a function; nothing is collected inside it"
        elif not name.startswith("test"):
            return f"{name!r} is not a test function (test*)"
    return None


def _resolve_js(path_text: str, root: Path) -> str | None:
    """A UI test file (Jest has no node ids): it exists and defines a ``test(`` or ``it(``."""
    rel = PurePosixPath(path_text)
    if rel.is_absolute() or ".." in rel.parts:
        return "the path must be repo-relative, with no '..'"
    try:
        text = (root / rel).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return f"{rel} does not exist"
    if _JS_SKIPPED_SUITE.search(text):
        return (
            f"{rel} has a skipped suite (describe.skip or xdescribe); a marker names a whole "
            "file, so it cannot tell which cases run"
        )
    return None if _JS_CASE.search(text) else f"{rel} defines no test() or it() case"


def tracked_pages(root: Path = REPO) -> list[str]:
    """Every tracked Markdown file, repo-relative."""
    out = subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", "ls-files", "-z", "--", "*.md"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    # A set: during a merge an unmerged path is listed once per stage.
    return sorted({p for p in out.split("\0") if p and (root / p).is_file()})


def scan(pages: list[str], root: Path = REPO) -> list[Marker]:
    found: list[Marker] = []
    for page in pages:
        found += markers((root / page).read_text(encoding="utf-8"), page)
    return found


def scan_near_misses(pages: list[str], root: Path = REPO) -> list[str]:
    errors: list[str] = []
    for page in pages:
        errors += near_misses((root / page).read_text(encoding="utf-8"), page)
    return errors


def check(found: list[Marker], root: Path = REPO) -> list[str]:
    """One ``page:line: problem`` line per marker that names no test."""
    errors = []
    for mk in found:
        if not mk.node_id:
            errors.append(f"{mk.page}:{mk.line}: empty claim marker; name the test's node id")
            continue
        why = resolve(mk.node_id, root)
        if why:
            errors.append(f"{mk.page}:{mk.line}: claim {mk.node_id}: {why}")
    return errors


def impact_entries(found: list[Marker]) -> list[dict]:
    """Docs-impact entries derived from the markers: a branch that changes a cited test
    file re-checks every page whose markers cite it (``ci/pre_merge.py``)."""
    by_file: dict[str, set[str]] = {}
    for mk in found:
        if mk.node_id:
            by_file.setdefault(mk.node_id.split("::", 1)[0], set()).add(mk.page)
    return [
        {
            "name": f"claims:{test_file}",
            "paths": [test_file],
            "docs": sorted(pages),
            "why": "these pages carry claim markers that cite this test file",
        }
        for test_file, pages in sorted(by_file.items())
    ]


def pytest_argv(found: list[Marker]) -> list[str]:
    """The ``--run`` command: each distinct pytest node id once, in first-seen order (UI
    test files are not run here; ``npm test`` in ``src/ui/`` runs them)."""
    ids = list(
        dict.fromkeys(mk.node_id for mk in found if mk.node_id.split("::")[0].endswith(".py"))
    )
    return [sys.executable, "-m", "pytest", "-p", "no:xdist", "-q", *ids]


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0] if __doc__ else None)
    ap.add_argument("--root", default=str(REPO), help=argparse.SUPPRESS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "list"):
        p = sub.add_parser(name)
        p.add_argument("files", nargs="*")
        if name == "check":
            p.add_argument("--run", action="store_true", help="also run the marked tests")
    args = ap.parse_args(argv)
    root = Path(args.root)
    try:
        pages = args.files or tracked_pages(root)
    except (OSError, subprocess.CalledProcessError) as exc:
        print(f"error: cannot list the tracked pages: {exc}", file=sys.stderr)
        return 2
    found = scan(pages, root)
    if args.cmd == "list":
        for mk in found:
            print(f"{mk.page}:{mk.line} {mk.node_id}")
        return 0
    errors = scan_near_misses(pages, root) + check(found, root)
    for err in errors:
        print(err)
    if errors:
        print(f"claims: {len(errors)} problem(s); fix the marker, the test id or the sentence.")
        return 1
    pages_seen = len({m.page for m in found})
    print(
        f"claims: {len(found)} test reference(s) on {pages_seen} page(s), all resolve.", flush=True
    )
    if args.run and found:
        return subprocess.run(
            pytest_argv(found), cwd=root, check=False
        ).returncode  # nosec B603 -- fixed argv: the interpreter, pytest and node ids
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
