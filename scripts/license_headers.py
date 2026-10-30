#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Add or check the license header on every source file (#1722).

Every source file we wrote carries the two-line SPDX header, in its language's
comment syntax, matching the copyright line of the root ``LICENSE``::

    # Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
    # SPDX-License-Identifier: MIT-0

Placement: a UTF-8 byte order mark stays first, then a shebang line, then a Python
encoding line (PEP 263) if there is one, then, in JS/TS, a leading ``/** ... */``
docblock that carries a tool pragma (``@jest-environment`` and the other ``@jest-``
pragmas, ``@flow``, ``@jsx``: their tools read only a docblock that opens the file),
then the header, then exactly one blank line before the code. Otherwise the JS/TS
header comes before any ``'use strict'`` directive (a comment before a directive
keeps it a directive). ``// @ts-check`` needs nothing special: TypeScript reads it
anywhere among the leading comments.

The header counts only in the file's leading comment block (the run of comment
lines after that preamble), never in a docstring or a string literal, and only
``SPDX-License-Identifier: MIT-0`` passes ``--check``: a file with another
identifier fails it, and ``--fix`` leaves that file alone rather than add a second
header. The file's line endings and trailing newline are kept as they are.

Out of scope: the directories in ``[tool.license-headers] exclude`` of
``pyproject.toml`` (the quarantined third-party snapshot, which keeps its own
provenance), vendored and build trees (``node_modules``, ``.venv``, ``build``,
``dist``, ...) and the files in ``EXCLUDED_FILES``, each with its reason.
Generated files (a "generated ... do not edit" marker near the top, or a file in
``GENERATED_FILES``) are never edited here: their generator emits the header, and
``--check`` still checks them so a generator that stops emitting it is caught.

Usage::

    uv run python scripts/license_headers.py --check            # list files without it, exit 1
    uv run python scripts/license_headers.py --fix              # add it where missing
    uv run python scripts/license_headers.py --check FILE ...   # only these (the pre-commit hook)

Without file arguments it scans every tracked or untracked, not ignored, file.
Running ``--fix`` twice changes nothing the second time.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess  # nosec B404 -- fixed argv, no shell
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

REPO_ROOT = Path(__file__).resolve().parents[1]

HEADER_LINES = (
    "Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.",
    "SPDX-License-Identifier: MIT-0",
)

#: Line-comment syntax per source extension.
COMMENT_PREFIX: dict[str, str] = {
    ".py": "#",
    ".sh": "#",
    ".js": "//",
    ".jsx": "//",
    ".mjs": "//",
    ".cjs": "//",
    ".ts": "//",
    ".tsx": "//",
    ".sql": "--",
}

#: Top-level directories that keep their own license (``[tool.license-headers]`` in
#: pyproject.toml, beside the other tools' skips of the same directories).
EXCLUDED_PREFIXES: tuple[str, ...] = tuple(
    tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"][
        "license-headers"
    ]["exclude"]
)

#: Path segments of vendored code and build outputs, anywhere in a path.
EXCLUDED_SEGMENTS = frozenset(
    {"node_modules", ".venv", "venv", "build", "dist", "site", "__pycache__", ".git"}
)

#: Individual files kept byte-for-byte, with the reason for each.
EXCLUDED_FILES: dict[str, str] = {}

#: Generated source files and the generator that writes (and must emit) their header.
GENERATED_FILES: dict[str, str] = {
    "src/ui/src/utils/engineCatalog.generated.js": "scripts/render_catalog.py",
}

#: The one identifier --check accepts.
LICENSE_ID = "MIT-0"

#: How many lines from the top a generated marker may sit.
_HEAD_LINES = 10

_BOM = "\ufeff"
_JS_SUFFIXES = frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"})
# Docblock pragmas a tool reads only when the docblock opens the file.
_PRAGMA_RE = re.compile(r"@(?:jest-|flow\b|noflow\b|jsx)")
# PEP 263: the encoding declaration must be on line 1 or 2.
_ENCODING_RE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*[-\w.]+")
_GENERATED_RE = re.compile(r"generated.*do not edit", re.IGNORECASE)


def in_scope(path: str) -> bool:
    """Whether the repo-relative ``path`` is a source file that needs the header."""
    p = PurePosixPath(path)
    if p.suffix not in COMMENT_PREFIX:
        return False
    if path.startswith(EXCLUDED_PREFIXES) or path in EXCLUDED_FILES:
        return False
    return not any(part in EXCLUDED_SEGMENTS for part in p.parts[:-1])


def _preamble(lines: list[str], path: str) -> int:
    """How many leading lines stay above the header: shebang, encoding line, pragma docblock."""
    keep = 0
    if lines and lines[0].startswith("#!"):
        keep = 1
    if path.endswith(".py") and keep < min(len(lines), 2) and _ENCODING_RE.match(lines[keep]):
        keep += 1
    if (
        PurePosixPath(path).suffix in _JS_SUFFIXES
        and keep < len(lines)
        and lines[keep].lstrip().startswith("/*")
    ):
        end = next((i for i in range(keep, len(lines)) if "*/" in lines[i]), None)
        if end is not None and _PRAGMA_RE.search("".join(lines[keep : end + 1])):
            keep = end + 1
    return keep


