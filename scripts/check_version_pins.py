#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Fail when a test pins a contract's current version (#1158 follow-up).

``scripts/resolve_contract_versions.py`` moves a contract's version after the last
rebase. A test that compares a written contract with a literal (``== "1.44"``)
then fails on the resolved branch, after review, and needs a hand edit. Tests
compare with ``current_version(Model)`` (``src/shared/contract_versioning.py``)
instead.

This check proves it the direct way. In a temporary worktree of HEAD it bumps
every contract the resolver can edit by one MINOR, exactly as the resolver does
(new default, Version History entry, both snapshots regenerated), then runs every
test file that mentions a contract version. A test that fails there pins a
version. The ``validate-contracts`` job runs it on every pull request::

    uv run python scripts/check_version_pins.py [--base REF] [--keep]

With ``--base REF`` (``ci/pre_merge.sh --quick``, #1845) it runs only the test files
the branch changed since its merge base with REF, and those that name a contract
module the branch changed (``src/contracts/<module>.py``, or a pending bump of one) or
a contract module that imports one, transitively. A change it cannot place runs every
file: ``src/contracts/__init__.py``, a file under ``tests/`` that is not a
``test_*.py``, and Python code under ``src/`` or ``scripts/`` outside the contract
modules (the check's own machinery included). A test that fails after the bump runs
again on HEAD as committed: one that fails there too is reported apart, as "fails
without the bump too", since it is broken, not pinned.

It reads committed HEAD: commit an edit before checking it. Exit 0 when every such
test passes after the simulated bump, 1 when a test fails only under the bump (it
pins a version), 2 when the check could not run or could not decide, including a
collection or setup error and a test that fails without the bump too.
"""

from __future__ import annotations

import argparse
import ast
import os
import re
import shutil
import subprocess  # nosec B404 -- runs git, the snapshot scripts and pytest with fixed argv
import sys
import tempfile
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# A test file that mentions any of these may pin a version.
MENTIONS = re.compile(r"contract_version|_VERSION\b|current_version")
# Suites the check does not run: they need the whole pipeline, a live stack or an SDK.
SKIP_DIRS = ("tests/e2e/", "tests/integration/", "tests/unit/atx_orchestrator/")
SIMULATED_REASON = "Simulated bump (scripts/check_version_pins.py)."
_CONTRACT_MODULE = re.compile(r"src/contracts/([A-Za-z_][A-Za-z0-9_]*)\.py")
_PENDING_FILE = re.compile(r"src/contracts/pending/[^/]+\.toml")


def bump_everything(root: Path, reason: str = SIMULATED_REASON) -> list[str]:
    """Bump every contract the resolver can edit under ``root``; return their keys."""
    from scripts.contract_bumps import PendingBump
    from scripts.contract_schema_snapshot import versioned_contract_models
    from scripts.resolve_contract_versions import resolve_texts

    contracts = root / "src" / "contracts"
    bumps = [
        PendingBump(name=f"{key}.sim.toml", contract=key, kind="minor", reason=reason)
        for key, _ in versioned_contract_models()
    ]

    def read(module: str) -> str:
        return (contracts / f"{module}.py").read_text(encoding="utf-8")

    texts, done, _ = resolve_texts(bumps, read, read, "2099-01-01")
    for module, text in texts.items():
        (contracts / f"{module}.py").write_text(text, encoding="utf-8")
    return [item.contract for item in done]


def candidate_tests(root: Path) -> list[str]:
    found = []
    for path in sorted((root / "tests").rglob("test_*.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith(SKIP_DIRS):
            continue
        if MENTIONS.search(path.read_text(encoding="utf-8", errors="replace")):
            found.append(rel)
    return found


def touched_modules(root: Path, changed: list[str]) -> set[str]:
    """Contract modules the branch changed: ``src/contracts/<module>.py``, or the module
    of the contract a pending bump under ``root`` names (a deleted one names nothing)."""
    modules: set[str] = set()
    for rel in changed:
        if match := _CONTRACT_MODULE.fullmatch(rel):
            modules.add(match.group(1))
        elif _PENDING_FILE.fullmatch(rel) and (root / rel).is_file():
            try:
                contract = tomllib.loads((root / rel).read_text(encoding="utf-8")).get("contract")
            except tomllib.TOMLDecodeError:
                continue
            if isinstance(contract, str) and contract:
                modules.add(contract.split(".")[0])
    modules.discard("__init__")
    return modules


def contract_imports(root: Path) -> dict[str, set[str]]:
    """``module -> the src/contracts modules it imports``, read with ``ast`` (nothing is
    imported). Counts ``import src.contracts.m``, ``from src.contracts.m import ...``,
    ``from src.contracts import m``, and the relative forms, anywhere in the file."""
    folder = root / "src" / "contracts"
    names = {p.stem for p in folder.glob("*.py")} - {"__init__"}
    graph: dict[str, set[str]] = {}
    for module in sorted(names):
        found: set[str] = set()
        try:
            tree = ast.parse((folder / f"{module}.py").read_text(encoding="utf-8"))
        except (OSError, SyntaxError, ValueError):
            graph[module] = found
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    head, _, tail = alias.name.partition("src.contracts.")
                    if not head and tail:
                        found.add(tail.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                base = node.module or ""
                if node.level == 0 and base.startswith("src.contracts."):
                    found.add(base.removeprefix("src.contracts.").split(".")[0])
                elif node.level == 1 and base:
                    found.add(base.split(".")[0])
                elif (node.level == 0 and base == "src.contracts") or (
                    node.level == 1 and not base
                ):
                    found.update(alias.name for alias in node.names)
        graph[module] = (found & names) - {module}
    return graph


def with_importers(modules: set[str], graph: dict[str, set[str]]) -> set[str]:
    """``modules`` and every contract module that imports one of them, transitively."""
    result = set(modules)
    grew = True
    while grew:
        grew = False
        for module, imported in graph.items():
            if module not in result and imported & result:
                result.add(module)
                grew = True
    return result


def runs_everything(rel: str) -> bool:
    """A change ``--base`` cannot place, so it runs every candidate (round 1 of #1856):
    ``src/contracts/__init__.py``; a file under ``tests/`` that is not a ``test_*.py`` (a
    conftest, a helper, a fixture), since any test may load it; and Python code under
    ``src/`` or ``scripts/`` outside the contract modules, since a writer that sets a
    version reaches tests through any number of calls that a name match cannot follow.
    The check's own machinery (the resolver, ``contract_bumps.py``, the snapshot scripts,
    ``src/shared/contract_versioning.py``, this script) falls under that last rule."""
    if rel == "src/contracts/__init__.py":  # re-exports: any contract
        return True
    name = Path(rel).name
    if rel.startswith("tests/"):
        return not (name.startswith("test_") and name.endswith(".py"))
    if rel.endswith(".py") and rel.startswith(("src/", "scripts/")):
        return not _CONTRACT_MODULE.fullmatch(rel)
    return False


def select_tests(root: Path, changed: list[str]) -> list[str]:
    """The candidate tests under ``root`` that ``--base`` runs (#1845): those the branch
    changed, and those whose text names a contract module it touched or one that imports
    such a module, transitively. Every candidate when a change is one ``runs_everything``
    cannot place."""
    candidates = candidate_tests(root)
    if any(runs_everything(rel) for rel in changed):
        return candidates
    changed_set = set(changed)
    touched = touched_modules(root, changed)
    modules = with_importers(touched, contract_imports(root)) if touched else set()
    names = [re.compile(rf"\b{re.escape(m)}\b") for m in sorted(modules)]
    chosen = []
    for rel in candidates:
        if rel in changed_set:
            chosen.append(rel)
        elif names:
            text = (root / rel).read_text(encoding="utf-8", errors="replace")
            if any(name.search(text) for name in names):
                chosen.append(rel)
    return chosen


def split_failures(bumped: list[str], plain: list[str]) -> tuple[list[str], list[str]]:
    """``(pins, broken)``: of the tests that failed after the bump, those that pass on
    HEAD as committed pin a version; those that fail there too are broken, not pinned."""
    plain_set = set(plain)
    return (
        [node for node in bumped if node not in plain_set],
        [node for node in bumped if node in plain_set],
    )


def might_touch(changed: list[str]) -> bool:
    """Cheap pre-check before a worktree: could ``select_tests`` pick anything?"""
    return any(
        runs_everything(rel)
        or _CONTRACT_MODULE.fullmatch(rel)
        or _PENDING_FILE.fullmatch(rel)
        or (rel.startswith("tests/") and Path(rel).name.startswith("test_"))
        for rel in changed
    )


def parse_outcomes(output: str) -> tuple[list[str], list[str]]:
    """``(failed, errors)`` node ids from pytest's short summary: a ``FAILED`` test
    pins a version; an ``ERROR`` (collection or setup) is a problem of its own."""
    failed: set[str] = set()
    errors: set[str] = set()
    for line in output.splitlines():
        for prefix, into in (("FAILED ", failed), ("ERROR ", errors)):
            if line.startswith(prefix):
                into.add(line[len(prefix) :].split(" - ")[0])
    return sorted(failed), sorted(errors)


def _run(
    cmd: list[str], cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 -- fixed argv
        cmd, cwd=cwd, env=env, capture_output=True, text=True, check=False
    )


def _pytest(tree: Path, env: dict[str, str], workers: str, args: list[str]):
    return _run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
            "-n",
            workers,
            *args,
        ],
        tree,
        env,
    )


def _could_not_run(result: subprocess.CompletedProcess[str], errors: list[str]) -> int:
    sys.stdout.write(result.stdout[-3000:])
    print(
        f"error: pytest could not run every test (exit {result.returncode}); a "
        "collection or setup error is not a version pin. Fix it, then rerun:",
        file=sys.stderr,
    )
    for node in errors:
        print(f"  {node}", file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="keep the temporary worktree")
    parser.add_argument("-n", default="4", help="pytest-xdist workers (default 4)")
    parser.add_argument(
        "--base",
        metavar="REF",
        help="only the tests the branch changed since its merge base with REF, and those "
        "naming a contract module it changed (#1845)",
    )
    args = parser.parse_args(argv)

    changed: list[str] | None = None
    if args.base:
        diff = _run(["git", "diff", "--name-only", f"{args.base}...HEAD"], REPO)
        if diff.returncode:
            sys.stderr.write(diff.stderr)
            print(f"error: could not list the files changed since {args.base}", file=sys.stderr)
            return 2
        changed = [line for line in diff.stdout.splitlines() if line]
        if not might_touch(changed):
            print(f"ok: no changed test file or contract module since {args.base}")
            return 0

    tmp = Path(tempfile.mkdtemp(prefix="version-pins-"))
    tree = tmp / "tree"
    if _run(["git", "worktree", "add", "-q", "--detach", str(tree), "HEAD"], REPO).returncode:
        shutil.rmtree(tmp, ignore_errors=True)
        print("error: could not make a worktree of HEAD", file=sys.stderr)
        return 2
    try:
        env = {**os.environ, "PYTHONPATH": str(tree)}
        probe = _run(
            [sys.executable, "-c", "import os, src; print(os.path.realpath(src.__path__[0]))"],
            tree,
            env,
        )
        if probe.stdout.strip() != os.path.realpath(tree / "src"):
            print(
                f"error: src did not import from the worktree ({probe.stdout.strip()})",
                file=sys.stderr,
            )
            return 2
        tests = candidate_tests(tree) if changed is None else select_tests(tree, changed)
        if not tests:
            where = "" if changed is None else f" the branch touched since {args.base}"
            print(f"ok: no test file{where} mentions a contract version")
            return 0
        bump = _run(
            [
                sys.executable,
                "-c",
                "import sys; from scripts.check_version_pins import bump_everything;"
                " from pathlib import Path; print(len(bump_everything(Path('.'))))",
            ],
            tree,
            env,
        )
        if bump.returncode:
            sys.stderr.write(bump.stderr)
            return 2
        for script in ("scripts/contract_schema_snapshot.py", "scripts/contract_value_snapshot.py"):
            if _run([sys.executable, script, "--write"], tree, env).returncode:
                print(f"error: {script} --write failed in the worktree", file=sys.stderr)
                return 2
        result = _pytest(tree, env, args.n, tests)
        failed, errors = parse_outcomes(result.stdout)
        scope = "" if changed is None else f", picked by the branch's changes since {args.base}"
        print(
            f"bumped {bump.stdout.strip()} contracts by one MINOR; ran {len(tests)} test "
            f"file(s){scope}"
        )
        if errors or result.returncode not in (0, 1):
            return _could_not_run(result, errors)
        if not failed:
            print("ok: no test pins a contract's current version")
            return 0
        # Each failure again on HEAD as committed (#1845): one that fails there too is
        # broken on its own, not pinned to a version.
        if _run(["git", "reset", "-q", "--hard", "HEAD"], tree).returncode:
            print("error: could not undo the simulated bump in the worktree", file=sys.stderr)
            return 2
        plain = _pytest(tree, env, args.n, failed)
        plain_failed, plain_errors = parse_outcomes(plain.stdout)
        if plain.returncode not in (0, 1):
            return _could_not_run(plain, plain_errors)
        pins, broken = split_failures(failed, sorted({*plain_failed, *plain_errors}))
        if pins:
            print(
                f"error: {len(pins)} test(s) fail only under the bump, once every contract "
                "moves one MINOR as resolve_contract_versions.py moves them: they pin a "
                "version. Compare with current_version(Model) from "
                "src.shared.contract_versioning instead (the check reads committed HEAD; "
                "commit your edits before rerunning):"
            )
            for node in pins:
                print(f"  {node}")
        if broken:
            print(
                f"error: {len(broken)} test(s) fail without the bump too, on HEAD as "
                "committed: not a version pin; fix them first (ci/test.sh shows why):"
            )
            for node in broken:
                print(f"  {node}")
        return 1 if pins else 2
    finally:
        if args.keep:
            print(f"kept {tree}")
        else:
            _run(["git", "worktree", "remove", "--force", str(tree)], REPO)
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
