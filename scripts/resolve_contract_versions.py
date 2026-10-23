#!/usr/bin/env python3
"""Assign the numbers of pending contract version bumps (#1158, ADR-008 Decision 8).

Run after the last rebase onto main, before the pull request enters the merge queue:

    uv run python scripts/resolve_contract_versions.py [--base upstream/main]

``--base`` defaults to ``<remote>/main`` for the remote that points at the public
repository, the one ``ci/pre_merge.sh`` fetches (``upstream`` on a fork, else
``origin``). For every contract with a pending file in ``src/contracts/pending/`` it
takes main's version of that contract, moves it by the strongest declared kind
(several pending files for one contract make one bump), writes the new
``contract_version`` default and a Version History entry built from the pending
files' reasons and issues, deletes those pending files and regenerates
``tests/contract/schema_snapshot.json`` and ``tests/contract/value_set_snapshot.json``.
It refuses when HEAD does not contain ``--base`` (rebase first). A contract it
cannot resolve (one it cannot edit, one the branch also moved by hand, a pending
file whose bump already landed) is reported with what to do, and the others are
still resolved.

    uv run python scripts/resolve_contract_versions.py --check [--require-resolved] [--against REF]

only validates: every pending file names a known contract, a kind and a reason, the
resolver could edit its contract, and its bump has not already landed.
``--require-resolved`` also fails while any pending file is left, which is how the
merge queue keeps an unresolved bump off main. ``--against REF`` (the merge base)
also compares the contracts with REF's committed snapshots, so a shape or value
change with no version move and no pending bump fails even when the branch
regenerated its own snapshot (the ``validate-contracts`` job and ``ci/pre_merge.sh``),
and runs the dry run with REF as main, so a pending contract whose version the branch
also moved by hand fails there, and so does a pending file for a contract REF does not
have (a new contract starts at its own version); the snapshot tests do not compare a
pending contract's version (#1393). A REF git cannot read is an error of its own.
"""

from __future__ import annotations

import argparse
import datetime
import json
import subprocess  # nosec B404 -- runs git and the snapshot scripts with fixed argv
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.contract_bumps import (  # noqa: E402
    PENDING_DIR,
    RESOLVE_COMMAND,
    BumpError,
    PendingBump,
    already_recorded,
    bump_version,
    by_hand_steps,
    class_of,
    contract_default,
    has_version_constant,
    history_newest,
    insert_history_entry,
    load_pending,
    pending_kinds,
    set_contract_default,
    strongest,
)

SNAPSHOT_SCRIPTS = ("scripts/contract_schema_snapshot.py", "scripts/contract_value_snapshot.py")


@dataclass(frozen=True)
class Resolution:
    contract: str
    kind: str
    before: str
    after: str
    files: tuple[str, ...]  # the pending file names it resolves


def _version_of(source: str, contract: str) -> str:
    cls = class_of(contract)
    if cls is not None:
        return contract_default(source, cls)
    if has_version_constant(source):
        raise BumpError(f"{contract} keeps its version in a constant too; bump it by hand")
    newest = history_newest(source)
    if newest is None:
        raise BumpError(f"{contract} has no Version History to bump")
    return newest


@dataclass(frozen=True)
class Failure:
    contract: str
    files: tuple[str, ...]
    message: str


def _resolve_one(
    contract: str, group: list[PendingBump], text: str, base_text: str | None, when: str
) -> tuple[str, Resolution]:
    module = group[0].module
    if base_text is None:
        raise BumpError(
            f"src/contracts/{module}.py is not on main; a new contract starts at its own "
            "version and needs no pending bump"
        )
    try:
        base_version = _version_of(base_text, contract)
    except BumpError as exc:
        raise BumpError(f"not on main as a versioned contract ({exc})") from exc
    stale = [b.name for b in group if already_recorded(text, b.reason)]
    if stale:
        raise BumpError(
            f"the bump of {', '.join(stale)} already landed: its reason is in the Version "
            "History here (a stacked branch re-adds a pending file when it is rebased onto "
            "the resolved commit); delete that pending file"
        )
    head_version = _version_of(text, contract)
    if head_version != base_version:
        raise BumpError(
            f"this branch already moved the version by hand ({base_version} on main, "
            f"{head_version} here); keep either the pending file or the hand bump. If main "
            "is not what you rebased onto, pass --base"
        )
    kind = strongest(b.kind for b in group)
    new = bump_version(base_version, kind)
    issues = ", ".join(f"#{n}" for n in sorted({b.issue for b in group if b.issue}))
    reason = " ".join(b.reason.strip() for b in group)
    cls = class_of(contract)
    if cls is not None:
        text = set_contract_default(text, cls, new)
    text = insert_history_entry(text, cls, base_version, new, when, issues, reason)
    return text, Resolution(contract, kind, base_version, new, tuple(b.name for b in group))


