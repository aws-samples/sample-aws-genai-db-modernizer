#!/usr/bin/env python3
"""Stop hook (interactive sessions): remind which real-model runs the diff needs (#731).

Runs ``ci/needs_llm_run.py`` over the files this branch changed since its
merge-base with ``origin/main`` (committed, uncommitted and untracked; files
that only main changed do not count) and, when ``e2e-llm`` or ``bedrock-smoke``
is required, shows its verdict to the user as a ``systemMessage``. Shown once
per session, and again only when the verdict changes. A reminder only: it never
blocks (always exits 0), stays silent when nothing is required or anything goes
wrong, and does nothing in headless runs (``MODERNIZER_CI_SANDBOX=1``) or when
the Stop event is itself a continuation (``stop_hook_active``).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess  # nosec B404 -- fixed argv: git and this repo's own ci/needs_llm_run.py
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hook_common  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
BASE = "origin/main"
STATE_DIR = os.path.join(tempfile.gettempdir(), "modernizer-llm-run-reminder")


def _git(args: list[str], cwd: str) -> list[str] | None:
    try:
        res = subprocess.run(  # nosec B603 B607 -- fixed argv
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    return [line for line in res.stdout.splitlines() if line]


def changed_paths(cwd: str = REPO_ROOT) -> list[str]:
    merge_base = _git(["merge-base", BASE, "HEAD"], cwd)
    if not merge_base:
        return []
    tracked = _git(["diff", "--name-only", merge_base[0]], cwd)
    untracked = _git(["ls-files", "--others", "--exclude-standard"], cwd)
    if tracked is None or untracked is None:
        return []
    return sorted(set(tracked + untracked))


def reminder(paths: list[str], cwd: str = REPO_ROOT) -> str | None:
    """needs_llm_run's verdict when a real-model run is required, else None."""
    if not paths:
        return None
    try:
        res = subprocess.run(  # nosec B603 -- fixed argv
            [sys.executable, "-I", os.path.join(cwd, "ci", "needs_llm_run.py"), *paths],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0 or ": required" not in res.stdout:
        return None
    return "This diff touches model-facing paths (ci/needs_llm_run.sh):\n" + res.stdout.strip()


def already_shown(session_id: str, message: str, state_dir: str | None = None) -> bool:
    """True when this session was already shown exactly this verdict; records it otherwise."""
    state_dir = state_dir or STATE_DIR
    name = re.sub(r"[^A-Za-z0-9_-]", "_", session_id)[:80]
    digest = hashlib.sha256(message.encode()).hexdigest()
    path = os.path.join(state_dir, name)
    try:
        with open(path, encoding="utf-8") as handle:
            if handle.read().strip() == digest:
                return True
    except OSError:
        pass
    try:
        os.makedirs(state_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(digest)
    except OSError:
        pass
    return False


def main() -> int:
    event = hook_common.read_event()
    if os.environ.get("MODERNIZER_CI_SANDBOX") == "1" or event.get("stop_hook_active"):
        return 0
    message = reminder(changed_paths())
    session_id = event.get("session_id")
    if (
        message
        and isinstance(session_id, str)
        and session_id
        and already_shown(session_id, message)
    ):
        return 0
    if message:
        print(json.dumps({"systemMessage": message}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 -- a reminder must never break a session
        sys.exit(0)
