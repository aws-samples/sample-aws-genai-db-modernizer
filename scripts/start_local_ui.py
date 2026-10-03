#!/usr/bin/env python3
"""Start (or stop) the local API + UI servers for a `/modernize --mode ui|both` run.

Headless `claude -p` runs execute Bash commands through a fixed permission
allowlist that matches each *subcommand* of a compound shell line. The old
UI-start block in `.claude/commands/modernize.md` was a multi-command shell
pipeline (`[ -x ... ] || npm ci`, `npm run build`, backgrounded `&`, then
curl polling) with pieces no allowlist entry covered. This script replaces
that whole pipeline with a single allowlistable command that does the same
work in-process and reports a single JSON result.

Usage:
    uv run python scripts/start_local_ui.py [--artifact-root ./artifacts] [--rebuild] [--timeout 180]
    uv run python scripts/start_local_ui.py --stop

Outputs JSON to stdout (the only output the caller parses):
    {"status": "ready", "api": "http://localhost:8000", "ui": "http://localhost:3000",
     "pids": {"api": 123, "serve": 456}}
    {"status": "error", "reason": "..."}
    {"status": "stopped", "pids": {"api": 123, "serve": 456}}

Exit codes: 0 ready/stopped, 1 generic error (timeout, port in use, build
failure), 2 npm registry auth failure (E401).
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess  # nosec B404 — intentional subprocess use to run uvicorn/npm/serve
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

API_PORT = 8000
UI_PORT = 3000
API_URL = f"http://localhost:{API_PORT}"
UI_URL = f"http://localhost:{UI_PORT}"
UI_DIR = REPO_ROOT / "src" / "ui"
LOCAL_UI_DIRNAME = ".local-ui"
POLL_INTERVAL_SECONDS = 1.0


class NpmAuthError(Exception):
    """npm registry returned E401 (expired/invalid token)."""


class BuildError(Exception):
    """A build subprocess (npm ci / npm run build) failed for a reason other than auth."""


def local_ui_dir(artifact_root: Path) -> Path:
    directory = artifact_root / LOCAL_UI_DIRNAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def pids_path(artifact_root: Path) -> Path:
    return local_ui_dir(artifact_root) / "pids.json"


def write_pids(artifact_root: Path, pids: dict[str, int]) -> None:
    pids_path(artifact_root).write_text(json.dumps(pids))


def read_pids(artifact_root: Path) -> dict[str, int]:
    path = pids_path(artifact_root)
    if not path.exists():
        return {}
    try:
        data: dict[str, int] = json.loads(path.read_text())
    except json.JSONDecodeError:
        return {}
    return data


def clear_pids(artifact_root: Path) -> None:
    path = pids_path(artifact_root)
    if path.exists():
        path.unlink()


def check_port_available(port: int) -> bool:
    """True if nothing else is listening on `port` (verified by binding it ourselves)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("localhost", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _run_npm(cmd: list[str], cwd: Path, env: dict[str, str]) -> None:
    proc = subprocess.run(  # nosec B603 # nosemgrep: dangerous-subprocess-use-audit, dangerous-subprocess-use
        cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=600
    )
    combined = (proc.stdout or "") + (proc.stderr or "")
    if "E401" in combined:
        raise NpmAuthError(combined)
    if proc.returncode != 0:
        raise BuildError(f"`{' '.join(cmd)}` failed (exit {proc.returncode}): {combined[-800:]}")


def build_ui(serve_bin: Path) -> None:
    """Install UI deps (only if `serve` is missing) and build the production bundle."""
    if not serve_bin.exists():
        _run_npm(["npm", "ci"], cwd=UI_DIR, env=os.environ.copy())

    env = os.environ.copy()
    env["REACT_APP_API_URL"] = f"{API_URL}/api/v1/"
    env["CI"] = "false"
    _run_npm(["npm", "run", "build"], cwd=UI_DIR, env=env)


def start_api(artifact_root: Path, log_dir: Path) -> subprocess.Popen:
    env = os.environ.copy()
    env["ARTIFACT_DIR"] = str(artifact_root)
    log_path = log_dir / "api.log"
    with open(log_path, "ab") as log_file:
        return subprocess.Popen(  # nosec B603 # nosemgrep: dangerous-subprocess-use-audit, dangerous-subprocess-use
            [
                sys.executable,
                "-m",
                "uvicorn",
                "src.api.main:app",
                "--host",
                "127.0.0.1",
                "--port",
                str(API_PORT),
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )


def start_serve(serve_bin: Path, log_dir: Path) -> subprocess.Popen:
    log_path = log_dir / "serve.log"
    with open(log_path, "ab") as log_file:
        return subprocess.Popen(  # nosec B603 # nosemgrep: dangerous-subprocess-use-audit, dangerous-subprocess-use
            [str(serve_bin), "-s", "build", "-l", str(UI_PORT), "--no-clipboard"],
            cwd=UI_DIR,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )


def _check_http_ok(url: str) -> bool:
    try:
        with urllib.request.urlopen(
            url, timeout=2
        ) as resp:  # nosec B310 — fixed localhost URLs only
            status: int = resp.status
            return 200 <= status < 300
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return False


def wait_for_ready(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    for url in (f"{API_URL}/health", UI_URL, f"{UI_URL}/analysis/monitor"):
        ok = False
        while time.monotonic() < deadline:
            if _check_http_ok(url):
                ok = True
                break
            time.sleep(POLL_INTERVAL_SECONDS)
        if not ok:
            return False
    return True


def kill_pids(pids: dict[str, int]) -> dict[str, int]:
    """Kill the process group of each pid. Both children were started with
    start_new_session=True, so each pid is also its process group id --
    killing the group takes any children *they* spawned (e.g. npm -> node)
    down too."""
    killed: dict[str, int] = {}
    for name, pid in pids.items():
        try:
            os.killpg(pid, signal.SIGTERM)
            killed[name] = pid
        except (ProcessLookupError, PermissionError):
            continue
    return killed


def run_start(artifact_root: Path, rebuild: bool, timeout: float) -> tuple[dict[str, Any], int]:
    log_dir = local_ui_dir(artifact_root)

    for port in (API_PORT, UI_PORT):
        if not check_port_available(port):
            return {
                "status": "error",
                "reason": f"port {port} is already in use by something this script didn't start",
            }, 1

    build_index = UI_DIR / "build" / "index.html"
    serve_bin = UI_DIR / "node_modules" / ".bin" / "serve"

    if rebuild or not build_index.exists():
        try:
            build_ui(serve_bin)
        except NpmAuthError:
            return {
                "status": "error",
                "reason": "npm registry auth (E401) — refresh the npm token",
            }, 2
        except BuildError as exc:
            return {"status": "error", "reason": str(exc)}, 1

    pids: dict[str, int] = {}
    try:
        api_proc = start_api(artifact_root, log_dir)
        pids["api"] = api_proc.pid
        serve_proc = start_serve(serve_bin, log_dir)
        pids["serve"] = serve_proc.pid
    except OSError as exc:
        kill_pids(pids)
        return {"status": "error", "reason": f"failed to start local servers: {exc}"}, 1

    write_pids(artifact_root, pids)

    if wait_for_ready(timeout):
        return {"status": "ready", "api": API_URL, "ui": UI_URL, "pids": pids}, 0

    kill_pids(pids)
    clear_pids(artifact_root)
    return {
        "status": "error",
        "reason": f"timeout waiting for local API/UI servers to become healthy after {timeout:.0f}s",
    }, 1


def run_stop(artifact_root: Path) -> tuple[dict[str, Any], int]:
    pids = read_pids(artifact_root)
    killed = kill_pids(pids)
    clear_pids(artifact_root)
    return {"status": "stopped", "pids": killed}, 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Start (or stop) the local API + UI servers for /modernize ui/both mode."
    )
    parser.add_argument(
        "--artifact-root",
        default="./artifacts",
        help="Root directory for local artifacts (default: ./artifacts)",
    )
    parser.add_argument(
        "--rebuild",
        action="store_true",
        help="Rebuild the UI bundle even if src/ui/build/index.html already exists",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=180,
        help="Seconds to wait for both servers to become healthy (default: 180)",
    )
    parser.add_argument(
        "--stop",
        action="store_true",
        help="Stop the servers started by a previous run (reads artifacts/.local-ui/pids.json)",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    artifact_root = Path(args.artifact_root).resolve()
    if args.stop:
        return run_stop(artifact_root)
    return run_start(artifact_root, rebuild=args.rebuild, timeout=args.timeout)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    result, code = run(args)
    print(json.dumps(result))
    sys.exit(code)


if __name__ == "__main__":
    main()