def resolve_texts(
    bumps: list[PendingBump],
    read_head: Callable[[str], str],
    read_base: Callable[[str], str | None],
    when: str,
) -> tuple[dict[str, str], list[Resolution], list[Failure]]:
    """New module texts, what changed, and the contracts that could not be resolved.

    ``read_head(module)`` is the module's text on the branch, ``read_base(module)``
    its text on main (``None`` when main has no such module). A contract that cannot
    be resolved is reported and leaves the others alone. Pure: no files written.
    """
    by_contract: dict[str, list[PendingBump]] = {}
    for bump in bumps:
        by_contract.setdefault(bump.contract, []).append(bump)
    texts: dict[str, str] = {}
    done: list[Resolution] = []
    failed: list[Failure] = []
    for contract in sorted(by_contract):
        group = by_contract[contract]
        module = group[0].module
        try:
            text = texts.get(module) or read_head(module)
            new_text, resolution = _resolve_one(contract, group, text, read_base(module), when)
        except (BumpError, SyntaxError) as exc:
            failed.append(Failure(contract, tuple(b.name for b in group), str(exc)))
            continue
        texts[module] = new_text
        done.append(resolution)
    return texts, done, failed


def _failure_lines(failed: list[Failure]) -> list[str]:
    lines = []
    for item in failed:
        lines.append(f"error: {item.contract}: {item.message}")
        if "already landed" not in item.message and "by hand (" not in item.message:
            for name in item.files:
                lines.append(f"       {by_hand_steps(item.contract, name)}")
    return lines


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", *args], capture_output=True, text=True, check=False, cwd=REPO
    )


def default_base() -> str:
    """``<remote>/main`` for the remote that points at the public repository (the one
    ``ci/pre_merge.sh`` fetches): ``upstream`` on a fork, else ``origin``."""
    from ci.pre_merge import pick_remote

    return f"{pick_remote(_git('remote', '-v').stdout)}/main"


def _module_text(module: str) -> str:
    return (REPO / "src" / "contracts" / f"{module}.py").read_text(encoding="utf-8")


def _against(ref: str, pending: dict[str, str]) -> list[str]:
    """Shape and value changes since ``ref`` with no version move and no pending bump."""
    from scripts import contract_schema_snapshot as schema
    from scripts import contract_value_snapshot as values
    from scripts.contract_bumps import pending_module_kinds

    problems: list[str] = []
    for module, path, check in (
        (schema, "tests/contract/schema_snapshot.json", schema.evaluate_against_base),
        (values, "tests/contract/value_set_snapshot.json", values.evaluate_against_base),
    ):
        proc = _git("show", f"{ref}:{path}")
        if proc.returncode != 0:
            problems.append(f"{ref} has no {path} to compare with")
            continue
        kinds = pending if module is schema else pending_module_kinds(pending)
        problems.extend(check(json.loads(proc.stdout), module.build_snapshot(), kinds))
    return problems


