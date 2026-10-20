#!/usr/bin/env python3
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

    uv run python scripts/check_version_pins.py [--keep]

It reads committed HEAD: commit an edit before checking it. Exit 0 when every such
test passes after the simulated bump, 1 when a test fails there (it pins a version),
2 when the check could not run, including a collection or setup error in a test.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess  # nosec B404 -- runs git, the snapshot scripts and pytest with fixed argv
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

# A test file that mentions any of these may pin a version.
MENTIONS = re.compile(r"contract_version|_VERSION\b|current_version")
# Suites the check does not run: they need the whole pipeline, a live stack or an SDK.
SKIP_DIRS = ("tests/e2e/", "tests/integration/", "tests/unit/atx_orchestrator/")
SIMULATED_REASON = "Simulated bump (scripts/check_version_pins.py)."


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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="keep the temporary worktree")
    parser.add_argument("-n", default="4", help="pytest-xdist workers (default 4)")
    args = parser.parse_args(argv)

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
        tests = candidate_tests(tree)
        result = _run(
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
                args.n,
                *tests,
            ],
            tree,
            env,
        )
        failed, errors = parse_outcomes(result.stdout)
        print(f"bumped {bump.stdout.strip()} contracts by one MINOR; ran {len(tests)} test files")
        if errors or result.returncode not in (0, 1):
            sys.stdout.write(result.stdout[-3000:])
            print(
                f"error: pytest could not run every test (exit {result.returncode}); a "
                "collection or setup error is not a version pin. Fix it, then rerun:",
                file=sys.stderr,
            )
            for node in errors:
                print(f"  {node}", file=sys.stderr)
            return 2
        if not failed:
            print("ok: no test pins a contract's current version")
            return 0
        print(
            f"error: {len(failed)} test(s) fail once every contract moves one MINOR, as "
            "resolve_contract_versions.py moves them: they pin a version. Compare with "
            "current_version(Model) from src.shared.contract_versioning instead "
            "(the check reads committed HEAD; commit your edits before rerunning):"
        )
        for node in failed:
            print(f"  {node}")
        return 1
    finally:
        if args.keep:
            print(f"kept {tree}")
        else:
            _run(["git", "worktree", "remove", "--force", str(tree)], REPO)
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