def _comment_re(path: str) -> re.Pattern[str]:
    if PurePosixPath(path).suffix in _JS_SUFFIXES:
        return re.compile(r"(?://|/\*+|\*+/?)")
    return re.compile(re.escape(COMMENT_PREFIX[PurePosixPath(path).suffix]))


def spdx_identifier(text: str, path: str) -> str | None:
    """The SPDX identifier in ``text``'s leading comment block, or None without one.

    The block is the run of comment (and blank) lines after the preamble (byte order
    mark, shebang, encoding line, pragma docblock); it ends at the first line of code,
    so a docstring or string literal holding the SPDX text never counts.
    """
    if text.startswith(_BOM):
        text = text[1:]
    lines = text.splitlines()
    comment = _comment_re(path)
    spdx = re.compile(comment.pattern + r"\s*SPDX-License-Identifier:\s*([^\s*]+)")
    for line in lines[_preamble(lines, path) :]:
        stripped = line.strip()
        if not stripped:
            continue
        if not comment.match(stripped):
            return None
        found = spdx.match(stripped)
        if found:
            return found.group(1)
    return None


def has_header(text: str, path: str) -> bool:
    """Whether ``text``'s leading comment block carries an SPDX identifier (of any license)."""
    return spdx_identifier(text, path) is not None


def is_generated(path: str, text: str) -> bool:
    """Whether a generator writes this file (so it is never edited here)."""
    if path in GENERATED_FILES or ".generated." in PurePosixPath(path).name:
        return True
    head = text.splitlines()[:_HEAD_LINES]
    return any(_GENERATED_RE.search(line) for line in head)


def _newline(text: str) -> str:
    first = text.find("\n")
    return "\r\n" if first > 0 and text[first - 1] == "\r" else "\n"


def add_header(text: str, path: str) -> str:
    """``text`` with the header added for ``path``'s language, or unchanged if it has one."""
    if has_header(text, path):
        return text
    bom = _BOM if text.startswith(_BOM) else ""
    text = text[len(bom) :]
    nl = _newline(text)
    prefix = COMMENT_PREFIX[PurePosixPath(path).suffix]
    header = [f"{prefix} {line}{nl}" for line in HEADER_LINES]

    lines = text.splitlines(keepends=True)
    keep = _preamble(lines, path)
    top = lines[:keep]
    if top and not top[-1].endswith(("\n", "\r")):
        top[-1] += nl
    rest = lines[keep:]
    while rest and not rest[0].strip():
        rest.pop(0)
    blank = [nl] if rest else []
    return bom + "".join(top + header + blank + rest)


def _list_files(root: Path) -> list[str]:
    out = subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout.decode("utf-8")
    return sorted({p for p in out.split("\0") if p})


def _relative(path: str, root: Path) -> str:
    p = Path(path)
    if p.is_absolute():
        p = p.resolve().relative_to(root.resolve())
    return p.as_posix()


def _read(file: Path) -> str:
    # newline="" keeps \r\n as written, so the file round-trips byte for byte.
    with file.open(encoding="utf-8", newline="") as fh:
        return fh.read()


def _write(file: Path, text: str) -> None:
    with file.open("w", encoding="utf-8", newline="") as fh:
        fh.write(text)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="list files without the header")
    mode.add_argument("--fix", action="store_true", help="add the header where missing")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help=argparse.SUPPRESS)
    parser.add_argument("files", nargs="*", help="only these files (default: the whole repo)")
    args = parser.parse_args(argv)

    root: Path = args.root
    paths = [_relative(f, root) for f in args.files] if args.files else _list_files(root)
    missing: list[str] = []
    fixed = checked = 0
    for path in paths:
        file = root / path
        if not in_scope(path) or not file.is_file():
            continue
        checked += 1
        text = _read(file)
        found = spdx_identifier(text, path)
        if found == LICENSE_ID:
            continue
        if found is not None:
            missing.append(f"{path} (SPDX-License-Identifier: {found}, expected {LICENSE_ID})")
            continue
        if args.fix and not is_generated(path, text):
            _write(file, add_header(text, path))
            fixed += 1
            continue
        if is_generated(path, text):
            generator = GENERATED_FILES.get(path, "its generator")
            missing.append(f"{path} (generated: make {generator} emit the header)")
        else:
            missing.append(path)

    if args.fix:
        print(f"license headers: added to {fixed} file(s)")
    if missing:
        print(f"license headers: {len(missing)} file(s) without the MIT-0 SPDX header:")
        for entry in missing:
            print(f"  {entry}")
        if args.check:
            print("Fix: uv run python scripts/license_headers.py --fix")
            return 1
    elif args.check:
        print(json.dumps({"license_headers": "ok", "checked": checked}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
