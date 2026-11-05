#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Revert each changed hunk, one at a time, and report the ones no test notices (#1852).

AGENTS.md's self-review item 2: revert each part of the fix and check that some test
fails. This script does it per hunk. It lists the hunks of the committed diff between
the merge base with ``--base`` and HEAD (``git diff -U0``, renames followed) in files
under ``src/`` and ``scripts/``, makes disposable worktrees of HEAD, and for each hunk
puts back the merge base's lines for that hunk only, runs the branch's new and changed
tests (``ci/main_tests.py``'s ``run`` and ``marked`` ids, so ``tests/e2e`` and
``tests/integration`` are not run), and restores the file. A hunk whose revert fails
none of them is ``unnoticed``: no test this branch adds or changes shows that the
hunk matters. Lines are split as git splits them, on ``\\n`` only.

Before any revert, the same tests run once on HEAD in a worktree; if they fail there,
nothing can be judged and the script exits 2. The runs pass ``--fail-on-skip`` when the
worktree's ``tests/conftest.py`` or ``conftest.py`` defines it (as ``ci/test.sh`` does),
so a skipped test fails the baseline; without it the baseline prints its skip count, as
a skipped test notices nothing. With no new or changed test, every checkable hunk is
reported unnoticed and nothing runs.

Skipped, and listed with the reason: a Python hunk whose revert leaves the file's AST
unchanged, docstrings included (only comments or formatting changed; ``whitespace
only`` when only whitespace differs), a hunk under ``src/ui/`` (its tests are Jest, not
pytest), a binary file and a file that is not UTF-8. Nothing else is skipped: a
docstring is behaviour here (contract schemas, tool specs, ``--help``), and in other
file types a ``#`` line or a blank line can be data (YAML block scalars, TOML multi-line
strings, heredocs, Markdown prompts), so those hunks run. A Python file that does not
parse is checked.

Bounds: ``--max-hunks`` (default 40) checkable hunks are run, in diff order, and the
rest are listed as ``capped``; each pytest run has ``--timeout`` seconds (default 300),
after which its process group is killed and the hunk is ``timeout`` (undecided, never
unnoticed); ``--jobs N`` (default 1) runs N worktrees in parallel. A progress line is
printed per run.

