#!/usr/bin/env python3
"""Pending contract version bumps (#1158, ADR-008 Decision 8).

Parallel pull requests used to claim the same ``contract_version`` (four PRs held
``synthesis_output`` 1.40 at once), and each had to renumber its version, its
Version History entry and both contract snapshots on every rebase. A pull request
now declares the *kind* of bump instead of a number:

    uv run python scripts/bump_contract.py synthesis_output minor --reason "..." --issue 1234

which writes one file, ``src/contracts/pending/<contract>.<slug>.toml``. Its name is
unique to the branch, so two pull requests never touch the same line for it. While a
bump is pending the contract keeps main's version, Version History and snapshots,
and the snapshot checks accept a shape change that the declared kind covers. After
the last rebase, before the pull request enters the merge queue:

    uv run python scripts/resolve_contract_versions.py

assigns the next free number above main's, writes the Version History entry from
the pending file, deletes it and regenerates both snapshots. The merge queue refuses
a pending file (``resolve_contract_versions.py --check --require-resolved`` in the
``validate-contracts`` job), so main never holds an unresolved bump.

This module holds the parsing and the text edits both scripts use, so the unit tests
(``tests/unit/scripts/test_contract_bumps.py``) cover them without git.
"""

from __future__ import annotations

import ast
import json
import re
import textwrap
import tomllib
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CONTRACTS_DIR = REPO / "src" / "contracts"
PENDING_DIR = CONTRACTS_DIR / "pending"
PENDING_RELDIR = "src/contracts/pending"
KINDS = ("minor", "major")
BUMP_COMMAND = "uv run python scripts/bump_contract.py"
RESOLVE_COMMAND = "uv run python scripts/resolve_contract_versions.py"

_KIND_RANK = {"none": 0, "minor": 1, "major": 2}
_VERSION_RE = re.compile(r"^(\d+)\.(\d+)$")
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_HEADING_RE = re.compile(r"^\s*Version [Hh]istory:\s*$")
_ENTRY_RE = re.compile(r"^(\s*)-\s+(v?)(\d+)\.(\d+)\b")
_DEFAULT_RE = re.compile(
    r"(contract_version\s*:\s*str\s*=\s*Field\(\s*(?:default\s*=\s*)?\")(\d+\.\d+)(\")"
)
# A module-level version constant (``MODEL_FLAG_SCHEMA_VERSION = "1.0"``): such a
# module's version lives in two places, so it is bumped by hand.
_CONSTANT_RE = re.compile(r"^[A-Z_]*VERSION\s*=\s*\"\d+\.\d+\"", re.MULTILINE)
HISTORY_WIDTH = 88


class BumpError(ValueError):
    """A pending bump that cannot be parsed, planned or applied."""


@dataclass(frozen=True)
class PendingBump:
    """One ``src/contracts/pending/*.toml`` file."""

    name: str
    contract: str
    kind: str
    reason: str
    issue: int | None = None

    @property
    def module(self) -> str:
        return module_of(self.contract)


def module_of(contract: str) -> str:
    """``synthesis_output`` for ``synthesis_output.SynthesisOutputContract``."""
    return contract.split(".", 1)[0]


def class_of(contract: str) -> str | None:
    """The model class a contract key names, ``None`` for a history-only module."""
    return contract.split(".", 1)[1] if "." in contract else None


def parse_version(version: str) -> tuple[int, int]:
    match = _VERSION_RE.match(version)
    if not match:
        raise BumpError(f"not a MAJOR.MINOR contract version: {version!r}")
    return int(match.group(1)), int(match.group(2))


def bump_version(version: str, kind: str) -> str:
    """The version one ``kind`` bump above ``version``."""
    major, minor = parse_version(version)
    if kind == "minor":
        return f"{major}.{minor + 1}"
    if kind == "major":
        return f"{major + 1}.0"
    raise BumpError(f"unknown bump kind {kind!r}; use one of {', '.join(KINDS)}")


def strongest(kinds: Iterable[str]) -> str:
    """The largest of ``kinds`` (``none`` for an empty list)."""
    return max(kinds, key=lambda k: _KIND_RANK[k], default="none")


