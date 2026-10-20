#!/usr/bin/env python3
"""Which of a branch's new and changed tests pass on main's code (#1155).

AGENTS.md's self-review item 1: every new test fails on main. ``ci/pre_merge.sh``
checks it: it lists the test functions this branch adds or changes, runs them in a
temporary worktree of main with the branch's ``tests/`` tree, and reports the ones
that pass there. A test that passes on main's code does not show the change works;
either it misses the changed path, or it guards behaviour main already has. Mark the
second kind with a ``@pytest.mark.passes_on_main`` decorator on the test or its class
and it is not run; it is listed by name as marked. A module-level ``pytestmark``
with that marker is not honoured (one line would exempt a whole new file): its tests
run as usual and the file is reported.

Standard library plus ``defusedxml`` (the dev extra) for the junit reports.
Subcommands (print to stdout):

* ``changed BASE_REF``: one line per new or changed test function, by comparing
  each changed ``tests/**/test_*.py`` file with its version at the merge base (a
  renamed file with the old path's version): ``run <node id>``, ``marked <node
  id>``, ``skip <path> <reason>`` (``tests/e2e`` and ``tests/integration`` need the
  whole pipeline or a live stack, so they are not run) or ``badmark <path>`` (a
  module-level ``passes_on_main``, not honoured).
* ``report JUNIT_DIR NODE_ID...``: one ``<outcome> <node id>`` line per node id
  from the pytest ``--junitxml`` reports (``*.xml``) in JUNIT_DIR. ``pass``: every
  case passed on main; ``noimport``: its file does not import on main;
  ``skipped``: a case was skipped on main and none failed; ``fail``: anything else (a failure, an error, or
  not collected).
"""

from __future__ import annotations

import ast
import subprocess  # nosec B404 -- runs git with fixed argv
import sys
from pathlib import Path, PurePosixPath

from defusedxml import ElementTree as ET

MARKER = "passes_on_main"
NOT_RUN = {"tests/e2e": "end-to-end suite", "tests/integration": "needs a live stack"}
_FUNCS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _segment(lines: list[str], node: ast.AST) -> str:
    decorators = getattr(node, "decorator_list", [])
    start = min([node.lineno] + [d.lineno for d in decorators])  # type: ignore[attr-defined]
    return "\n".join(lines[start - 1 : node.end_lineno])  # type: ignore[attr-defined]


def _is_marker(expr: ast.expr) -> bool:
    """``pytest.mark.passes_on_main`` or a call of it (``(...)``), nothing else."""
    if isinstance(expr, ast.Call):
        expr = expr.func
    return (
        isinstance(expr, ast.Attribute)
        and expr.attr == MARKER
        and isinstance(expr.value, ast.Attribute)
        and expr.value.attr == "mark"
        and isinstance(expr.value.value, ast.Name)
        and expr.value.value.id == "pytest"
    )


def _marked(node: ast.AST) -> bool:
    return any(_is_marker(d) for d in getattr(node, "decorator_list", []))


def module_marked(source: str) -> bool:
    """Whether a module-level ``pytestmark`` carries the marker (not honoured)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    for node in tree.body:
        targets = node.targets if isinstance(node, ast.Assign) else []
        if any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in targets):
            assert isinstance(node, ast.Assign)  # nosec B101 -- narrowed above
            values = (
                node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
            )
            if any(_is_marker(v) for v in values):
                return True
    return False


def _collect(
    body: list[ast.stmt], lines: list[str], prefix: str, marked: bool, found: dict
) -> None:
    for node in body:
        if isinstance(node, _FUNCS) and node.name.startswith("test"):
            found[prefix + node.name] = (_segment(lines, node), marked or _marked(node))
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            _collect(node.body, lines, f"{prefix}{node.name}::", marked or _marked(node), found)


def functions_in(source: str) -> dict[str, tuple[str, bool]]:
    """``{"test_x", "TestC::test_x" or "TestA::TestB::test_x": (source, marked)}``.

    ``source`` includes the decorators, so a changed parametrize list counts as a
    changed test. ``marked`` is whether a ``passes_on_main`` decorator applies to it
    (on the function or an enclosing class).
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    found: dict[str, tuple[str, bool]] = {}
    _collect(tree.body, source.splitlines(), "", False, found)
    return found