What it does not see: uncommitted edits (it reads HEAD's commit, and warns when
``src/``, ``scripts/`` or ``tests/`` has any), deleted files, a hunk only an existing,
unchanged test notices (only the branch's new and changed tests run), and whether a
test that fails fails for the right reason.

It never writes to the caller's working tree. The worktrees live in a temporary
``hunk-revert-*`` folder holding an ``owner`` file (this pid) and a ``pgids`` file (the
pytest process groups), each pid with its start time from ``ps``; the worktrees are
added ``--lock``-ed with the reason ``hunk-revert pid <pid>``. On exit, and on the first SIGINT, SIGTERM or SIGHUP, it
ignores those signals from then on, kills every pytest process group and removes the
worktrees and the folder; a second Ctrl-C does not cut that short. A run killed
outright (SIGKILL) cannot clean up, so every run starts with a sweep: a ``hunk-revert``
worktree or folder whose owner is gone is removed (a folder with no owner file only
once it is a minute old), and each recorded pytest run that still runs is killed with its
process group and every descendant (found from ``ps``'s parent links, so a child that
started its own session goes too); a run that times out or is interrupted is killed the
same way. ``ps`` runs as ``ps -ww`` in the C locale and UTC, so its output is never cut
to a terminal width and start times compare across runs. A process counts as gone when ``ps`` lists no live process with that pid and
start time: a zombie is gone, and a reused pid is not the recorded process. Then
``git worktree prune`` drops entries whose folder is gone (any worktree's,
as the main-tests step does). Tests run with this interpreter, ``PYTHONPATH`` on the
worktree (checked: ``src`` must import from there) and ``PYTHONDONTWRITEBYTECODE=1``,
and every ``__pycache__`` folder in the worktree is removed after each restore, so a
stale ``.pyc`` never stands in for a reverted file.

Usage::

    uv run python ci/hunk_revert.py [--base REF] [--max-hunks N] [--timeout S]
                                     [--jobs N] [--strict]

Exit 0 with the report (advisory); with ``--strict``, 1 when a hunk is unnoticed;
2 when it cannot judge (no merge base, a worktree or the baseline run failed); 130 on
an interrupt. ``ci/pre_merge.sh --mutation`` runs it with ``--strict`` and only warns.
"""

from __future__ import annotations

import argparse
import ast
import os
import queue
import re
import shutil
import signal
import subprocess  # nosec B404 -- runs git and pytest with fixed argv
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

try:
    from ci import main_tests
except ImportError:  # run as a script: ci/ is sys.path[0]
    import main_tests  # type: ignore[no-redef]

ROOTS = ("src", "scripts")
UI_PREFIX = "src/ui/"
PREFIX = "hunk-revert-"
LOCK_REASON = "hunk-revert pid "
SIGNALS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
STALE_SECONDS = 60
_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


@dataclass(frozen=True)
class Hunk:
    """One ``git diff -U0`` hunk: ``path`` at HEAD, ``old_path`` at the merge base
    (``None`` for an added file), and the header's four numbers."""

    path: str
    old_path: str | None
    old_start: int
    old_count: int
    new_start: int
    new_count: int

    @property
    def location(self) -> str:
        """``path:line``: the first changed line at HEAD, or for a pure deletion the
        line the deleted lines would come back before."""
        line = self.new_start if self.new_count else self.new_start + 1
        return f"{self.path}:{line}"


def parse_hunk_headers(patch: str) -> list[tuple[int, int, int, int]]:
    """``(old start, old count, new start, new count)`` per ``@@`` header (a missing
    count is 1)."""
    return [
        (int(a), int(b) if b else 1, int(c), int(d) if d else 1)
        for a, b, c, d in _HEADER.findall(patch.replace("\r\n", "\n"))
    ]


def parse_name_status_z(output: str) -> list[tuple[str, str | None]]:
    """``(path at HEAD, path at the base)`` from ``git diff --name-status -z -M``
    (``None`` for an added file); deletions and other statuses are left out."""
    tokens = output.split("\0")
    out: list[tuple[str, str | None]] = []
    i = 0
    while i < len(tokens) and tokens[i]:
        status = tokens[i][:1]
        if status in ("R", "C"):
            out.append((tokens[i + 2], tokens[i + 1]))
            i += 3
            continue
        if status == "A":
            out.append((tokens[i + 1], None))
        elif status == "M":
            out.append((tokens[i + 1], tokens[i + 1]))
        i += 2
    return out


def split_lines(text: str) -> list[str]:
    """Lines as git counts them: split on ``\\n`` only, each keeping its ``\\n``
    (``str.splitlines`` also splits on a form feed, a lone ``\\r`` and others)."""
    parts = text.split("\n")
    return [p + "\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _old_and_new(head: str, base: str, hunk: Hunk) -> tuple[list[str], list[str], int]:
    base_lines = split_lines(base)
    old = base_lines[hunk.old_start - 1 : hunk.old_start - 1 + hunk.old_count]
    start = hunk.new_start - 1 if hunk.new_count else hunk.new_start
    head_lines = split_lines(head)
    return old, head_lines[start : start + hunk.new_count], start


def revert(head: str, base: str, hunk: Hunk) -> str:
    """HEAD's text with this hunk's lines replaced by the merge base's."""
    old, _, start = _old_and_new(head, base, hunk)
    head_lines = split_lines(head)
    return "".join(head_lines[:start] + old + head_lines[start + hunk.new_count :])


def _code(source: str) -> str | None:
    """The module's AST dump (docstrings included), ``None`` when it does not parse."""
    try:
        return ast.dump(ast.parse(source))
    except (SyntaxError, ValueError):
        return None


def skip_reason(path: str, head: str, reverted: str) -> str | None:
    """Why reverting this hunk (HEAD's text to ``reverted``) is not worth a run, or
    ``None`` to run it. Only Python comments and formatting are skipped."""
    if path.startswith(UI_PREFIX):
        return "UI code: this step runs pytest only"
    if not path.endswith(".py"):
        return None
    before = _code(head)
    if before is None or before != _code(reverted):
        return None
    if "".join(head.split()) == "".join(reverted.split()):
        return "whitespace only"
    return "comment or formatting only"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(  # nosec B603 B607 -- fixed argv
        ["git", *args], cwd=cwd, capture_output=True, check=False
    )


def _show(cwd: Path, ref: str, path: str) -> bytes:
    return _git(cwd, "show", f"{ref}:{path}").stdout


@dataclass
class Item:
    hunk: Hunk
    head: str
    reverted: str
    snippet: str


def collect(repo: Path, merge_base: str) -> tuple[list[Item], list[tuple[str, str]]]:
    """The checkable hunks, and ``(location, reason)`` for the skipped ones."""
    names = _git(
        repo,
        "diff",
        "--name-status",
        "-z",
        "-M",
        "--diff-filter=AMR",
        merge_base,
        "HEAD",
        "--",
        *ROOTS,
    ).stdout.decode("utf-8", "surrogateescape")
    items: list[Item] = []
    skipped: list[tuple[str, str]] = []
    for path, old_path in parse_name_status_z(names):
        if PurePosixPath(path).parts[:1] not in [(r,) for r in ROOTS]:
            continue
        paths = [p for p in (old_path, path) if p] if old_path != path else [path]
        patch = _git(
            repo,
            "diff",
            "-U0",
            "-M",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            merge_base,
            "HEAD",
            "--",
            *paths,
        ).stdout
        try:
            head = _show(repo, "HEAD", path).decode("utf-8")
            base = _show(repo, merge_base, old_path).decode("utf-8") if old_path else ""
        except UnicodeDecodeError:
            skipped.append((path, "not UTF-8 text"))
            continue
        if b"\nBinary files " in b"\n" + patch or b"\nGIT binary patch" in patch:
            skipped.append((path, "binary file"))
            continue
        for header in parse_hunk_headers(patch.decode("utf-8", "replace")):
            hunk = Hunk(path, old_path, *header)
            reverted = revert(head, base, hunk)
            reason = skip_reason(path, head, reverted)
            if reason:
                skipped.append((hunk.location, reason))
                continue
            old, new = _old_and_new(head, base, hunk)[:2]
            first = next((ln.strip() for ln in (new or old) if ln.strip()), "")
            snippet = ("+ " if new else "- ") + (first[:60] + ("..." if len(first) > 60 else ""))
            items.append(Item(hunk, head, reverted, snippet))
    return items, skipped


def _ps(*args: str) -> str:
    """``ps -ww`` output in a fixed format: no width limit (procps cuts a line to
    ``COLUMNS`` otherwise), the C locale and UTC, so a start time recorded by one run
    compares equal in the next."""
    env = {**os.environ, "LC_ALL": "C", "TZ": "UTC"}
    return subprocess.run(  # nosec B603 B607 -- fixed argv
        ["ps", "-ww", *args], capture_output=True, text=True, check=False, env=env
    ).stdout


def identity(pid: int) -> str | None:
    """The start time ``ps`` reports for ``pid``, or ``None`` when no live process has
    that pid. A zombie counts as gone (``os.kill(pid, 0)`` succeeds on one), and the start
    time tells a reused pid from the process that was recorded."""
    stat, _, start = _ps("-o", "stat=,lstart=", "-p", str(pid)).strip().partition(" ")
    if not stat or stat.startswith("Z"):
        return None
    return " ".join(start.split()) or None


def _record(pid: int) -> str:
    """One ``owner`` or ``pgids`` line: the pid and its start time, tab-separated."""
    return f"{pid}\t{identity(pid) or '-'}\n"


def _recorded(path: Path) -> list[tuple[int, str | None]]:
    """``(pid, start time or None)`` per line of an ``owner`` or ``pgids`` file."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    out: list[tuple[int, str | None]] = []
    for line in text.splitlines():
        pid, _, start = line.partition("\t")
        if pid.strip().isdigit():
            out.append((int(pid), None if start.strip() in ("", "-") else start.strip()))
    return out


def _same_process(pid: int, start: str | None) -> bool:
    """Whether the recorded process still runs: alive, not a zombie, and (when its start
    time was recorded) started at that time, so a reused pid does not count."""
    now = identity(pid)
    return now is not None and (start is None or now == start)


def _descendants(pid: int) -> list[int]:
    """Every live descendant of ``pid``, from ``ps``'s parent links (a child that started
    its own session or group is still a child)."""
    children: dict[int, list[int]] = {}
    for line in _ps("-A", "-o", "pid=,ppid=").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append(int(parts[0]))
    out: list[int] = []
    todo = list(children.get(pid, []))
    while todo:
        child = todo.pop()
        out.append(child)
        todo += children.get(child, [])
    return out


def kill_tree(pid: int) -> None:
    """SIGKILL a pytest run: its process group (it leads a session of its own) and every
    descendant, found before the kill, with each one's group too."""
    victims = [pid, *_descendants(pid)]
    for victim in victims:
        for kill in (os.killpg, os.kill):
            try:
                kill(victim, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def _registered(repo: Path) -> list[Path]:
    """Every worktree path of ``repo``."""
    out = _git(repo, "worktree", "list", "--porcelain").stdout.decode("utf-8", "replace")
    prefix = "worktree "
    return [Path(ln[len(prefix) :]) for ln in out.splitlines() if ln.startswith(prefix)]


def sweep(repo: Path, tmp: Path | None = None) -> tuple[int, int, int]:
    """Remove what an earlier run killed outright left behind; ``(worktrees, folders,
    pytest runs killed)``. A worktree or folder whose owner still runs is left alone."""
    tmp = tmp or Path(tempfile.gettempdir())
    me = os.getpid()

    def stale(folder: Path) -> bool:
        owner = _recorded(folder / "owner")
        if owner:
            pid, start = owner[0]
            return pid != me and not _same_process(pid, start)
        try:
            return time.time() - folder.stat().st_mtime > STALE_SECONDS
        except OSError:
            return True  # the folder is gone: only the registration is left

    killed = 0

    def clear(folder: Path) -> None:
        nonlocal killed
        for pid, start in _recorded(folder / "pgids"):
            # Only the recorded process (pid and start time), never a reused pid.
            if start is not None and _same_process(pid, start):
                kill_tree(pid)
                killed += 1
        shutil.rmtree(folder, ignore_errors=True)

    trees = folders = 0
    for path in _registered(repo):
        folder = path.parent
        if not folder.name.startswith(PREFIX) or not stale(folder):
            continue
        # Remove the registration first (twice --force: it is locked), then the folder.
        _git(repo, "worktree", "remove", "--force", "--force", str(path))
        clear(folder)
        trees += 1
    _git(repo, "worktree", "prune")
    for folder in sorted(tmp.glob(PREFIX + "*")):
        if folder.is_dir() and stale(folder):
            clear(folder)
            folders += 1
    return trees, folders, killed


def _remove_pycache(tree: Path) -> None:
    for dirpath, dirnames, _ in os.walk(tree):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules")]
        if "__pycache__" in dirnames:
            shutil.rmtree(Path(dirpath) / "__pycache__", ignore_errors=True)
            dirnames.remove("__pycache__")


class Runner:
    """A pool of disposable worktrees of HEAD and the pytest runs in them."""

    def __init__(self, repo: Path, jobs: int, timeout: float, node_ids: list[str]) -> None:
        self.repo = repo
        self.timeout = timeout
        self.node_ids = node_ids
        self.root = Path(tempfile.mkdtemp(prefix=PREFIX))
        (self.root / "owner").write_text(_record(os.getpid()), encoding="utf-8")
        self.trees: list[Path] = []
        self.free: queue.Queue[Path] = queue.Queue()
        self.live: set[subprocess.Popen[bytes]] = set()
        self.lock = threading.Lock()
        self.stopping = False
        self.jobs = jobs
        self.extra: list[str] = []

    def setup(self) -> str | None:
        """Make the worktrees; an error message, or ``None``."""
        for i in range(self.jobs):
            tree = self.root / f"wt{i}"
            proc = _git(
                self.repo, "worktree", "add", "-q", "--detach", "--lock",
                "--reason", f"{LOCK_REASON}{os.getpid()}", str(tree), "HEAD",
            )  # fmt: skip
            if proc.returncode != 0:
                return f"could not make a worktree: {proc.stderr.decode(errors='replace').strip()}"
            self.trees.append(tree)
            self.free.put(tree)
        tree = self.trees[0]
        where = subprocess.run(  # nosec B603 -- fixed argv: this interpreter
            [sys.executable, "-c", "import os, src; print(os.path.realpath(src.__path__[0]))"],
            cwd=tree,
            env=self._env(tree),
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        if where != str((tree / "src").resolve()):
            return f"src/ did not import from the worktree (got {where or 'nothing'})"
        if any('"--fail-on-skip"' in _text(tree / c) for c in ("tests/conftest.py", "conftest.py")):
            self.extra = ["--fail-on-skip"]
        return None

    def _env(self, tree: Path) -> dict[str, str]:
        return {**os.environ, "PYTHONPATH": str(tree), "PYTHONDONTWRITEBYTECODE": "1"}

    def pytest(self, tree: Path) -> tuple[str, float, str]:
        """``(outcome, seconds, output)``: ``pass``, ``fail`` or ``timeout``."""
        argv = [
            sys.executable, "-m", "pytest", "-q", "-x", "-o", "addopts=",
            "-p", "no:cacheprovider", "-p", "no:xdist", *self.extra, *self.node_ids,
        ]  # fmt: skip
        started = time.monotonic()
        with self.lock:
            if self.stopping:
                return "fail", 0.0, "stopped"
            proc = subprocess.Popen(  # nosec B603 -- fixed argv: this interpreter, pytest, ids
                argv,
                cwd=tree,
                env=self._env(tree),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # its own process group: a timeout kills all of it
            )
            self.live.add(proc)
            with (self.root / "pgids").open("a", encoding="utf-8") as fh:
                fh.write(_record(proc.pid))
        try:
            out, _ = proc.communicate(timeout=self.timeout)
            outcome = "pass" if proc.returncode == 0 else "fail"
        except subprocess.TimeoutExpired:
            kill_tree(proc.pid)
            out, _ = proc.communicate()
            outcome = "timeout"
        except BaseException:  # an interrupt while waiting: the run must not outlive it
            kill_tree(proc.pid)
            raise
        finally:
            with self.lock:
                self.live.discard(proc)
        return outcome, time.monotonic() - started, out.decode("utf-8", "replace")

    def check(self, item: Item) -> tuple[str, float]:
        tree = self.free.get()
        target = tree / item.hunk.path
        try:
            target.write_bytes(item.reverted.encode("utf-8"))
            outcome, seconds, _ = self.pytest(tree)
        finally:
            target.write_bytes(item.head.encode("utf-8"))
            _remove_pycache(tree)
            self.free.put(tree)
        if outcome == "timeout":
            return "timeout", seconds
        return ("noticed" if outcome == "fail" else "unnoticed"), seconds

    def stop(self) -> None:
        """Start no new run and kill every running pytest process group."""
        with self.lock:
            self.stopping = True
            for proc in list(self.live):
                kill_tree(proc.pid)

    def cleanup(self) -> None:
        for tree in self.trees:
            _git(self.repo, "worktree", "remove", "--force", "--force", str(tree))
        shutil.rmtree(self.root, ignore_errors=True)
        _git(self.repo, "worktree", "prune")


def _text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def _say(line: str) -> None:
    """Print, unless the reader is gone (a Ctrl-C also kills ``| tee``)."""
    try:
        print(line, flush=True)
    except OSError:
        pass


def _ignore_signals() -> None:
    for sig in SIGNALS:
        signal.signal(sig, signal.SIG_IGN)


def _interrupt(signum: int, frame: object) -> None:
    _ignore_signals()  # first thing: a second signal cannot cut the cleanup short
    raise KeyboardInterrupt


_SKIPPED = re.compile(r"\b(\d+) skipped\b")


def run(args: argparse.Namespace) -> int:
    repo_out = _git(Path.cwd(), "rev-parse", "--show-toplevel")
    if repo_out.returncode != 0:
        print("error: not in a git repository", file=sys.stderr)
        return 2
    repo = Path(repo_out.stdout.decode().strip())
    swept = sweep(repo)
    if any(swept):
        print(
            f"sweep: removed {swept[0]} worktree(s) and {swept[1]} folder(s) and killed "
            f"{swept[2]} pytest run(s) an earlier, killed run left behind"
        )
    mb = _git(repo, "merge-base", "HEAD", args.base)
    if mb.returncode != 0:
        print(f"error: no merge base between HEAD and {args.base}", file=sys.stderr)
        return 2
    merge_base = mb.stdout.decode().strip()
    os.chdir(repo)  # main_tests runs git in the current folder
    node_ids = [
        line.split(" ", 1)[1]
        for line in main_tests.plan(merge_base)
        if line.startswith(("run ", "marked "))
    ]
    items, skipped = collect(repo, merge_base)
    dirty = _git(repo, "status", "--porcelain", "--", *ROOTS, "tests").stdout.strip()
    print(
        f"hunk-revert: {len(items) + len(skipped)} hunk(s) under {', '.join(ROOTS)} against "
        f"{args.base} (merge base {merge_base[:9]}); {len(node_ids)} new or changed test(s)"
    )
    if dirty:
        print("hunk-revert: uncommitted edits are not checked; it reads HEAD's commit")
    for location, reason in skipped:
        print(f"skip {location} ({reason})")
    capped = items[args.max_hunks :]
    items = items[: args.max_hunks]
    results: dict[int, tuple[str, float]] = {}
    if items and not node_ids:
        print("hunk-revert: no new or changed test: every hunk goes unnoticed, nothing is run")
        results = {i: ("unnoticed", 0.0) for i in range(len(items))}
    elif items:
        executor: ThreadPoolExecutor | None = None
        runner: Runner | None = None
        try:
            runner = Runner(repo, max(1, min(args.jobs, len(items))), args.timeout, node_ids)
            print(f"worktrees: {runner.root}", flush=True)
            error = runner.setup()
            if error:
                print(f"error: {error}")
                return 2
            outcome, seconds, out = runner.pytest(runner.trees[0])
            if outcome != "pass":
                print("\n".join(out.splitlines()[-15:]))
                print(
                    f"error: the new and changed tests fail on the branch itself ({outcome}, "
                    f"{seconds:.1f}s); fix them first, nothing can be judged"
                )
                return 2
            m = _SKIPPED.search(out.splitlines()[-1] if out.strip() else "")
            note = (
                f"; {m.group(1)} skipped: a skipped test notices nothing"
                if m and not runner.extra
                else ""
            )
            print(f"baseline: the tests pass on HEAD ({seconds:.1f}s{note})", flush=True)
            executor = ThreadPoolExecutor(max_workers=len(runner.trees))
            futures = {executor.submit(runner.check, item): i for i, item in enumerate(items)}
            for done, future in enumerate(as_completed(futures), 1):
                i = futures[future]
                results[i] = future.result()
                verdict, seconds = results[i]
                print(
                    f"[{done}/{len(items)}] {verdict} {items[i].hunk.location} ({seconds:.1f}s)",
                    flush=True,
                )
        except KeyboardInterrupt:
            _say("hunk-revert: interrupted; removing the worktrees")
            return 130
        finally:
            _ignore_signals()  # from here on, nothing interrupts the cleanup
            if runner is not None:
                runner.stop()
                if executor is not None:
                    executor.shutdown(wait=True, cancel_futures=True)
                runner.cleanup()
    for i, item in enumerate(items):
        print(f"{results[i][0]} {item.hunk.location} {item.snippet}")
    for item in capped:
        print(f"capped {item.hunk.location} {item.snippet}")
    counts = {
        v: sum(1 for r in results.values() if r[0] == v)
        for v in ("noticed", "unnoticed", "timeout")
    }
    cap_note = f"; {len(capped)} over the cap (--max-hunks {args.max_hunks})" if capped else ""
    print(
        f"hunk-revert: summary: {len(items)} checked: {counts['noticed']} noticed, "
        f"{counts['unnoticed']} unnoticed, {counts['timeout']} timed out; "
        f"{len(skipped)} skipped{cap_note}"
    )
    return 1 if args.strict and counts["unnoticed"] else 0


def parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("--base", default="origin/main", help="compare with this ref's merge base")
    ap.add_argument("--max-hunks", type=int, default=40, help="hunks to run (default 40)")
    ap.add_argument("--timeout", type=float, default=300, help="seconds per run (default 300)")
    ap.add_argument("--jobs", type=int, default=1, help="worktrees run in parallel (default 1)")
    ap.add_argument("--strict", action="store_true", help="exit 1 when a hunk is unnoticed")
    return ap


def main(argv: list[str]) -> int:
    ap = parser()
    args = ap.parse_args(argv)
    if args.max_hunks < 0 or args.jobs < 1 or args.timeout <= 0:
        ap.error("--max-hunks must be >= 0, --jobs >= 1 and --timeout > 0")
    for sig in SIGNALS:
        signal.signal(sig, _interrupt)
    try:
        return run(args)
    except KeyboardInterrupt:  # before or after the worktrees exist: nothing to remove
        _say("hunk-revert: interrupted")
        return 130


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
