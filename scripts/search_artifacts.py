"""Search file contents inside this repository (a read-only `grep` substitute).

Headless Claude Code sessions do not always expose a Grep tool, and the CI
permission settings (`.claude/settings.ci.json`) deny `grep` through Bash.
This script is the allowlisted way for a pipeline subagent to find lines in
the run's artifacts (or in the repo's own source tree) without reading a
whole file:

    uv run python scripts/search_artifacts.py '"query_id"' artifacts/<db>/<job>/schema-dynamodb/v2
    uv run python scripts/search_artifacts.py 'class TradeOff' src/contracts --context 5
    uv run python scripts/search_artifacts.py 'users' artifacts/<db> --glob 'schema_draft_group_*.json' --files-only

Output is grep-like: ``path:line:text`` for a match and ``path-line-text`` for
a context line, paths relative to the repo root, with ``--`` between
non-adjacent blocks. Long lines are truncated and the whole output is capped
so it stays inline in the session instead of being persisted to a file; a
final ``[search_artifacts] …`` line says when anything was cut.

Containment (always on, not only under MODERNIZER_CI_SANDBOX=1):

* the search path must resolve (symlinks followed) inside the repo root, so
  absolute paths elsewhere, ``..`` and escaping symlinks are refused;
* files found while walking a directory are skipped if they resolve outside
  the repo, and ``.git``/``.venv``/``node_modules``/``.local-ui`` and similar
  directories are never entered;
* ``.env``-style, key, certificate and credential files are never opened;
* binary files are skipped.

Under MODERNIZER_CI_SANDBOX=1 the path is also checked with
:func:`scripts._sandbox.sandbox_violation`, like every other allowlisted
script.

Exit status: 0 if anything matched, 1 if nothing did, 2 on an error (printed
as ``{"status": "error", "message": ...}``).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from scripts._sandbox import REPO_ROOT, sandbox_violation  # noqa: E402

DEFAULT_MAX_MATCHES = 200
MAX_CONTEXT = 20
MAX_LINE_CHARS = 400
# Claude Code persists Bash output above ~30k characters to a file the
# session then has to search again (issue #275); stay well under that.
MAX_OUTPUT_CHARS = 20_000
BINARY_SNIFF_BYTES = 8192

SKIP_DIRS = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "node_modules",
        "__pycache__",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        ".hypothesis",
        ".local-ui",
        ".aws",
        ".ssh",
    }
)

# Never opened, whether named directly or found while walking a directory.
SECRET_NAME_PATTERNS: tuple[str, ...] = (
    ".env",
    ".env.*",
    "*.env",
    ".envrc",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.jks",
    "*.keystore",
    "id_rsa*",
    "id_dsa*",
    "id_ecdsa*",
    "id_ed25519*",
    ".npmrc",
    ".netrc",
    ".pypirc",
    ".pgpass",
    ".git-credentials",
    "credentials",
    "credentials.*",
    "*credential*",
    "*secret*",
    "*.tfstate",
    "*.tfvars",
)


class SearchError(Exception):
    """A refused or invalid request; reported as a JSON error, exit 2."""


@dataclass
class SearchResult:
    lines: list[str] = field(default_factory=list)
    matches: int = 0
    files_matched: int = 0
    truncated_reason: str | None = None


def is_secret_name(name: str) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatchcase(lowered, pattern) for pattern in SECRET_NAME_PATTERNS)


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def resolve_search_path(raw: str, repo_root: Path) -> Path:
    """Resolve ``raw`` (relative to the cwd, like the other scripts) and refuse
    anything outside ``repo_root`` or inside a skipped/secret location."""
    root = repo_root.resolve()
    resolved = Path(raw).resolve()
    if not _inside(resolved, root):
        raise SearchError(f"path {raw!r} resolves outside the repository root ({root}); refused")
    rel_parts = resolved.relative_to(root).parts
    if any(is_secret_name(part) for part in (*rel_parts, Path(raw).name)):
        raise SearchError(f"path {raw!r} looks like a credential or secret file; refused")
    if any(part in SKIP_DIRS for part in rel_parts):
        raise SearchError(f"path {raw!r} is inside a directory this search never reads")
    if not resolved.exists():
        raise SearchError(f"path {raw!r} does not exist")
    return resolved


def _matches_glob(rel_path: str, name: str, glob: str | None) -> bool:
    if glob is None:
        return True
    pattern = glob.removeprefix("**/")
    return fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel_path, glob)


def iter_files(start: Path, repo_root: Path, glob: str | None) -> Iterator[Path]:
    """Yield readable, in-repo, non-secret files under ``start`` (sorted)."""
    root = repo_root.resolve()
    if start.is_file():
        candidates: Iterator[Path] = iter([start])
    else:

        def walk() -> Iterator[Path]:
            for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
                dirnames[:] = sorted(
                    d for d in dirnames if d not in SKIP_DIRS and not is_secret_name(d)
                )
                for filename in sorted(filenames):
                    yield Path(dirpath) / filename

        candidates = walk()

    for candidate in candidates:
        if is_secret_name(candidate.name):
            continue
        resolved = candidate.resolve()
        if not _inside(resolved, root) or not resolved.is_file():
            continue
        if is_secret_name(resolved.name):
            continue
        rel = resolved.relative_to(root).as_posix()
        if not _matches_glob(rel, resolved.name, glob):
            continue
        yield resolved


def _is_binary(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            return b"\0" in handle.read(BINARY_SNIFF_BYTES)
    except OSError:
        return True


def _clip(text: str) -> str:
    if len(text) <= MAX_LINE_CHARS:
        return text
    return text[:MAX_LINE_CHARS] + f"… [+{len(text) - MAX_LINE_CHARS} chars]"


def search(
    pattern: str,
    path: str,
    *,
    repo_root: Path = REPO_ROOT,
    context: int = 0,
    max_matches: int = DEFAULT_MAX_MATCHES,
    files_only: bool = False,
    ignore_case: bool = False,
    glob: str | None = None,
    max_output_chars: int = MAX_OUTPUT_CHARS,
) -> SearchResult:
    if context < 0 or context > MAX_CONTEXT:
        raise SearchError(f"--context must be between 0 and {MAX_CONTEXT}")
    if max_matches < 1:
        raise SearchError("--max-matches must be at least 1")
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        raise SearchError(f"invalid regular expression {pattern!r}: {exc}") from exc

    root = repo_root.resolve()
    start = resolve_search_path(path, root)
    result = SearchResult()
    used = 0

    def emit(line: str) -> bool:
        nonlocal used
        if used + len(line) + 1 > max_output_chars:
            result.truncated_reason = (
                f"output capped at {max_output_chars} characters; "
                "narrow the pattern or path, or lower --context"
            )
            return False
        result.lines.append(line)
        used += len(line) + 1
        return True

    for file_path in iter_files(start, root, glob):
        if _is_binary(file_path):
            continue
        rel = file_path.relative_to(root).as_posix()
        try:
            with file_path.open(encoding="utf-8", errors="replace") as handle:
                text_lines = handle.read().splitlines()
        except OSError:
            continue

        hits = [i for i, line in enumerate(text_lines) if regex.search(line)]
        hit_set = set(hits)
        if not hits:
            continue
        result.files_matched += 1

        if files_only:
            if not emit(rel):
                return result
            continue

        printed_upto = -1
        for i in hits:
            if result.matches >= max_matches:
                result.truncated_reason = (
                    f"stopped after {max_matches} matches (--max-matches); "
                    "narrow the pattern or raise --max-matches"
                )
                return result
            lo = max(0, i - context, printed_upto + 1)
            hi = min(len(text_lines) - 1, i + context)
            if context and printed_upto >= 0 and lo > printed_upto + 1 and not emit("--"):
                return result
            for j in range(lo, hi + 1):
                if j <= printed_upto:
                    continue
                sep = ":" if j in hit_set else "-"
                if not emit(f"{rel}{sep}{j + 1}{sep}{_clip(text_lines[j])}"):
                    return result
                printed_upto = j
            result.matches += 1
        if context and not emit("--"):
            return result

    if result.lines and result.lines[-1] == "--":
        result.lines.pop()
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Search file contents under the repository (read-only grep substitute)."
    )
    parser.add_argument("pattern", help="Python regular expression")
    parser.add_argument("path", help="File or directory inside the repository (e.g. artifacts/)")
    parser.add_argument(
        "--context", "-C", type=int, default=0, help=f"Lines of context (0-{MAX_CONTEXT})"
    )
    parser.add_argument(
        "--max-matches",
        type=int,
        default=DEFAULT_MAX_MATCHES,
        help=f"Stop after this many matching lines (default {DEFAULT_MAX_MATCHES})",
    )
    parser.add_argument(
        "--files-only", action="store_true", help="Print only the paths of matching files"
    )
    parser.add_argument("--ignore-case", "-i", action="store_true", help="Case-insensitive")
    parser.add_argument(
        "--glob", help="Only search files whose name or repo-relative path matches this glob"
    )
    return parser


def _error(message: str) -> NoReturn:
    print(json.dumps({"status": "error", "message": message}))
    sys.exit(2)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    violation = sandbox_violation(args, path_args=("path",), name_args=())
    if violation:
        _error(violation)

    try:
        result = search(
            args.pattern,
            args.path,
            context=args.context,
            max_matches=args.max_matches,
            files_only=args.files_only,
            ignore_case=args.ignore_case,
            glob=args.glob,
        )
    except SearchError as exc:
        _error(str(exc))

    for line in result.lines:
        print(line)
    if result.truncated_reason:
        print(f"[search_artifacts] truncated: {result.truncated_reason}")
    if not result.files_matched:
        print("[search_artifacts] no matches")
        sys.exit(1)


if __name__ == "__main__":
    main()
