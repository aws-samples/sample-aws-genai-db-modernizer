#!/usr/bin/env python3
"""Declare a pending contract version bump (#1158, ADR-008 Decision 8).

    uv run python scripts/bump_contract.py <contract> minor|major --reason "..." [--issue N]

``<contract>`` is a schema snapshot key (``synthesis_output.SynthesisOutputContract``)
or a contract module (``synthesis_output``) when the module has one versioned model,
or none and a Version History of its own. The script writes
``src/contracts/pending/<contract>.<slug>.toml`` (the slug defaults to the branch
name) and changes nothing else: the version number, the Version History entry and
the snapshots are written by ``scripts/resolve_contract_versions.py`` after the last
rebase. Running it again for the same contract and slug replaces the file.
"""

from __future__ import annotations

import argparse
import subprocess  # nosec B404 -- runs git with fixed argv
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from scripts.contract_bumps import (  # noqa: E402
    CONTRACTS_DIR,
    KINDS,
    PENDING_DIR,
    RESOLVE_COMMAND,
    BumpError,
    PendingBump,
    by_hand_steps,
    fragment_name,
    fragment_text,
    has_version_constant,
    history_newest,
    module_of,
)


def versioned_keys() -> list[str]:
    from scripts.contract_schema_snapshot import versioned_contract_models

    return [key for key, _ in versioned_contract_models()]


def resolve_contract(name: str, keys: list[str]) -> str:
    """The pending file's ``contract`` for what the user typed."""
    if "." in name:
        if name in keys:
            return name
        raise BumpError(
            f"{name} is not a versioned contract (see tests/contract/schema_snapshot.json)"
        )
    in_module = [key for key in keys if key.split(".", 1)[0] == name]
    if len(in_module) == 1:
        return in_module[0]
    if in_module:
        raise BumpError(
            f"{name} has {len(in_module)} versioned models; name one: {', '.join(in_module)}"
        )
    path = CONTRACTS_DIR / f"{name}.py"
    if not path.is_file():
        raise BumpError(f"no contract module src/contracts/{name}.py")
    source = path.read_text(encoding="utf-8")
    if has_version_constant(source):
        raise BumpError(
            f"{name} keeps its version in a constant as well as its Version History; bump it by hand"
        )
    if history_newest(source) is None:
        raise BumpError(f"{name} has no contract_version and no Version History to bump")
    return name


def check_resolvable(contract: str, kind: str, reason: str) -> None:
    """Refuse now, not after review, a contract the resolver cannot edit."""
    from scripts.resolve_contract_versions import resolve_texts

    path = CONTRACTS_DIR / f"{module_of(contract)}.py"
    source = path.read_text(encoding="utf-8")
    bump = PendingBump(name="(new)", contract=contract, kind=kind, reason=reason)
    _, _, failed = resolve_texts([bump], lambda m: source, lambda m: source, "0000-00-00")
    if failed:
        message = failed[0].message
        if "already landed" in message:
            raise BumpError(f"{contract}: that reason is already a Version History entry")
        raise BumpError(f"{contract}: {message}. {by_hand_steps(contract)}")


def current_branch() -> str:
    proc = subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        cwd=REPO,
    )
    branch = proc.stdout.strip()
    return "" if proc.returncode or branch == "HEAD" else branch


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "contract", help="module.Class, or a contract module with one versioned model"
    )
    parser.add_argument("kind", choices=KINDS)
    parser.add_argument("--reason", required=True, help="the Version History entry text")
    parser.add_argument("--issue", type=int, help="the issue number the entry cites")
    parser.add_argument("--slug", help="unique part of the file name (default: the branch name)")
    args = parser.parse_args(argv)
    try:
        contract = resolve_contract(args.contract, versioned_keys())
        name = fragment_name(contract, args.slug or current_branch())
        text = fragment_text(contract, args.kind, args.reason, args.issue)
        check_resolvable(contract, args.kind, args.reason)
    except BumpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    path = PENDING_DIR / name
    replaced = path.exists()
    path.write_text(text, encoding="utf-8")
    shown = path.relative_to(REPO) if path.is_relative_to(REPO) else path
    print(f"{'replaced' if replaced else 'wrote'} {shown} ({contract}, {args.kind})")
    if args.kind == "major":
        print("note: a MAJOR bump ships its reader adapter in the same change (ADR-008 Decision 4)")
    print(f"after your last rebase, before the merge queue: {RESOLVE_COMMAND}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