def _check(require_resolved: bool, against: str | None) -> int:
    from scripts.contract_schema_snapshot import versioned_contract_models

    try:
        bumps = load_pending()
    except BumpError as exc:
        print(f"error: {exc}")
        return 1
    if against and _git("rev-parse", "--verify", "--quiet", f"{against}^{{commit}}").returncode:
        # Otherwise every module would read as "not on main" (an incomplete fetch).
        print(f"error: --against {against} is not a commit git can read here; fetch it")
        return 1
    keys = {key for key, _ in versioned_contract_models()}
    problems = [
        f"error: {b.name}: {b.contract} is not a versioned contract"
        for b in bumps
        if class_of(b.contract) is not None and b.contract not in keys
    ]
    known = [b for b in bumps if class_of(b.contract) is None or b.contract in keys]

    def read(module: str) -> str:
        path = REPO / "src" / "contracts" / f"{module}.py"
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def read_ref(module: str) -> str | None:
        proc = _git("show", f"{against}:src/contracts/{module}.py")
        return proc.stdout if proc.returncode == 0 else None

    # Dry run, as if main were REF (else this tree): it finds what the resolver would
    # refuse (a contract it cannot edit, a bump that already landed, and with REF a
    # version the branch also moved by hand) while the PR is open. The snapshot tests
    # leave a pending contract's version to this check (#1393).
    _, _, failed = resolve_texts(known, read, read_ref if against else read, "0000-00-00")
    problems += _failure_lines(failed)
    if against:
        problems += [f"error: {p}" for p in _against(against, pending_kinds(bumps))]
    for problem in problems:
        print(problem)
    if problems:
        return 1
    if not bumps:
        print("ok: no pending contract bumps")
    for bump in bumps:
        print(f"pending: {bump.contract} {bump.kind} ({PENDING_DIR.name}/{bump.name})")
    if against:
        print(f"ok: every contract change since {against} moves its version or has a pending bump")
    if bumps and require_resolved:
        print(
            f"error: {len(bumps)} pending contract bump(s); run {RESOLVE_COMMAND} after the "
            "last rebase and push the result before the merge queue (ADR-008 Decision 8)"
        )
        return 1
    return 0


def _resolve(base: str, when: str, snapshots: bool) -> int:
    try:
        bumps = load_pending()
    except BumpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not bumps:
        print("no pending contract bumps")
        return 0
    if _git("rev-parse", "--verify", "--quiet", f"{base}^{{commit}}").returncode != 0:
        print(f"error: {base} is not a commit; fetch it or pass --base", file=sys.stderr)
        return 1
    if _git("merge-base", "--is-ancestor", base, "HEAD").returncode != 0:
        print(f"error: HEAD does not contain {base}; rebase onto it first", file=sys.stderr)
        return 1

    def read_base(module: str) -> str | None:
        proc = _git("show", f"{base}:src/contracts/{module}.py")
        return proc.stdout if proc.returncode == 0 else None

    texts, done, failed = resolve_texts(bumps, _module_text, read_base, when)
    for module, text in texts.items():
        (REPO / "src" / "contracts" / f"{module}.py").write_text(text, encoding="utf-8")
    for item in done:
        for name in item.files:
            (PENDING_DIR / name).unlink()
        print(f"{item.contract}: {item.before} -> {item.after} ({item.kind})")
    for line in _failure_lines(failed):
        print(line, file=sys.stderr)
    if snapshots and done:
        for script in SNAPSHOT_SCRIPTS:
            proc = subprocess.run(  # nosec B603 -- fixed argv
                [sys.executable, script, "--write"],
                capture_output=True,
                text=True,
                check=False,
                cwd=REPO,
            )
            sys.stdout.write(proc.stdout)
            if proc.returncode != 0:
                sys.stderr.write(proc.stderr)
                print(
                    f"error: {script} --write failed; the contracts are resolved but the "
                    "snapshots are not. Fix the cause and rerun it, or start over with: "
                    "git checkout -- src/contracts tests/contract",
                    file=sys.stderr,
                )
                return 1
    if done:
        print("review the diff, then amend your commit and push")
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--base",
        help="main's ref (default: <remote>/main for the remote that points at the public repo)",
    )
    parser.add_argument("--date", help="the Version History date (default: today, UTC)")
    parser.add_argument("--check", action="store_true", help="validate pending files only")
    parser.add_argument(
        "--require-resolved",
        action="store_true",
        help="validate, and fail on any pending file (implies --check)",
    )
    parser.add_argument(
        "--against",
        metavar="REF",
        help="with --check: also compare the contracts with REF's snapshots (the merge base)",
    )
    parser.add_argument(
        "--no-snapshots", action="store_true", help="do not regenerate the two snapshots"
    )
    args = parser.parse_args(argv)
    if args.check or args.require_resolved or args.against:
        return _check(args.require_resolved, args.against)
    when = args.date or datetime.datetime.now(datetime.UTC).date().isoformat()
    return _resolve(args.base or default_base(), when, not args.no_snapshots)


if __name__ == "__main__":
    sys.exit(main())
