#!/usr/bin/env python3
"""The rule catalog's TOML format, recorded so it stays backwards compatible (#1647).

Callers read the catalog's TOML files (``src/catalog/``) directly. Its *values* (fit
scores, penalties, prices, thresholds) are tuning that changes between releases; its
*format* is promised backwards compatible within 0.x. This module records the format and
compares the working tree with it:

- ``build_format(root)`` walks every ``*.toml`` under ``root``. A file directly under it
  is a file kind of its own (``aurora_versions``, ``routing``, ...); a directory is one
  file kind for all its files (``engines``, ``patterns``, ``gaps``, ``write_intents``).
  Per kind it records the file names and every key path (``a.b`` for a nested table,
  ``a[]`` for an array's items, ``a[].b`` for a key of an array of tables), each key's
  TOML value types and whether it is required: present in every table it can appear in
  (every file of the kind for a top-level key, every ``a`` table for ``a.b``, every
  element for ``a[].b``).
- A map keyed by data rather than by a name the catalog defines (instance types in
  ``prices.toml``, model names in ``model_prices.toml``: ``DATA_KEYED``) is recorded by
  its value schema only, as one ``*`` segment, so an instance type or a model coming or
  going is not a format change; retyping its values or dropping one of their fields is.
- ``evaluate_format(recorded, current)`` lists every change that can break a reader: a
  file kind or a file removed, a key removed (a rename is a removal plus an addition), a
  key given a type it never had, a required key now missing from some file or element,
  or an optional key that is now in every one. Added kinds, files and keys pass; an
  optional key may be absent from any file.

Rule ids are stable: the engine, pattern, gap rule and write intent files, and the named
entries of the shared files (thresholds, capabilities, signals), are recorded by name, so
retiring one needs a deprecation like any other removal.

The committed snapshot is ``tests/contract/catalog_format_snapshot.json``, compared by
``tests/contract/test_catalog_format_snapshot.py``. ``released`` names the release tag it
was recorded at, or is null for a draft recorded from the working tree before the tag. It
is never edited by hand (``scripts/hooks/block_baseline_edit.py``). A deliberate removal
is a documented deprecation first (the key or file stays readable for at least one
release, and the release notes say so), then a maintainer-approved snapshot update.

The release step records the tag's own format, read with ``git archive`` at the tag, and
the tag's catalog files as a fixture (``tests/contract/fixtures/catalog_releases/``) that
``tests/contract/test_catalog_old_releases_load.py`` loads with the current loader::

    uv run python scripts/catalog_format_snapshot.py --write-released v0.4.0-beta.3

``--draft`` records a draft from the working tree (``released: null``);
``--record-files <tag>`` writes only the tag's file fixture. With no option the current
format is printed.
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import re
import shutil
import subprocess  # nosec B404 -- git with fixed arguments, no shell
import sys
import tarfile
import tempfile
import tomllib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
CATALOG_DIR = REPO / "src" / "catalog"
CATALOG_PREFIX = "src/catalog"
SNAPSHOT_PATH = REPO / "tests" / "contract" / "catalog_format_snapshot.json"
RELEASE_FILES_DIR = REPO / "tests" / "contract" / "fixtures" / "catalog_releases"
WRITE_RELEASED_COMMAND = "uv run python scripts/catalog_format_snapshot.py --write-released <tag>"
RECORD_FILES_COMMAND = "uv run python scripts/catalog_format_snapshot.py --record-files <tag>"

ABOUT = (
    "The rule catalog's TOML format (#1647): per file kind, its files and its key paths "
    "with their value types and whether every table they can appear in has them (a * "
    "segment is a map keyed by data). Compared by "
    "tests/contract/test_catalog_format_snapshot.py: removing, renaming or retyping a key, "
    "a required key missing from a file, making an optional key required, or removing a "
    "file or file kind fails; additions pass. released names the tag it was recorded at "
    "(null: a draft before the tag). Recorded by the release step, never by hand: "
    + WRITE_RELEASED_COMMAND
)

#: Per file kind, the maps keyed by data, as key-path patterns: ``*`` is a data key (its
#: name is not recorded), ``?`` matches any one key and keeps it, and ``{a,b}`` lists
#: literal keys. A ``*`` table on the way to a pattern's end is collapsed too, so
#: ``*.*.x`` collapses both levels above ``x`` while ``?.?.x.*`` collapses only the leaf.
DATA_KEYED: dict[str, tuple[str, ...]] = {
    # The instance classes of each offer section and the offer files' dates, generated
    # from the public price list by scripts/refresh_prices.py. The section names
    # (``service.configuration``) are part of the format and stay literal.
    "prices": ("?.?.instance_hourly.*", "?.?.instance_memory_gib.*", "published.*"),
    # Model name -> inference profile -> per-token prices.
    "model_prices": ("*.*.{input,output,cache_read,cache_write}",),
}


# --------------------------------------------------------------------------- format

#: A key TOML writes bare; any other key is quoted in a key path, as TOML quotes it.
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")


def _toml_type(value: Any) -> str:
    """The TOML type name of a parsed value."""
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "string"
    if isinstance(value, dt.datetime):
        return "datetime"
    if isinstance(value, dt.date):
        return "date"
    if isinstance(value, dt.time):
        return "time"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "table"
    raise TypeError(f"not a TOML value: {value!r}")


def _segment_matches(pattern: str, segment: str) -> bool:
    if pattern in ("*", "?"):
        return True
    if pattern.startswith("{") and pattern.endswith("}"):
        return segment in pattern[1:-1].split(",")
    return pattern == segment


class _Kind:
    """Key-path statistics for one file kind while it is walked."""

    def __init__(self, data_keyed: tuple[str, ...] = ()) -> None:
        self.patterns = [tuple(p.split(".")) for p in data_keyed]
        self.types: dict[str, set[str]] = {}
        self.seen: dict[str, int] = {}  # key path -> tables it appears in
        self.containers: dict[str, int] = {}  # container path -> tables walked there
        self.parent: dict[str, str] = {}  # key path -> its container path
        self.collapsed: set[str] = set()  # key paths that end in a data key

    def _segment(self, container: tuple[str, ...], name: str, value: Any) -> str:
        """``name``, or ``*`` when a ``DATA_KEYED`` pattern makes it a data key."""
        depth = len(container)
        wildcard = False
        for pattern in self.patterns:
            if len(pattern) <= depth or not (len(pattern) == depth + 1 or isinstance(value, dict)):
                continue
            if not all(_segment_matches(p, c) for p, c in zip(pattern, container, strict=False)):
                continue
            if not _segment_matches(pattern[depth], name):
                continue
            if pattern[depth] != "*":
                return name  # a literal key of some pattern stays a key
            wildcard = True
        return "*" if wildcard else name

    def walk_table(self, table: Mapping[str, Any], container: tuple[str, ...]) -> None:
        here = ".".join(container)
        self.containers[here] = self.containers.get(here, 0) + 1
        counted: set[str] = set()
        for key, value in table.items():
            name = key if _BARE_KEY.fullmatch(key) else json.dumps(key)
            segments = (*container, self._segment(container, name, value))
            path = ".".join(segments)
            self.parent[path] = here
            if segments[-1] == "*" and name != "*":
                self.collapsed.add(path)
            if path not in counted:  # a data-keyed map counts once per table
                counted.add(path)
                self.seen[path] = self.seen.get(path, 0) + 1
            self._value(segments, value)

    def _value(self, segments: tuple[str, ...], value: Any) -> None:
        path = ".".join(segments)
        self.types.setdefault(path, set()).add(_toml_type(value))
        if isinstance(value, dict):
            self.walk_table(value, segments)
        elif isinstance(value, list):
            items = (*segments[:-1], f"{segments[-1]}[]")
            for item in value:
                self.types.setdefault(".".join(items), set()).add(_toml_type(item))
                if isinstance(item, dict):
                    self.walk_table(item, items)
                elif isinstance(item, list):
                    self._value(items, item)

    def keys(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for path in sorted(self.types):
            entry: dict[str, Any] = {"types": sorted(self.types[path])}
            # A table key has a required flag; an array's items and a data key do not.
            if path in self.seen and path not in self.collapsed:
                entry["required"] = self.seen[path] == self.containers[self.parent[path]]
            out[path] = entry
        return out


def _kinds(root: Path) -> dict[str, tuple[str, list[Path]]]:
    """File kind -> (its location, its files), for every TOML file under ``root``."""
    kinds: dict[str, tuple[str, list[Path]]] = {}
    for path in sorted(root.glob("*.toml")):
        kinds[path.stem] = (f"{CATALOG_PREFIX}/{path.name}", [path])
    for directory in sorted(p for p in root.iterdir() if p.is_dir()):
        files = sorted(directory.glob("*.toml"))
        if files:
            if directory.name in kinds:
                raise ValueError(f"{directory.name}: both a file kind and a directory")
            kinds[directory.name] = (f"{CATALOG_PREFIX}/{directory.name}/*.toml", files)
    nested = sorted(
        p.relative_to(root).as_posix() for p in root.glob("*/**/*.toml") if p.parent.parent != root
    )
    if nested:
        raise ValueError(f"TOML files nested more than one level deep: {nested}")
    return kinds


def build_format(root: Path = CATALOG_DIR) -> dict[str, Any]:
    """Every file kind under ``root``: its location, files and key paths."""
    out: dict[str, Any] = {}
    for name, (location, files) in _kinds(root).items():
        kind = _Kind(DATA_KEYED.get(name, ()))
        for path in files:
            with path.open("rb") as fh:
                kind.walk_table(tomllib.load(fh), ())
        out[name] = {
            "location": location,
            "files": [p.name for p in files],
            "keys": kind.keys(),
        }
    return out


def evaluate_format(recorded: Mapping[str, Any], current: Mapping[str, Any]) -> list[str]:
    """Every change from ``recorded`` to ``current`` that can break a reader."""
    problems: list[str] = []
    for name in sorted(recorded):
        before = recorded[name]
        after = current.get(name)
        if after is None:
            problems.append(f"{name}: file kind removed ({before['location']})")
            continue
        for gone in sorted(set(before["files"]) - set(after["files"])):
            problems.append(f"{name}: file {gone} removed")
        for path, old in sorted(before["keys"].items()):
            new = after["keys"].get(path)
            if new is None:
                problems.append(f"{name}: key {path} removed or renamed")
                continue
            if added := sorted(set(new["types"]) - set(old["types"])):
                problems.append(
                    f"{name}: key {path} retyped: was {'/'.join(old['types'])}, "
                    f"now also {'/'.join(added)}"
                )
            if old.get("required") is True and new.get("required") is not True:
                problems.append(
                    f"{name}: required key {path} is missing from a file or table that had "
                    "to have it"
                )
            if old.get("required") is False and new.get("required") is True:
                problems.append(f"{name}: key {path} was optional and is now in every file")
    return problems


# --------------------------------------------------------------------------- git


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # nosec B603 B607 -- git with fixed arguments
        ["git", *args], cwd=repo, capture_output=True, check=False
    )


def tag_exists(tag: str, repo: Path = REPO) -> bool:
    return _git(repo, "rev-parse", "-q", "--verify", f"refs/tags/{tag}").returncode == 0


def catalog_files_at(tag: str, repo: Path = REPO) -> dict[str, str]:
    """Every TOML file under ``src/catalog/`` at ``tag``: path below it -> text."""
    if not tag_exists(tag, repo):
        raise SystemExit(
            f"catalog_format_snapshot: {tag!r} is not a tag here; create or fetch the "
            "release tag first (git fetch --tags)"
        )
    archive = _git(repo, "archive", "--format=tar", tag, CATALOG_PREFIX)
    if archive.returncode != 0:
        raise SystemExit(archive.stderr.decode(errors="replace"))
    files: dict[str, str] = {}
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        for member in tar.getmembers():
            if member.isfile() and member.name.endswith(".toml"):
                extracted = tar.extractfile(member)
                assert extracted is not None  # nosec B101 -- a regular file member
                rel = member.name.removeprefix(CATALOG_PREFIX + "/")
                files[rel] = extracted.read().decode("utf-8")
    return dict(sorted(files.items()))


def materialize(files: Mapping[str, str], root: Path) -> Path:
    """Write ``files`` (path below the catalog -> text) under ``root``; returns ``root``."""
    for rel, text in files.items():
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


def format_at(tag: str, repo: Path = REPO) -> dict[str, Any]:
    """The catalog format as ``tag`` ships it, read from the tag, not the working tree."""
    with tempfile.TemporaryDirectory() as tmp:
        return build_format(materialize(catalog_files_at(tag, repo), Path(tmp)))


# --------------------------------------------------------------------------- writing


def released_document(
    tag: str | None, repo: Path = REPO, root: Path = CATALOG_DIR
) -> dict[str, Any]:
    """The snapshot document: ``tag``'s format, or the working tree's for a draft."""
    return {
        "about": ABOUT,
        "released": tag,
        "kinds": build_format(root) if tag is None else format_at(tag, repo),
    }


def _dump(document: Mapping[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_released(
    tag: str | None, repo: Path = REPO, path: Path = SNAPSHOT_PATH
) -> dict[str, Any]:
    """Write the snapshot for ``tag`` (a draft from the working tree when None)."""
    document = released_document(tag, repo)
    _dump(document, path)
    return document


def record_files(tag: str, repo: Path = REPO, directory: Path = RELEASE_FILES_DIR) -> Path:
    """Write ``tag``'s catalog files, as raw TOML, to ``directory/<tag>/``; returns it."""
    files = catalog_files_at(tag, repo)
    target = directory / tag
    if target.exists():
        shutil.rmtree(target)
    return materialize(files, target)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--write-released",
        metavar="TAG",
        help=(
            "record TAG's catalog format (read from the tag) in "
            "tests/contract/catalog_format_snapshot.json and its files as a fixture "
            "(the release step)"
        ),
    )
    parser.add_argument(
        "--draft",
        action="store_true",
        help="record the working tree's format as a draft (released: null) before a tag",
    )
    parser.add_argument(
        "--record-files", metavar="TAG", help="write only TAG's catalog files as a fixture"
    )
    args = parser.parse_args(argv)
    if args.write_released is not None or args.draft:
        if args.write_released is not None and args.draft:
            parser.error("--write-released and --draft are exclusive")
        tag = None if args.draft else args.write_released.strip()
        if tag == "":
            parser.error("--write-released needs a tag; --draft records a draft")
        document = write_released(tag)
        fixture = record_files(tag) if tag else None
        counts = {name: len(kind["keys"]) for name, kind in document["kinds"].items()}
        print(
            json.dumps(
                {
                    "recorded": SNAPSHOT_PATH.relative_to(REPO).as_posix(),
                    "released": tag,
                    "files": fixture.relative_to(REPO).as_posix() if fixture else None,
                    "keys": counts,
                }
            )
        )
        return 0
    if args.record_files:
        fixture = record_files(args.record_files.strip())
        print(json.dumps({"recorded": fixture.relative_to(REPO).as_posix()}))
        return 0
    print(json.dumps(build_format(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