def kind_covers(declared: str, required: str) -> bool:
    """Whether a declared bump is at least the one a change requires."""
    return _KIND_RANK[declared] >= _KIND_RANK[required]


def slugify(text: str) -> str:
    return _SLUG_RE.sub("-", text.lower()).strip("-")[:60]


def fragment_name(contract: str, slug: str) -> str:
    clean = slugify(slug)
    if not clean:
        raise BumpError("the pending file needs a slug: pass --slug or run on a named branch")
    return f"{contract}.{clean}.toml"


def fragment_text(contract: str, kind: str, reason: str, issue: int | None) -> str:
    """The TOML of one pending bump (JSON strings are valid TOML basic strings)."""
    if kind not in KINDS:
        raise BumpError(f"unknown bump kind {kind!r}; use one of {', '.join(KINDS)}")
    if not reason.strip():
        raise BumpError("a pending bump needs a --reason: it becomes the Version History entry")
    lines = [
        "# A pending contract version bump (ADR-008 Decision 8). The number is assigned by",
        f"# {RESOLVE_COMMAND} after the last rebase.",
        f"contract = {json.dumps(contract)}",
        f"kind = {json.dumps(kind)}",
        f"reason = {json.dumps(' '.join(reason.split()), ensure_ascii=False)}",
    ]
    if issue is not None:
        lines.append(f"issue = {int(issue)}")
    return "\n".join(lines) + "\n"


def parse_fragment(name: str, text: str) -> PendingBump:
    """Parse one pending file; ``name`` is its file name."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise BumpError(f"{name}: not valid TOML ({exc})") from exc
    unknown = set(data) - {"contract", "kind", "reason", "issue"}
    if unknown:
        raise BumpError(f"{name}: unknown keys {sorted(unknown)}")
    contract, kind, reason = data.get("contract"), data.get("kind"), data.get("reason")
    issue = data.get("issue")
    if not isinstance(contract, str) or not contract:
        raise BumpError(f"{name}: `contract` must name a contract (module.Class or module)")
    if kind not in KINDS:
        raise BumpError(f"{name}: `kind` must be one of {', '.join(KINDS)}, not {kind!r}")
    if not isinstance(reason, str) or not reason.strip():
        raise BumpError(f"{name}: `reason` must say what changed (it becomes the history entry)")
    if issue is not None and (not isinstance(issue, int) or isinstance(issue, bool)):
        raise BumpError(f"{name}: `issue` must be an issue number")
    if not name.startswith(f"{contract}."):
        raise BumpError(f"{name}: the file name must start with its contract, {contract!r}")
    return PendingBump(name=name, contract=contract, kind=kind, reason=reason, issue=issue)


def load_pending(directory: Path | None = None) -> list[PendingBump]:
    """Every pending bump in ``directory``, sorted by file name."""
    directory = PENDING_DIR if directory is None else directory
    if not directory.is_dir():
        return []
    return [
        parse_fragment(path.name, path.read_text(encoding="utf-8"))
        for path in sorted(directory.glob("*.toml"))
    ]


def pending_kinds(bumps: Iterable[PendingBump]) -> dict[str, str]:
    """``{contract: strongest declared kind}``."""
    grouped: dict[str, list[str]] = {}
    for bump in bumps:
        grouped.setdefault(bump.contract, []).append(bump.kind)
    return {contract: strongest(kinds) for contract, kinds in grouped.items()}


def pending_module_kinds(kinds: Mapping[str, str]) -> dict[str, str]:
    """``{module: strongest declared kind}`` for the value-set check, which versions
    a module as a whole."""
    grouped: dict[str, list[str]] = {}
    for contract, kind in kinds.items():
        grouped.setdefault(module_of(contract), []).append(kind)
    return {module: strongest(group) for module, group in grouped.items()}


# --- text edits -------------------------------------------------------------------


def _class_node(tree: ast.Module, class_name: str) -> ast.ClassDef:
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    raise BumpError(f"class {class_name} not found")


def _docstring_lines(node: ast.Module | ast.ClassDef) -> tuple[int, int] | None:
    """0-based first and last line of a docstring, ``None`` when there is none."""
    if not node.body:
        return None
    first = node.body[0]
    if (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
        and first.end_lineno is not None
    ):
        return first.lineno - 1, first.end_lineno - 1
    return None


def _default_span(source: str, class_name: str) -> tuple[int, int, str]:
    """Character span and value of ``class_name``'s ``contract_version`` default."""
    tree = ast.parse(source)
    node = _class_node(tree, class_name)
    lines = source.splitlines(keepends=True)
    start = sum(len(line) for line in lines[: node.lineno - 1])
    end = sum(len(line) for line in lines[: (node.end_lineno or node.lineno)])
    match = _DEFAULT_RE.search(source, start, end)
    if match is None:
        raise BumpError(f"{class_name} has no literal contract_version default")
    return match.start(2), match.end(2), match.group(2)