def changed_tests(path: str, base: str | None, head: str) -> list[tuple[str, str]]:
    """``(action, node id)`` for each test in ``head`` that is new or differs from
    ``base`` (``None`` when the file is new); ``action`` is ``run`` or ``marked``."""
    before = functions_in(base) if base is not None else {}
    out: list[tuple[str, str]] = []
    for name, (text, marked) in functions_in(head).items():
        if name in before and before[name][0] == text:
            continue
        out.append(("marked" if marked else "run", f"{path}::{name}"))
    return out


def is_test_file(path: str) -> bool:
    p = PurePosixPath(path)
    return p.parts[:1] == ("tests",) and p.name.startswith("test_") and p.suffix == ".py"


def not_run_reason(path: str) -> str | None:
    for prefix, reason in NOT_RUN.items():
        if path == prefix or path.startswith(prefix + "/"):
            return reason
    return None


def parse_name_status(output: str) -> list[tuple[str, str | None]]:
    """``(new path, old path)`` from ``git diff --name-status -M``; ``old`` is the
    same path for a modification, the source of a rename, ``None`` for an add."""
    out: list[tuple[str, str | None]] = []
    for line in output.splitlines():
        parts = line.split("\t")
        status = parts[0][:1]
        if status == "A" and len(parts) == 2:
            out.append((parts[1], None))
        elif status == "M" and len(parts) == 2:
            out.append((parts[1], parts[1]))
        elif status in ("R", "C") and len(parts) == 3:
            out.append((parts[2], parts[1]))
    return out


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", *args], capture_output=True, text=True, check=False
    )


def _show(ref: str, path: str) -> str | None:
    proc = _git("show", f"{ref}:{path}")
    return proc.stdout if proc.returncode == 0 else None


def _changed(base_ref: str) -> int:
    proc = _git("merge-base", "HEAD", base_ref)
    if proc.returncode != 0:
        print(f"error: no merge base between HEAD and {base_ref}", file=sys.stderr)
        return 2
    merge_base = proc.stdout.strip()
    diff = _git(
        "diff", "--name-status", "-M", "--diff-filter=AMRC", merge_base, "HEAD", "--", "tests"
    )
    for path, old in parse_name_status(diff.stdout):
        if not is_test_file(path):
            continue
        reason = not_run_reason(path)
        if reason is not None:
            print(f"skip {path} {reason}")
            continue
        head = _show("HEAD", path)
        if head is None:
            continue
        if module_marked(head):
            print(f"badmark {path}")
        base = _show(merge_base, old) if old is not None else None
        for action, node in changed_tests(path, base, head):
            print(f"{action} {node}")
    return 0


def _junit_key(node_id: str) -> tuple[str, str]:
    """The ``(classname, name)`` pytest's junit report gives a node id's cases."""
    path, _, rest = node_id.partition("::")
    parts = rest.split("::")
    module = path.removesuffix(".py").replace("/", ".")
    return ".".join([module, *parts[:-1]]), parts[-1]


def outcomes(reports: list[str], node_ids: list[str]) -> list[tuple[str, str]]:
    """``(outcome, node id)`` for each of ``node_ids``; see the module docstring."""
    cases: dict[tuple[str, str], list[str]] = {}
    broken: set[str] = set()  # modules whose collection failed
    for report in reports:
        for case in ET.fromstring(report).iter("testcase"):
            tags = {child.tag for child in case}
            if not case.get("classname") and "error" in tags:
                broken.add(case.get("name", ""))
                continue
            key = (case.get("classname", ""), case.get("name", "").split("[", 1)[0])
            state = (
                "fail"
                if tags & {"failure", "error"}
                else "skipped" if "skipped" in tags else "pass"
            )
            cases.setdefault(key, []).append(state)
    out: list[tuple[str, str]] = []
    for node in node_ids:
        module = node.partition("::")[0].removesuffix(".py").replace("/", ".")
        got = cases.get(_junit_key(node))
        if module in broken and not got:
            out.append(("noimport", node))
        elif not got or "fail" in got:
            out.append(("fail", node))
        elif all(state == "pass" for state in got):
            out.append(("pass", node))
        else:
            out.append(("skipped", node))  # some or all cases skipped on main
    return out


def main(argv: list[str]) -> int:
    cmd, rest = (argv[0], argv[1:]) if argv else ("", [])
    if cmd == "changed" and len(rest) == 1:
        return _changed(rest[0])
    if cmd == "report" and rest:
        reports = [p.read_text(encoding="utf-8") for p in sorted(Path(rest[0]).glob("*.xml"))]
        for outcome, node in outcomes(reports, rest[1:]):
            print(f"{outcome} {node}")
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
