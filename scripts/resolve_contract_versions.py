#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Assign the numbers of pending contract version bumps (#1158, #2176, ADR-008 Decision 8).

A pull request carries only its pending files (``scripts/bump_contract.py``); a pending
MINOR waits on main and the release resolves every pending file once:

    uv run python scripts/resolve_contract_versions.py --release --issue N [--date D] [--no-commit]

For every contract with a pending file in ``src/contracts/pending/`` it takes this tree's
version (the last release's), moves it by the strongest declared kind (several pending
files for one contract make one bump), writes the new ``contract_version`` default and a
Version History entry built from the pending files' reasons and issues, deletes those
pending files, regenerates ``tests/contract/schema_snapshot.json`` and
``tests/contract/value_set_snapshot.json`` and commits all of it as one commit
(``Refs #N``; ``--no-commit`` leaves it uncommitted in the tree). It refuses uncommitted
changes under ``src/contracts/`` or ``tests/contract/``, and writes nothing when any
contract cannot be resolved.

    uv run python scripts/resolve_contract_versions.py [--base upstream/main]

is the per-PR resolve, now for a MAJOR only: a pending MAJOR never waits on main (the
merge queue refuses it, see below), so the pull request that makes it resolves it after
its last rebase, with its reader adapter. It resolves every contract that has a pending
MAJOR file (with that contract's other pending files, main's MINOR ones included) and
leaves the rest to the release. ``--base`` defaults to ``<remote>/main`` for the remote
that points at the public repository, the one ``ci/pre_merge.sh`` fetches (``upstream``
on a fork, else ``origin``). It refuses when HEAD does not contain ``--base`` (rebase
first). A contract it cannot resolve (one it cannot edit, one the branch also moved by
hand, a pending file whose bump already landed) is reported with what to do, and the
others are still resolved.

    uv run python scripts/resolve_contract_versions.py --check [--refuse-pending-major]
        [--require-resolved] [--against REF]

only validates: every pending file names a known contract, a kind and a reason, the
resolver could edit its contract, and its bump has not already landed.
``--refuse-pending-major`` fails while a pending MAJOR is left, which is how the merge
queue keeps one off main. ``--require-resolved`` fails while any pending file is left
(after a release resolve). ``--against REF`` (the merge base) also:

- compares the contracts with REF's committed snapshots, so a shape or value change with
  no version move and no pending bump fails;
- compares them with the snapshots REF's own code builds (``git archive`` of REF, built in
  a fresh process), counting only the pending files this change adds or changes, so a
  change needs a pending file of its own even when main already holds one for that
  contract (``changes_without_own_bump``);
- refuses a ``contract_version``, a Version History line or a snapshot entry the change
  wrote by hand (``hand_set_problems``): only the release resolve and the per-PR resolve
  of a MAJOR write them;
- runs the dry run with REF as main, so a pending contract whose version the branch also
  moved by hand fails there, and so does a pending file for a contract REF does not have
  (a new contract starts at its own version);