def contract_default(source: str, class_name: str) -> str:
    """The ``contract_version`` default ``class_name`` declares in ``source``."""
    return _default_span(source, class_name)[2]


def set_contract_default(source: str, class_name: str, version: str) -> str:
    parse_version(version)
    start, end, _ = _default_span(source, class_name)
    return source[:start] + version + source[end:]


@dataclass(frozen=True)
class _History:
    first_entry: int  # 0-based line of the first entry
    last_line: int  # 0-based last line of the list (an entry or its continuation)
    indent: str
    prefix: str  # "v" when the entries are written "v2.0"
    versions: tuple[tuple[int, int], ...]

    @property
    def newest(self) -> tuple[int, int]:
        return max(self.versions)

    @property
    def descending(self) -> bool:
        return len(self.versions) > 1 and self.versions[0] > self.versions[-1]


def _list_goes_on(lines: list[str], start: int, last: int, indent: str) -> bool:
    """Whether the first non-blank line from ``start`` is still part of the list."""
    for k in range(start, last + 1):
        line = lines[k]
        if not line.strip():
            continue
        entry = _ENTRY_RE.match(line)
        if entry and entry.group(1) == indent:
            return True
        return len(line) - len(line.lstrip()) > len(indent) and '"""' not in line
    return False


def _histories(lines: list[str], first: int, last: int) -> list[_History]:
    found: list[_History] = []
    i = first
    while i <= last:
        if not _HEADING_RE.match(lines[i]):
            i += 1
            continue
        j = i + 1
        match = _ENTRY_RE.match(lines[j]) if j <= last else None
        if match is None:
            i += 1
            continue
        indent, prefix = match.group(1), match.group(2)
        versions: list[tuple[int, int]] = []
        end = j
        while j <= last:
            line = lines[j]
            entry = _ENTRY_RE.match(line)
            if entry and entry.group(1) == indent:
                versions.append((int(entry.group(3)), int(entry.group(4))))
                end = j
            elif line.strip() and len(line) - len(line.lstrip()) > len(indent):
                end = j
            elif not line.strip() and _list_goes_on(lines, j + 1, last, indent):
                pass  # a paragraph break inside an entry
            else:
                break
            j += 1
        found.append(_History(i + 1, end, indent, prefix, tuple(versions)))
        i = j
    return found


def history_newest(source: str) -> str | None:
    """The newest entry of the module docstring's Version History, if any."""
    tree = ast.parse(source)
    span = _docstring_lines(tree)
    if span is None:
        return None
    histories = _histories(source.splitlines(), *span)
    if not histories:
        return None
    major, minor = histories[0].newest
    return f"{major}.{minor}"


def has_version_constant(source: str) -> bool:
    return _CONSTANT_RE.search(source) is not None


def _format_entry(history: _History, version: str, when: str, issues: str, reason: str) -> str:
    meta = f"{when}, {issues}" if issues else when
    text = f"{history.prefix}{version} ({meta}): {' '.join(reason.split())}"
    return textwrap.fill(
        text,
        width=HISTORY_WIDTH,
        initial_indent=f"{history.indent}- ",
        subsequent_indent=f"{history.indent}  ",
        break_long_words=False,
        break_on_hyphens=False,
    )


