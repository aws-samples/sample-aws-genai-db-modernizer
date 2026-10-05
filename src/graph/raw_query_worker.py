"""Run one caller-supplied read query in a separate, resource-capped process.

Some statements build large values before the engine's query timeout or
buffer pool apply (for example a chain of clauses that keeps doubling a string
or list). Running each raw query in a short-lived child process bounds them:

- the child opens the database read-only with a small buffer pool and a
  limited number of threads, and sets the engine query timeout;
- a watchdog thread in the child exits it as soon as its peak resident memory
  passes ``MEMORY_LIMIT_BYTES``;
- the parent kills the child if it has not answered within the timeout plus a
  short grace period;
- the serialised result is capped at ``MAX_RESULT_BYTES``.

The parent side is ``run_raw_query``. This file is also the child's entry point
(``python raw_query_worker.py``), so it only imports the standard library and
``ladybug`` at module level.
"""

from __future__ import annotations

import base64
import datetime as _dt
import decimal
import json
import os
import subprocess  # nosec B404 - runs this module's own entry point, no shell
import sys
import threading
import time
import uuid
from typing import Any

MEMORY_LIMIT_BYTES = 768 * 1024 * 1024
BUFFER_POOL_BYTES = 256 * 1024 * 1024
MAX_THREADS = 2
MAX_RESULT_BYTES = 16 * 1024 * 1024
GRACE_SECONDS = 3.0
_MEMORY_EXIT_CODE = 3
_ORPHAN_EXIT_CODE = 4
_WATCHDOG_INTERVAL_S = 0.01


class RawQueryError(Exception):
    """A raw query failed. ``kind`` is one of: timeout, memory, too_large, query."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


# --- parent side --------------------------------------------------------------


def run_raw_query(
    db_path: str,
    cypher: str,
    params: dict | None,
    max_rows: int,
    timeout_ms: int,
) -> tuple[list[dict], bool]:
    """Run ``cypher`` against the database at ``db_path`` in a child process.

    Returns ``(rows, truncated)``; raises RawQueryError on failure.
    """
    request = json.dumps(
        {
            "db_path": db_path,
            "cypher": cypher,
            "params": params,
            "max_rows": max_rows,
            "timeout_ms": timeout_ms,
            "memory_limit_bytes": MEMORY_LIMIT_BYTES,
            "buffer_pool_bytes": BUFFER_POOL_BYTES,
            "max_threads": MAX_THREADS,
            "max_result_bytes": MAX_RESULT_BYTES,
        }
    ).encode()
    proc = subprocess.Popen(  # nosec B603 - fixed argv: this interpreter + this file
        [sys.executable, "-I", os.path.abspath(__file__)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    )
    try:
        out, _ = proc.communicate(request, timeout=timeout_ms / 1000 + GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        raise RawQueryError("timeout", "The query did not finish within the time limit.") from None

    if proc.returncode == _MEMORY_EXIT_CODE:
        raise RawQueryError("memory", "The query needed more memory than allowed.")
    try:
        reply = json.loads(out)
    except ValueError:
        raise RawQueryError("query", "The query failed.") from None
    if not reply.get("ok"):
        raise RawQueryError(reply.get("kind", "query"), reply.get("message", "The query failed."))
    return reply["rows"], bool(reply["truncated"])


# --- child side ---------------------------------------------------------------


_STATM = "/proc/self/statm"


def _rss_bytes() -> int:
    """Resident memory of this process.

    Linux carries the parent's peak RSS into a forked and exec'd child, so
    ``ru_maxrss`` there can start above the limit; read the current RSS from
    /proc instead. macOS has no /proc; its ``ru_maxrss`` (bytes) starts fresh.
    """
    try:
        with open(_STATM) as f:
            resident_pages = int(f.read().split()[1])
        return resident_pages * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError):
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak if sys.platform == "darwin" else peak * 1024)


def _watch_memory(limit: int, parent_pid: int) -> None:
    while True:
        if _rss_bytes() > limit:
            os._exit(_MEMORY_EXIT_CODE)
        # The server that started us is gone: nobody will read the result.
        if os.getppid() != parent_pid:
            os._exit(_ORPHAN_EXIT_CODE)
        time.sleep(_WATCHDOG_INTERVAL_S)


def _to_json(value: Any) -> Any:
    if isinstance(value, _dt.datetime | _dt.date | _dt.time):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        return value.total_seconds()
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, bytes | bytearray):
        return base64.b64encode(bytes(value)).decode()
    return str(value)


def _reply(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, default=_to_json))
    sys.stdout.flush()


def _child_main() -> None:
    request = json.loads(sys.stdin.read())
    threading.Thread(
        target=_watch_memory,
        args=(request["memory_limit_bytes"], os.getppid()),
        daemon=True,
    ).start()

    import ladybug as lb

    try:
        db = lb.Database(
            request["db_path"],
            read_only=True,
            buffer_pool_size=request["buffer_pool_bytes"],
            max_num_threads=request["max_threads"],
        )
        conn = lb.Connection(db)
        conn.set_query_timeout(request["timeout_ms"])
        params = request["params"]
        result = (
            conn.execute(request["cypher"], parameters=params)
            if params is not None
            else conn.execute(request["cypher"])
        )
        if isinstance(result, list):
            result = result[0]
        result = result.rows_as_dict()
        rows = result.get_n(request["max_rows"])
        truncated = result.has_next()
        body = json.dumps({"ok": True, "rows": rows, "truncated": truncated}, default=_to_json)
    except Exception as exc:  # noqa: BLE001 - reported to the parent
        message = str(exc)
        kind = "timeout" if "Interrupted" in message else "query"
        if kind == "timeout":
            message = "The query did not finish within the time limit."
        _reply({"ok": False, "kind": kind, "message": message})
        sys.exit(0)

    if len(body) > request["max_result_bytes"]:
        _reply({"ok": False, "kind": "too_large", "message": "The query result is too large."})
    else:
        sys.stdout.write(body)
        sys.stdout.flush()


if __name__ == "__main__":
    _child_main()
    # Skip interpreter teardown (and the engine's) once the reply is written.
    os._exit(0)
