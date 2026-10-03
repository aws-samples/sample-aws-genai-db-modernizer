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

The pid file and server logs live in `.local-ui/` at the repo root
(gitignored), not under the artifact root: the local API lists every
directory under the artifact root as a database, and the headless CI
permission allowlist may write anything under `artifacts/`, so neither should
see this script's bookkeeping. `--stop` only signals a recorded pid whose
command line still looks like the server it started (`uvicorn` / `serve`), so
a stale pid file can never take down an unrelated process that reused the pid.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import socket
import subprocess  # nosec B404 — intentional subprocess use to run uvicorn/npm/serve
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

API_PORT = 8000
UI_PORT = 3000
API_URL = f"http://localhost:{API_PORT}"
UI_URL = f"http://localhost:{UI_PORT}"
# Health probes go to the loopback address both servers bind explicitly, so
# they never depend on how "localhost" resolves (::1 first on macOS).
API_PROBE_URL = f"http://127.0.0.1:{API_PORT}"
UI_PROBE_URL = f"http://127.0.0.1:{UI_PORT}"
UI_LISTEN = f"tcp://127.0.0.1:{UI_PORT}"
UI_DIR = REPO_ROOT / "src" / "ui"
STATE_DIR = REPO_ROOT / ".local-ui"
POLL_INTERVAL_SECONDS = 1.0
LOOPBACK_HOSTS: tuple[tuple[int, str], ...] = (
    (socket.AF_INET, "127.0.0.1"),
    (socket.AF_INET6, "::1"),
)
# Substring each recorded process's command line must contain for --stop to
# signal it (see the module docstring).
PROCESS_MARKERS = {"api": "uvicorn", "serve": "serve"}


class NpmAuthError(Exception):
    """npm registry returned E401 (expired/invalid token)."""


class BuildError(Exception):
    """A build subprocess (npm ci / npm run build) failed for a reason other than auth."""


def local_ui_dir() -> Path:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    return STATE_DIR


def pids_path() -> Path:
    return local_ui_dir() / "pids.json"


def write_pids(pids: dict[str, int]) -> None:
    pids_path().write_text(json.dumps(pids))


def read_pids() -> dict[str, int]:
    path = pids_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, int)}


def clear_pids() -> None:
    path = pids_path()
    if path.exists():
        path.unlink()


def check_port_available(port: int) -> bool:
    """True if nothing accepts connections on `port` on either loopback
    address (IPv4 127.0.0.1 or IPv6 ::1). Probes by connecting rather than
    binding: a bind on one address family can succeed while another process
    listens on the other one."""
    for family, host in LOOPBACK_HOSTS:
        try:
            sock = socket.socket(family, socket.SOCK_STREAM)
        except OSError:
            continue  # address family unsupported on this host
        try:
            sock.settimeout(1.0)
            if sock.connect_ex((host, port)) == 0:
                return False
        except OSError:
            pass
        finally:
            sock.close()
    return True


def _run_npm(cmd: list[str], cwd: Path, env: dict[str, str]) -> None:
    try:
        proc = subprocess.run(  # nosec B603 # nosemgrep: dangerous-subprocess-use-audit, dangerous-subprocess-use
            cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=600
        )
    except subprocess.TimeoutExpired as exc:
        raise BuildError(f"`{' '.join(cmd)}` timed out after {exc.timeout:.0f}s") from exc
    except (FileNotFoundError, OSError) as exc:
        raise BuildError(f"`{' '.join(cmd)}` could not be started: {exc}") from exc
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
            [str(serve_bin), "-s", "build", "-l", UI_LISTEN, "--no-clipboard"],
            cwd=UI_DIR,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )


def _check_http_ok(url: str) -> bool:
    """True when a GET on a loopback ``url`` answers 2xx within 2 s.

    Uses ``http.client`` against an explicit loopback host so the probe can only
    ever reach the servers this script started (no arbitrary URL opening).
    """
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in ("127.0.0.1", "localhost", "::1"):
        return False
    conn = http.client.HTTPConnection(parts.hostname, parts.port or 80, timeout=2)
    try:
        conn.request("GET", parts.path or "/")
        status = conn.getresponse().status
        return 200 <= status < 300
    except (OSError, http.client.HTTPException):
        return False
    finally:
        conn.close()


def wait_for_ready(timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    for url in (f"{API_PROBE_URL}/health", UI_PROBE_URL, f"{UI_PROBE_URL}/analysis/monitor"):
        ok = False
        while time.monotonic() < deadline:
            if _check_http_ok(url):
                ok = True
                break
            time.sleep(POLL_INTERVAL_SECONDS)
        if not ok:
            return False
    return True


def process_command(pid: int) -> str | None:
    """The command line of ``pid`` per ``ps``, or None if it isn't running
    (or ``ps`` can't be run)."""
    try:
        proc = subprocess.run(  # nosec B603 B607 -- fixed argv, integer pid
            ["ps", "-o", "command=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


def kill_pids(pids: dict[str, int]) -> dict[str, int]:
    """Kill the process group of each pid whose command line still matches
    the server recorded under that name (see PROCESS_MARKERS). Both children
    were started with start_new_session=True, so each pid is also its process
    group id -- killing the group takes any children *they* spawned (e.g.
    npm -> node) down too."""
    killed: dict[str, int] = {}
    for name, pid in pids.items():
        marker = PROCESS_MARKERS.get(name)
        command = process_command(pid)
        if marker is None or command is None or marker not in command:
            continue
        try:
            os.killpg(pid, signal.SIGTERM)
            killed[name] = pid
        except (ProcessLookupError, PermissionError):
            continue
    return killed


def run_start(artifact_root: Path, rebuild: bool, timeout: float) -> tuple[dict[str, Any], int]:
    log_dir = local_ui_dir()

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

    write_pids(pids)

    if wait_for_ready(timeout):
        return {"status": "ready", "api": API_URL, "ui": UI_URL, "pids": pids}, 0

    kill_pids(pids)
    clear_pids()
    return {
        "status": "error",
        "reason": f"timeout waiting for local API/UI servers to become healthy after {timeout:.0f}s",
    }, 1


def run_stop() -> tuple[dict[str, Any], int]:
    pids = read_pids()
    killed = kill_pids(pids)
    clear_pids()
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
        help="Stop the servers started by a previous run (reads .local-ui/pids.json)",
    )
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    from scripts._sandbox import sandbox_violation

    violation = sandbox_violation(args)
    if violation:
        return {"status": "error", "reason": violation}, 1
    artifact_root = Path(args.artifact_root).resolve()
    if args.stop:
        return run_stop()
    return run_start(artifact_root, rebuild=args.rebuild, timeout=args.timeout)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    try:
        result, code = run(args)
    except (OSError, subprocess.TimeoutExpired) as exc:
        # Callers parse stdout as JSON; never let a stray OS error print a traceback instead.
        result, code = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}, 1
    print(json.dumps(result))
    sys.exit(code)


if __name__ == "__main__":
    main()
