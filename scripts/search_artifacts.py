"""Search file contents inside this repository (a read-only `grep` substitute).

Headless Claude Code sessions do not always expose a Grep tool, and the CI
permission settings (`.claude/settings.ci.json`) deny `grep` through Bash.
This script is the allowlisted way for a pipeline subagent to find lines in
the run's artifacts (or in the repo's own source tree) without reading a
whole file:

    uv run python scripts/search_artifacts.py '"query_id"' artifacts/<db>/<job>/schema-dynamodb/v2
    uv run python scripts/search_artifacts.py 'class TradeOff' src/contracts --context 5
    uv run python scripts/search_artifacts.py 'users' artifacts/<db> --glob 'schema_draft_group_*.json' --files-only
    uv run python scripts/search_artifacts.py '"query_id"' artifacts/<db>/<job> --count

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

Resource limits: the pattern is at most 500 characters, and the whole
search has a 20-second wall-clock budget (POSIX ``setitimer``); a pattern
that blows it, such as a catastrophic-backtracking ``(a+)+$``, ends with a
JSON error, exit 2. A line longer than 20,000 characters is searched only in
its first and last 20,000 characters, separately: a match in the head counts
only if it ends before the cut, and a match in the tail only if it starts
after it, so ``$``, ``^``, ``\b`` and lookaheads never match at an artificial
cut-off. Matches that would span the unsearched middle are missed; a final
``[search_artifacts] N lines were only searched …`` line says when this
applied. Such lines are still printed, clipped.

Error messages never echo the pattern or the path.

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
import signal
import sys
from collections.abc import Iterator
from contextlib import contextmanager
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
MAX_PATTERN_CHARS = 500
# Longer lines are searched only in this many characters from each end;
# generated JSON can have single lines of ~100k characters.
MAX_MATCH_CHARS = 20_000
TIME_BUDGET_SECONDS = 20.0

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
        ".docker",
        ".kube",
        ".gnupg",
    }
)
# Multi-component directories never entered (compared case-insensitively).
SKIP_DIR_PATHS: tuple[tuple[str, ...], ...] = ((".config", "gcloud"),)

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
    "*.crt",
    "*.cer",
    "*.der",
    "*.ppk",
    "*.p8",
    "*.asc",
    "*.gpg",
    ".htpasswd",
    ".boto",
    "*token*",
    "kubeconfig",
    "kubeconfig.*",
    "*sa-key*.json",
    "*service-account*.json",
)


class SearchError(Exception):
    """A refused or invalid request; reported as a JSON error, exit 2."""


class SearchTimeout(SearchError):
    """The search ran past its wall-clock budget."""


@dataclass
class SearchResult:
    lines: list[str] = field(default_factory=list)
    matches: int = 0
    files_matched: int = 0
    truncated_reason: str | None = None
    capped_lines: int = 0


def is_secret_name(name: str) -> bool:
    lowered = name.lower()
    return any(fnmatch.fnmatchcase(lowered, pattern) for pattern in SECRET_NAME_PATTERNS)


def _inside(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _is_skipped_dir(name: str) -> bool:
    return name.lower() in SKIP_DIRS


def refusal_reason(parts: tuple[str, ...]) -> str | None:
    """Why a repo-relative path (as ``parts``) must not be read, or ``None``.

    The single check for both the search path and every file found while
    walking (after resolving symlinks), so a link cannot reach a skipped
    directory or a secret file the direct path would be refused for."""
    lowered = tuple(part.lower() for part in parts)
    if any(is_secret_name(part) for part in lowered):
        return "path looks like a credential or secret file; refused"
    if any(_is_skipped_dir(part) for part in lowered):
        return "path is inside a directory this search never reads; refused"
    for skip in SKIP_DIR_PATHS:
        n = len(skip)
        if any(lowered[i : i + n] == skip for i in range(len(lowered) - n + 1)):
            return "path is inside a directory this search never reads; refused"
    return None


def _safe_resolve(path: Path) -> Path | None:
    """``path.resolve()``, or ``None`` on a symlink loop, an over-long name,
    an embedded NUL or another OS error."""
    try:
        return path.resolve()
    except (OSError, RuntimeError, ValueError):
        return None


def resolve_search_path(raw: str, repo_root: Path) -> Path:
    """Resolve ``raw`` (relative to the cwd, like the other scripts) and refuse
    anything outside ``repo_root`` or inside a skipped/secret location."""
    root = repo_root.resolve()
    resolved = _safe_resolve(Path(raw))
    if resolved is None:
        raise SearchError("path cannot be resolved (symlink loop, invalid or too long); refused")
    if not _inside(resolved, root):
        raise SearchError("path resolves outside the repository root; refused")
    reason = refusal_reason((*resolved.relative_to(root).parts, Path(raw).name))
    if reason:
        raise SearchError(reason)
    try:
        exists = resolved.exists()
    except (OSError, ValueError):
        exists = False
    if not exists:
        raise SearchError("path does not exist")
    return resolved


def _matches_glob(name: str, rel_paths: tuple[str, ...], glob: str | None) -> bool:
    """``glob`` against the file name, or against any of ``rel_paths`` (the
    repo-relative path and the path relative to the search start)."""
    if glob is None:
        return True
    pattern = glob.removeprefix("**/")
    if fnmatch.fnmatch(name, pattern):
        return True
    return any(fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(rel, pattern) for rel in rel_paths)


def iter_files(start: Path, repo_root: Path, glob: str | None) -> Iterator[Path]:
    """Yield readable, in-repo, non-secret files under ``start`` (sorted)."""
    root = repo_root.resolve()
    if start.is_file():
        candidates: Iterator[Path] = iter([start])
    else:

        def walk() -> Iterator[Path]:
            for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
                dirnames[:] = sorted(
                    d for d in dirnames if not _is_skipped_dir(d) and not is_secret_name(d)
                )
                for filename in sorted(filenames):
                    yield Path(dirpath) / filename

        candidates = walk()

    start_dir = start if start.is_dir() else start.parent
    for candidate in candidates:
        if is_secret_name(candidate.name):
            continue
        resolved = _safe_resolve(candidate)
        if resolved is None or not _inside(resolved, root):
            continue
        try:
            if not resolved.is_file():
                continue
        except OSError:
            continue
        if refusal_reason(resolved.relative_to(root).parts):
            continue
        rel = resolved.relative_to(root).as_posix()
        from_start = candidate.relative_to(start_dir).as_posix()
        if not _matches_glob(resolved.name, (rel, from_start), glob):
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


def line_matches(regex: re.Pattern[str], line: str, cap: int = MAX_MATCH_CHARS) -> bool:
    """``regex`` matches ``line``; lines longer than ``cap`` are searched in
    their first and last ``cap`` characters only, ignoring any match that
    touches the cut (so anchors and lookarounds cannot fire there)."""
    if len(line) <= cap:
        return regex.search(line) is not None
    head, tail = line[:cap], line[-cap:]
    if any(m.end() < cap for m in regex.finditer(head)):
        return True
    return any(m.start() > 0 for m in regex.finditer(tail))


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
    count: bool = False,
    max_output_chars: int = MAX_OUTPUT_CHARS,
) -> SearchResult:
    if context < 0 or context > MAX_CONTEXT:
        raise SearchError(f"--context must be between 0 and {MAX_CONTEXT}")
    if max_matches < 1:
        raise SearchError("--max-matches must be at least 1")
    if files_only and count:
        raise SearchError("use either --files-only or --count, not both")
    if len(pattern) > MAX_PATTERN_CHARS:
        raise SearchError(f"pattern is longer than {MAX_PATTERN_CHARS} characters")
    try:
        regex = re.compile(pattern, re.IGNORECASE if ignore_case else 0)
    except re.error as exc:
        where = f" at position {exc.pos}" if exc.pos is not None else ""
        raise SearchError(f"invalid regular expression: {exc.msg}{where}") from exc

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

        hits = []
        for i, line in enumerate(text_lines):
            if len(line) > MAX_MATCH_CHARS:
                result.capped_lines += 1
            if line_matches(regex, line):
                hits.append(i)
        hit_set = set(hits)
        if not hits:
            continue
        result.files_matched += 1

        if files_only or count:
            result.matches += len(hits)
            if not emit(f"{rel}:{len(hits)}" if count else rel):
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
    parser.add_argument(
        "--count", action="store_true", help="Print path:N (matching lines) per matching file"
    )
    parser.add_argument("--ignore-case", "-i", action="store_true", help="Case-insensitive")
    parser.add_argument(
        "--glob",
        help="Only search files whose name, repo-relative path or path relative to "
        "the search path matches this glob",
    )
    return parser


@contextmanager
def time_budget(seconds: float) -> Iterator[None]:
    """Raise :class:`SearchTimeout` if the body runs longer than ``seconds``.

    Uses ``SIGALRM``/``setitimer`` (POSIX, main thread); the regex engine
    checks for signals, so a catastrophic-backtracking match is interrupted.
    A no-op where that is unavailable."""
    if not hasattr(signal, "setitimer"):
        yield
        return

    def _expired(signum: int, frame: object) -> None:
        raise SearchTimeout("regex exceeded time budget; simplify the pattern")

    previous = signal.signal(signal.SIGALRM, _expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _error(message: str) -> NoReturn:
    print(json.dumps({"status": "error", "message": message}))
    sys.exit(2)


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    if sandbox_violation(args, path_args=("path",), name_args=()):
        # The sandbox message echoes the argument; keep this script's errors generic.
        _error("path resolves outside the repository root; refused under MODERNIZER_CI_SANDBOX=1")

    try:
        with time_budget(TIME_BUDGET_SECONDS):
            result = search(
                args.pattern,
                args.path,
                context=args.context,
                max_matches=args.max_matches,
                files_only=args.files_only,
                count=args.count,
                ignore_case=args.ignore_case,
                glob=args.glob,
            )
    except SearchError as exc:
        _error(str(exc))

    for line in result.lines:
        print(line)
    if result.capped_lines:
        print(
            f"[search_artifacts] {result.capped_lines} "
            f"{'line was' if result.capped_lines == 1 else 'lines were'} only searched up to "
            f"{MAX_MATCH_CHARS} chars from each end"
        )
    if result.truncated_reason:
        print(f"[search_artifacts] truncated: {result.truncated_reason}")
    if not result.files_matched:
        print("[search_artifacts] no matches")
        sys.exit(1)


if __name__ == "__main__":
    main()