def insert_history_entry(
    source: str,
    class_name: str | None,
    current: str,
    version: str,
    when: str,
    issues: str,
    reason: str,
) -> str:
    """Add ``version``'s Version History entry next to ``current``'s.

    The history is the model's class docstring's when its newest entry is
    ``current``, else the module docstring's. Entries keep the list's own order
    (newest last, or newest first) and its ``v`` prefix, if it has one.
    """
    tree = ast.parse(source)
    lines = source.splitlines(keepends=True)
    bare = [line.rstrip("\n") for line in lines]
    owners: list[ast.Module | ast.ClassDef] = []
    if class_name is not None:
        owners.append(_class_node(tree, class_name))
    owners.append(tree)
    want = parse_version(current)
    seen: list[str] = []
    for owner in owners:
        span = _docstring_lines(owner)
        if span is None:
            continue
        for history in _histories(bare, *span):
            seen.append(".".join(map(str, history.newest)))
            if history.newest != want:
                continue
            if '"""' in bare[history.last_line] or "'''" in bare[history.last_line]:
                raise BumpError(
                    "the Version History ends on the docstring's closing quotes; "
                    "move them to a line of their own"
                )
            entry = _format_entry(history, version, when, issues, reason) + "\n"
            at = history.first_entry if history.descending else history.last_line + 1
            return "".join(lines[:at]) + entry + "".join(lines[at:])
    where = f"{class_name} or its module" if class_name else "the module docstring"
    found = f" (newest entries found: {', '.join(seen)})" if seen else ""
    raise BumpError(f"no Version History in {where} has {current} as its newest entry{found}")


def history_entries(source: str) -> list[str]:
    """The text of every Version History entry of a module (module and class
    docstrings), after its ``- X.Y (...):`` lead, with whitespace collapsed."""
    tree = ast.parse(source)
    bare = source.splitlines()
    owners: list[ast.Module | ast.ClassDef] = [tree]
    owners += [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]
    entries: list[str] = []
    for owner in owners:
        span = _docstring_lines(owner)
        if span is None:
            continue
        for history in _histories(bare, *span):
            current: list[str] = []
            for line in bare[history.first_entry : history.last_line + 1]:
                entry = _ENTRY_RE.match(line)
                if entry and entry.group(1) == history.indent:
                    if current:
                        entries.append(" ".join(current))
                    current = [line[entry.end() :]]
                else:
                    current.append(line)
            if current:
                entries.append(" ".join(current))
    out = []
    for text in entries:
        text = " ".join(text.split())
        lead = re.match(r"^(?:\([^)]*\))?\s*:?\s*", text)
        out.append(text[lead.end() :] if lead else text)
    return out


def already_recorded(source: str, reason: str) -> bool:
    """Whether ``reason`` is already (part of) a Version History entry in ``source``:
    a pending file left behind after its bump landed (a stacked branch re-adds it when
    it is rebased onto the resolved commit). An entry that resolved several pending
    files joins their reasons with a space, so a reason matches a whole entry or a
    whole sentence-ended part of one."""
    wanted = " ".join(reason.split())
    if not wanted:
        return False
    ends_sentence = wanted[-1] in ".!?)"

    def part_of(text: str) -> bool:
        if text == wanted:
            return True
        if not ends_sentence:
            return False  # a part of a joined entry is a whole sentence
        at = text.find(wanted)
        while at != -1:
            before_ok = at == 0 or text[at - 2 : at] in (". ", "! ", "? ", ") ")
            after = at + len(wanted)
            if before_ok and (after == len(text) or text[after] == " "):
                return True
            at = text.find(wanted, at + 1)
        return False

    return any(part_of(text) for text in history_entries(source))


def by_hand_steps(contract: str, name: str | None = None) -> str:
    """What to do for a contract the resolver cannot edit."""
    drop = f"delete {PENDING_RELDIR}/{name}; " if name else ""
    return (
        f"bump this contract by hand: {drop}set {contract}'s contract_version default to "
        "the next version above main's; add that version's entry to its Version History; "
        "then run uv run python scripts/contract_schema_snapshot.py --write and "
        "uv run python scripts/contract_value_snapshot.py --write"
    )