- compares the public ``assess()`` contract with REF's copy of
  ``tests/contract/assess_released_since_1_3.json``
  (``contract_schema_snapshot.check_released_against``, #1184), so a branch cannot pass by
  re-recording that baseline. A REF git cannot read is an error of its own.
"""

from __future__ import annotations

import argparse
import ast
import datetime
import io
import json
import re
import subprocess  # nosec B404 -- runs git and the snapshot scripts with fixed argv
import sys
import tarfile
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from collections.abc import Set as AbstractSet
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.contract_bumps import (  # noqa: E402
    PENDING_DIR,
    PENDING_RELDIR,
    RESOLVE_COMMAND,
    BumpError,
    PendingBump,
    already_recorded,
    bump_version,
    by_hand_steps,
    class_of,
    contract_default,
    has_version_constant,
    history_lines_by_owner,
    history_newest,
    insert_history_entry,
    load_pending,
    module_of,
    parse_fragment,
    pending_kinds,
    pending_module_kinds,
    set_contract_default,
    strongest,
)

SNAPSHOT_SCRIPTS = ("scripts/contract_schema_snapshot.py", "scripts/contract_value_snapshot.py")
SCHEMA_SNAPSHOT = "tests/contract/schema_snapshot.json"
VALUE_SNAPSHOT = "tests/contract/value_set_snapshot.json"
RELEASE_SUBJECT = "chore(contracts): resolve pending contract bumps at release"
RELEASE_COMMAND = f"{RESOLVE_COMMAND} --release --issue N"


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


def resolver_refusal(
    contract: str, source: str, kind: str = "minor", reason: str = ""
) -> str | None:
    """Why the resolver cannot bump ``contract`` in ``source``, or None when it can.

    A dry run of ``resolve_texts`` with main's text the same as the branch's. A
    contract it refuses is one ``scripts/bump_contract.py`` sends to a hand bump
    (``by_hand_steps``); ``scripts/hooks/block_contract_version_edit.py`` lets
    exactly those be edited by hand (#1408).
    """
    bump = PendingBump(name="(new)", contract=contract, kind=kind, reason=reason or "(dry run)")
    _, _, failed = resolve_texts([bump], lambda m: source, lambda m: source, "0000-00-00")
    return failed[0].message if failed else None


def module_contracts(module: str, source: str) -> list[str]:
    """The contracts of a module as ``bump_contract.py`` names them.

    ``module.Class`` for each top-level class with a literal ``contract_version``
    default, else the module itself when it has a Version History. The schema
    snapshot lists the same classes; ``tests/unit/hooks/test_block_contract_version_edit.py``
    asserts it.
    """
    classes = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.ClassDef):
            try:
                contract_default(source, node.name)
            except BumpError:
                continue
            classes.append(f"{module}.{node.name}")
    if classes:
        return classes
    return [module] if history_newest(source) is not None else []


def hand_bumped_contracts(module: str, source: str) -> set[str]:
    """The contracts of ``module`` the resolver cannot edit: they are bumped by hand."""
    return {c for c in module_contracts(module, source) if resolver_refusal(c, source)}


# --- what a change may write (#2176) --------------------------------------------------


@dataclass(frozen=True)
class Tree:
    """The contract state of one tree: module sources by name, its pending files and its
    two committed snapshots."""

    modules: Mapping[str, str]
    pending: Sequence[PendingBump] = ()
    schema: Mapping[str, Any] = field(default_factory=dict)
    values: Mapping[str, Any] = field(default_factory=dict)


def own_bumps(ref: Iterable[PendingBump], head: Iterable[PendingBump]) -> list[PendingBump]:
    """The pending files of ``head`` that ``ref`` does not hold as they are: the ones this
    change adds or changes (a pending file main already holds belongs to another PR)."""
    held = set(ref)
    return [bump for bump in head if bump not in held]


def for_this_pull_request(bumps: Iterable[PendingBump]) -> list[PendingBump]:
    """The pending files the per-PR resolve takes: every pending file of a contract that
    has a pending MAJOR. A MINOR waits for the release."""
    bumps = list(bumps)
    majors = {b.contract for b in bumps if b.kind == "major"}
    return [b for b in bumps if b.contract in majors]


def _versions(source: str, contracts: Iterable[str]) -> dict[str, str]:
    found: dict[str, str] = {}
    for contract in contracts:
        try:
            found[contract] = _version_of(source, contract)
        except (BumpError, SyntaxError):
            continue
    return found


def hand_set_problems(
    ref: Tree, head: Tree, governing: Callable[[str], str | None] | None = None
) -> list[str]:
    """Every ``contract_version``, Version History line and snapshot entry that ``head``
    changed since ``ref`` and that only a resolve may write (ADR-008 Decision 8, #2176).

    A contract the resolver can edit may move its version in two ways only: the release
    resolve of ``ref``'s pending files for it (they are gone in ``head`` and the version
    is ``ref``'s moved by their strongest kind), or a MAJOR (``ref``'s next MAJOR, with
    no pending file left for it), which the pull request that makes it resolves. A
    contract the resolver cannot edit is bumped by hand and is not checked; neither are a
    new or removed module, model or snapshot entry. In a module none of whose versions
    moved, every Version History line of a docstring both trees have is unchanged. A
    snapshot entry may change with its contract's (or, for a value set, its governing
    module's) allowed move; otherwise it may not change while that contract or module
    has a pending file (the release writes it), and its recorded version may not change
    at all. ``governing`` maps a value set's module to the module whose version governs
    it (``contract_value_snapshot.version_source``); the module itself by default.
    """
    moves = _classify(ref, head)
    problems = list(moves.problems)
    pending = {b.contract for b in head.pending} | {b.contract for b in ref.pending}
    moved = moves.release | moves.major
    exempt = moves.exempt
    held = {b.name: b for b in ref.pending}
    for bump in head.pending:
        if bump.name in held and held[bump.name] != bump:
            problems.append(
                f"{PENDING_RELDIR}/{bump.name}: a pending file main holds was edited; it is "
                "another pull request's declaration, which the release resolves as it is. "
                "Restore it and declare your own bump with uv run python "
                f"scripts/bump_contract.py {bump.contract} minor|major --reason ..."
            )
    problems += _snapshot_edits(
        SCHEMA_SNAPSHOT, ref.schema, head.schema, lambda key: key, moved, exempt, pending
    )
    govern = governing or (lambda module: module)

    def module_key(key: str) -> str:
        entry = head.values.get(key) or ref.values.get(key) or {}
        module = str(entry.get("module", module_of(key)))
        return govern(module) or module

    problems += _snapshot_edits(
        VALUE_SNAPSHOT,
        ref.values,
        head.values,
        module_key,
        {module_of(c) for c in moved},
        {module_of(c) for c in exempt},
        {module_of(c) for c in pending},
    )
    return problems


@dataclass(frozen=True)
class _Moves:
    problems: tuple[str, ...]
    release: frozenset[str]  # moved by the release resolve of REF's pending files
    major: frozenset[str]  # moved to REF's next MAJOR, resolved in its pull request
    exempt: frozenset[str]  # contracts the resolver cannot edit: bumped by hand


def released_contracts(ref: Tree, head: Tree) -> set[str]:
    """The contracts ``head`` moved as the release resolve of ``ref``'s pending files."""
    return set(_classify(ref, head).release)


def _only_appended(before: list[str], after: list[str]) -> bool:
    """Whether ``after`` holds every line of ``before``, unchanged and in order."""
    rest = iter(after)
    return all(any(line == other for other in rest) for line in before)


_DECLARED_RE = re.compile(
    r"(?:^[A-Z_]*VERSION|contract_version\s*:\s*str\s*=\s*Field\(\s*(?:default\s*=\s*)?)\s*=?\s*\"(\d+\.\d+)\"",
    re.MULTILINE,
)


def _hand_history_moves(module: str, before: str, after: str) -> list[str]:
    """A module bumped by hand (``model_flag``: its version is also a constant) may move
    its Version History's newest entry only to a version it declares, so a history line
    alone cannot move the version the value-set snapshot records."""
    try:
        old, new = history_newest(before), history_newest(after)
    except SyntaxError:
        return []
    if old is None or new is None or old == new:
        return []
    declared = sorted(set(_DECLARED_RE.findall(after)))
    if new in declared:
        return []
    return [
        f"{module}: the Version History's newest entry moved {old} -> {new}, but the module "
        f"declares {', '.join(declared) or 'no version'}; a contract the resolver cannot edit "
        "is bumped by hand, its version and its history entry together"
    ]


def _classify(ref: Tree, head: Tree) -> _Moves:
    problems: list[str] = []
    head_names = {b.name for b in head.pending}
    head_pending = {b.contract for b in head.pending}
    release: set[str] = set()
    major: set[str] = set()
    exempt: set[str] = set()
    for module in sorted(set(ref.modules) & set(head.modules)):
        before, after = ref.modules[module], head.modules[module]
        try:
            contracts = module_contracts(module, after)
            hand = hand_bumped_contracts(module, after)
        except SyntaxError:
            continue
        exempt |= hand
        old, new = _versions(before, contracts), _versions(after, contracts)
        any_move = False
        for contract in sorted(set(old) & set(new) - hand):
            if old[contract] == new[contract]:
                continue
            any_move = True
            change = f"{contract}: contract_version moved {old[contract]} -> {new[contract]}"
            released = [
                b.kind for b in ref.pending if b.contract == contract and b.name not in head_names
            ]
            if contract in head_pending:
                problems.append(
                    f"{change} but still has a pending file; keep either the pending file or "
                    "the resolved version"
                )
            elif released and new[contract] == bump_version(old[contract], strongest(released)):
                release.add(contract)
            elif new[contract] == bump_version(old[contract], "major"):
                major.add(contract)  # a MAJOR, resolved in the pull request that makes it
            elif released:
                kind = strongest(released)
                expected = bump_version(old[contract], kind)
                problems.append(
                    f"{change}, but the release resolve of its pending files ({kind}) "
                    f"gives {expected}; run {RELEASE_COMMAND}"
                )
            else:
                problems.append(
                    f"{change} by hand; a pull request declares the bump with "
                    f"uv run python scripts/bump_contract.py {contract} minor|major --reason "
                    f"... and keeps the version, the Version History and the snapshots at "
                    f"main's: {RELEASE_COMMAND} writes them at release (ADR-008 Decision 8)"
                )
        if hand:
            problems += _hand_history_moves(module, before, after)
            continue
        try:
            lines_before, lines_after = (
                history_lines_by_owner(before),
                history_lines_by_owner(after),
            )
        except SyntaxError:
            continue
        owners = set(lines_before) & set(lines_after)
        if not any_move:
            if any(lines_before[o] != lines_after[o] for o in owners):
                problems.append(
                    f"{module}: the Version History changed by hand with no version move; "
                    f"only the release resolve ({RELEASE_COMMAND}) writes it (ADR-008 "
                    "Decision 8)"
                )
        elif not all(_only_appended(lines_before[o], lines_after[o]) for o in owners):
            problems.append(
                f"{module}: a Version History line changed; a resolve only appends the new "
                "version's entry, every older line stays as it was"
            )
    return _Moves(tuple(problems), frozenset(release), frozenset(major), frozenset(exempt))


def _snapshot_edits(
    path: str,
    before: Mapping[str, Any],
    after: Mapping[str, Any],
    owner: Callable[[str], str],
    moved: AbstractSet[str],
    exempt: AbstractSet[str],
    pending: AbstractSet[str],
) -> list[str]:
    problems: list[str] = []
    for key in sorted(set(before) & set(after)):
        if before[key] == after[key]:
            continue
        who = owner(key)
        if who in moved or who in exempt:
            continue
        if who in pending:
            problems.append(
                f"{path}: {key} changed while {who} has a pending bump; the snapshots keep "
                f"main's entry until the release resolve writes it ({RELEASE_COMMAND}). "
                f"Take main's copy: git checkout <main> -- {path}"
            )
        elif before[key].get("version") != after[key].get("version"):
            problems.append(
                f"{path}: {key} records {before[key].get('version')} -> "
                f"{after[key].get('version')} with no resolved version move; only a resolve "
                f"writes it ({RELEASE_COMMAND})"
            )
    return problems


def changes_without_own_bump(
    ref_schema: dict[str, Any],
    head_schema: dict[str, Any],
    ref_values: dict[str, Any],
    head_values: dict[str, Any],
    own: Mapping[str, str],
    released: Iterable[str] = (),
) -> list[str]:
    """Shape and value changes between the snapshots REF's code builds and this tree's
    that no pending file of this change covers (``own``: contract -> strongest kind).

    A contract in ``released`` (moved by the release resolve of REF's pending files) is
    compared at REF's version, and so are its module's value sets: the release move covers
    the changes REF's code already has, not a change the same tree adds, so only a pure
    release passes."""
    from scripts import contract_schema_snapshot as schema
    from scripts import contract_value_snapshot as values

    keys = set(released)
    modules = {module_of(c) for c in keys}
    head_schema = {
        k: ({**v, "version": ref_schema[k]["version"]} if k in keys and k in ref_schema else v)
        for k, v in head_schema.items()
    }
    head_values = {
        k: (
            {**v, "version": ref_values[k]["version"]}
            if v.get("module") in modules and k in ref_values
            else v
        )
        for k, v in head_values.items()
    }
    return schema.evaluate_against_base(ref_schema, head_schema, own) + (
        values.evaluate_against_base(ref_values, head_values, pending_module_kinds(own))
    )


def _extract(ref: str, root: Path) -> None:
    archive = subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", "archive", "--format=tar", ref, "src", "scripts", "tests/contract"],
        capture_output=True,
        check=False,
        cwd=REPO,
    )
    if archive.returncode != 0:
        raise BumpError(f"cannot read {ref}: {archive.stderr.decode().strip()}")
    # The data filter rejects absolute paths, ".." and links outside root, and the
    # archive is this repository's own tree at ref.
    # nosemgrep: trailofbits.python.tarfile-extractall-traversal.tarfile-extractall-traversal
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(root, filter="data")


def _built(root: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """The two snapshots ``root``'s own scripts build from ``root``'s own contracts."""
    built = []
    for script in SNAPSHOT_SCRIPTS:
        done = subprocess.run(  # nosec B603 -- this interpreter, the ref's own script
            [sys.executable, str(root / script)],
            capture_output=True,
            text=True,
            check=False,
            cwd=root,
        )
        if done.returncode != 0:
            raise BumpError(f"{script} failed on the ref's tree: {done.stderr.strip()[-400:]}")
        built.append(json.loads(done.stdout))
    return built[0], built[1]


def _read_tree(root: Path) -> Tree:
    contracts = root / "src" / "contracts"
    modules = {
        path.stem: path.read_text(encoding="utf-8")
        for path in sorted(contracts.glob("*.py"))
        if path.name != "__init__.py"
    }
    pending_dir = contracts / "pending"
    pending = (
        [
            parse_fragment(p.name, p.read_text(encoding="utf-8"))
            for p in sorted(pending_dir.glob("*.toml"))
        ]
        if pending_dir.is_dir()
        else []
    )

    def load(path: str) -> dict[str, Any]:
        file = root / path
        return json.loads(file.read_text(encoding="utf-8")) if file.is_file() else {}

    return Tree(modules, tuple(pending), load(SCHEMA_SNAPSHOT), load(VALUE_SNAPSHOT))


def _governing(module: str) -> str | None:
    from scripts.contract_value_snapshot import version_source

    try:
        return version_source(module)
    except ImportError:
        return module


def _since_ref(ref: str, bumps: list[PendingBump], seen: Iterable[str] = ()) -> list[str]:
    """``changes_without_own_bump`` and ``hand_set_problems`` between REF and this tree.
    A change of a key in ``seen`` (already reported against REF's committed snapshots)
    is not reported again by the first."""
    from scripts import contract_schema_snapshot as schema
    from scripts import contract_value_snapshot as values

    with tempfile.TemporaryDirectory(prefix="contracts-at-ref-") as name:
        root = Path(name)
        try:
            _extract(ref, root)
            ref_tree = _read_tree(root)
            ref_schema, ref_values = _built(root)
        except BumpError as exc:
            return [f"--against {ref}: {exc}"]
    here = _read_tree(REPO)
    here = Tree(here.modules, tuple(bumps), here.schema, here.values)
    own = pending_kinds(own_bumps(ref_tree.pending, bumps))
    skip = set(seen)
    problems = [
        p
        for p in changes_without_own_bump(
            ref_schema,
            schema.build_snapshot(),
            ref_values,
            values.build_snapshot(),
            own,
            released_contracts(ref_tree, here),
        )
        if p.split(":", 1)[0] not in skip
    ]
    return problems + hand_set_problems(ref_tree, here, _governing)


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


def _check(require_resolved: bool, against: str | None, refuse_major: bool = False) -> int:
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
        committed = _against(against, pending_kinds(bumps))
        problems += [f"error: {p}" for p in committed]
        # A change already reported against main's committed snapshot is not repeated.
        seen = {p.split(":", 1)[0] for p in committed}
        problems += [f"error: {p}" for p in _since_ref(against, bumps, seen=seen)]
        # The public assess() contract, against main's copy of its released snapshot (#1184).
        from scripts.contract_schema_snapshot import check_released_against

        problems += [f"error: {p}" for p in check_released_against(against)]
    majors = [b for b in bumps if b.kind == "major"]
    if refuse_major:
        problems += [
            f"error: {b.name}: a pending MAJOR cannot reach main (artifacts of the new shape "
            f"would carry the old MAJOR); resolve it in this pull request after the last "
            f"rebase with {RESOLVE_COMMAND}, with its reader adapter (ADR-008 Decision 8)"
            for b in majors
        ]
    for problem in problems:
        print(problem)
    if problems:
        return 1
    if not bumps:
        print("ok: no pending contract bumps")
    for bump in bumps:
        print(f"pending: {bump.contract} {bump.kind} ({PENDING_DIR.name}/{bump.name})")
    if against:
        print(
            f"ok: every contract change since {against} has a pending file of its own or a "
            "resolved version, and nothing resolved was written by hand"
        )
    if bumps and require_resolved:
        print(
            f"error: {len(bumps)} pending contract bump(s); the release resolves them: "
            f"{RELEASE_COMMAND} (ADR-008 Decision 8)"
        )
        return 1
    return 0


def _write_snapshots() -> int:
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
    return 0


def _apply(texts: dict[str, str], done: list[Resolution]) -> None:
    for module, text in texts.items():
        (REPO / "src" / "contracts" / f"{module}.py").write_text(text, encoding="utf-8")
    for item in done:
        for name in item.files:
            (PENDING_DIR / name).unlink()
        print(f"{item.contract}: {item.before} -> {item.after} ({item.kind})")


def _resolve(base: str, when: str, snapshots: bool) -> int:
    try:
        bumps = load_pending()
    except BumpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if not bumps:
        print("no pending contract bumps")
        return 0
    chosen = for_this_pull_request(bumps)
    if not chosen:
        print(
            f"no pending MAJOR bump: {len(bumps)} pending bump(s) are resolved at release "
            f"({RELEASE_COMMAND}); leave the version, the Version History and the snapshots "
            "at main's (ADR-008 Decision 8)"
        )
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

    texts, done, failed = resolve_texts(chosen, _module_text, read_base, when)
    _apply(texts, done)
    for line in _failure_lines(failed):
        print(line, file=sys.stderr)
    if snapshots and done and _write_snapshots():
        return 1
    if done:
        print("review the diff, then amend your commit and push")
    return 1 if failed else 0


def release_message(done: list[Resolution], issue: int) -> str:
    """The release resolve's commit message (CONTRIBUTING.md's Commit Message Format)."""
    files = sum(len(item.files) for item in done)
    return (
        f"{RELEASE_SUBJECT}\n\n"
        f"Resolves {files} pending bump(s) of {len(done)} contract(s) at release, each by the\n"
        "strongest kind declared for it (ADR-008 Decision 8, #2176).\n\n"
        f"Refs #{issue}\n"
    )


def _release(when: str, issue: int | None, commit: bool) -> int:
    try:
        bumps = load_pending()
    except BumpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if commit and issue is None:
        print(
            "error: pass --issue N (the release's tracking issue, the commit's Refs trailer), "
            "or --no-commit",
            file=sys.stderr,
        )
        return 1
    if not bumps:
        print("no pending contract bumps; nothing to resolve")
        return 0
    dirty = _git("status", "--porcelain", "--", "src/contracts", "tests/contract").stdout.strip()
    if dirty:
        print(
            "error: uncommitted changes under src/contracts/ or tests/contract/; the release "
            f"resolve starts from a clean tree:\n{dirty}",
            file=sys.stderr,
        )
        return 1

    def read_base(module: str) -> str | None:
        proc = _git("show", f"HEAD:src/contracts/{module}.py")
        return proc.stdout if proc.returncode == 0 else None

    texts, done, failed = resolve_texts(bumps, _module_text, read_base, when)
    if failed:
        for line in _failure_lines(failed):
            print(line, file=sys.stderr)
        print("error: nothing written; fix those pending files and rerun", file=sys.stderr)
        return 1
    _apply(texts, done)
    if _write_snapshots():
        return 1
    if not commit:
        print("resolved; review the diff and commit it as one commit")
        return 0
    paths = ["src/contracts", SCHEMA_SNAPSHOT, VALUE_SNAPSHOT]
    added = _git("add", "-A", "--", *paths)
    made = (
        added if added.returncode else _git("commit", "-q", "-m", release_message(done, issue or 0))
    )
    if made.returncode != 0:
        print(
            f"error: git could not commit the release resolve: {made.stderr.strip()}",
            file=sys.stderr,
        )
        return 1
    print(f"committed: {RELEASE_SUBJECT}; open a pull request with it before the tag")
    return 0


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
        "--refuse-pending-major",
        action="store_true",
        help="validate, and fail on a pending MAJOR (implies --check; the merge queue)",
    )
    parser.add_argument(
        "--against",
        metavar="REF",
        help="with --check: also compare the contracts with REF (the merge base)",
    )
    parser.add_argument(
        "--release",
        action="store_true",
        help="the release step: resolve every pending file and commit it as one commit",
    )
    parser.add_argument("--issue", type=int, help="with --release: the commit's Refs #N")
    parser.add_argument(
        "--no-commit", action="store_true", help="with --release: leave the result uncommitted"
    )
    parser.add_argument(
        "--no-snapshots", action="store_true", help="do not regenerate the two snapshots"
    )
    args = parser.parse_args(argv)
    if args.check or args.require_resolved or args.against or args.refuse_pending_major:
        return _check(args.require_resolved, args.against, args.refuse_pending_major)
    when = args.date or datetime.datetime.now(datetime.UTC).date().isoformat()
    if args.release:
        return _release(when, args.issue, not args.no_commit)
    return _resolve(args.base or default_base(), when, not args.no_snapshots)


if __name__ == "__main__":
    sys.exit(main())
