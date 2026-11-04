# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Token usage of every model call in a run, by phase and model id (#1494).

Bedrock returns the usage of each call (input, output, cache read and cache write
tokens). ``src/shared/role_bedrock_model.py`` (the one model wrapper) and the two direct boto3 calls report it here, :func:`phase` names the
pipeline phase a call belongs to, and ``assess()`` and ``scripts/run_assessment.py``
(#1675) write the totals as ``<db>/<job>/_logs/model_usage.json`` (an internal file,
ADR-008 Decision 1: callers may not rely on its shape). ``scripts/usage_cost.py`` turns that file into
dollars with ``src/catalog/model_prices.toml``.

One run at a time per process: calls are added to the process-wide ledger, which
``assess()`` and ``scripts/run_assessment.py`` clear when a run starts. Concurrent
``assess()`` calls in one process share it (as before #145), unless a per-run ceiling
is set: then ``assess()`` holds :func:`exclusive_run`, so a concurrent second call
fails instead of mixing counts. The job's
record keeps every run (``runs``) and their sum (:func:`write_usage`).

The ledger also times the run (#145): each phase's wall-clock seconds and the engines
it processed (``phases``, in run order) and the run's own wall-clock time
(``wall_clock_seconds``), so a run is measurable per phase and not only per token.
``src/shared/run_limits.py`` checks the opt-in per-run ceilings against it.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)

USAGE_KEY = "_logs/model_usage.json"
#: Counters kept per phase and model id (Bedrock Converse names).
FIELDS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheWriteInputTokens")
#: The same counters under the Anthropic ``invoke_model`` body's names.
_INVOKE_NAMES = {
    "input_tokens": "inputTokens",
    "output_tokens": "outputTokens",
    "cache_read_input_tokens": "cacheReadInputTokens",
    "cache_creation_input_tokens": "cacheWriteInputTokens",
}
UNKNOWN_PHASE = "unassigned"
#: The run clock (seconds); a test replaces it to move time by hand.
_clock = time.monotonic


def _zero() -> dict[str, int]:
    return {"calls": 0, **dict.fromkeys(FIELDS, 0)}


def _timing() -> dict[str, Any]:
    return {"seconds": 0.0, "engines": []}


def _add_engines(into: list[str], engines: Any) -> None:
    for engine in engines or ():
        if isinstance(engine, str) and engine not in into:
            into.append(engine)


class UsageLedger:
    """Thread-safe token totals keyed by phase, then model id, and the run's timings."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._phase = UNKNOWN_PHASE
        self._totals: dict[str, dict[str, dict[str, int]]] = {}
        self._started = _clock()
        self._timings: dict[str, dict[str, Any]] = {}
        self._open: tuple[str, float] | None = None
        self._stopped: dict[str, Any] | None = None

    def reset(self) -> None:
        """Start a new run: no calls, no timings, the run clock from now."""
        with self._lock:
            self._phase = UNKNOWN_PHASE
            self._totals = {}
            self._started = _clock()
            self._timings = {}
            self._open = None
            self._stopped = None

    def _close_locked(self) -> None:
        if self._open is not None:
            name, start = self._open
            self._timings[name]["seconds"] += _clock() - start
            self._open = None

    def start_timing(self, name: str) -> None:
        """Time phase ``name`` from now; the phase timed so far (if any) ends here. A
        phase timed again adds to its seconds."""
        with self._lock:
            self._close_locked()
            self._timings.setdefault(name, _timing())
            self._open = (name, _clock())

    def stop_timing(self) -> None:
        """End the phase being timed (nothing when none is)."""
        with self._lock:
            self._close_locked()

    def record_engines(self, engines: Any, phase: str | None = None) -> None:
        """Add the engines ``phase`` processed (default: the phase being timed), in
        order, each once."""
        with self._lock:
            name = phase or (self._open[0] if self._open else self._phase)
            _add_engines(self._timings.setdefault(name, _timing())["engines"], engines)

    def elapsed(self) -> float:
        """The run's wall-clock seconds so far."""
        with self._lock:
            return _clock() - self._started

    def note_ceiling_exceeded(self, detail: Mapping[str, Any]) -> None:
        """Record which per-run ceiling stopped the run (``src/shared/run_limits.py``)."""
        with self._lock:
            self._stopped = dict(detail)

    def set_phase(self, name: str) -> str:
        with self._lock:
            previous, self._phase = self._phase, name
            return previous

    def record(self, model_id: str, usage: Mapping[str, Any]) -> None:
        """Add one call's usage (Converse field names)."""
        with self._lock:
            row = self._totals.setdefault(self._phase, {}).setdefault(model_id, _zero())
            row["calls"] += 1
            for field in FIELDS:
                row[field] += int(usage.get(field) or 0)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            now = _clock()
            by_phase = {
                phase: {model: dict(row) for model, row in sorted(models.items())}
                for phase, models in self._totals.items()
            }
            phases: dict[str, dict[str, Any]] = {}
            for name, row in self._timings.items():
                seconds = row["seconds"]
                if self._open is not None and self._open[0] == name:
                    seconds += now - self._open[1]  # still running: its time so far
                phases[name] = {"seconds": round(seconds, 3), "engines": list(row["engines"])}
            wall_clock = round(now - self._started, 3)
            stopped = dict(self._stopped) if self._stopped else None
        by_model: dict[str, dict[str, int]] = {}
        total = _zero()
        for models in by_phase.values():
            for model, row in models.items():
                into = by_model.setdefault(model, _zero())
                for key, value in row.items():
                    into[key] += value
                    total[key] += value
        snapshot: dict[str, Any] = {
            "totals": total,
            "by_model": by_model,
            "by_phase": by_phase,
            "wall_clock_seconds": wall_clock,
            "phases": phases,
        }
        if stopped:
            snapshot["ceiling_exceeded"] = stopped
        return snapshot


LEDGER = UsageLedger()


def record_usage(model_id: str, usage: Mapping[str, Any] | None) -> None:
    """Report one call's Converse-style ``usage`` (a missing one counts nothing)."""
    if usage:
        LEDGER.record(model_id, usage)


def record_invoke_model_usage(model_id: str, body: Mapping[str, Any]) -> None:
    """Report the ``usage`` of an ``invoke_model`` response body (Anthropic names)."""
    usage = body.get("usage") or {}
    record_usage(model_id, {_INVOKE_NAMES[k]: v for k, v in usage.items() if k in _INVOKE_NAMES})


def record_engines(engines: Any, phase: str | None = None) -> None:
    """Report the engines the phase being timed (or ``phase``) processed (#145)."""
    LEDGER.record_engines(engines, phase)


class ConcurrentRunError(RuntimeError):
    """A second run started in a process while one is in progress (#145)."""


_RUN_LOCK = threading.Lock()


def _reset_run_lock() -> None:
    """A forked child starts with no run in progress, even when the parent's lock was
    held (by another thread) at the fork."""
    global _RUN_LOCK
    _RUN_LOCK = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_reset_run_lock)


@contextmanager
def exclusive_run() -> Iterator[None]:
    """Hold the process's one ceilinged run: the per-run ceilings are checked against
    the process-wide ledger, so a second run started while one is in progress raises
    :class:`ConcurrentRunError` instead of mixing their counts. ``assess()`` takes it
    only when a ceiling is set; without one, concurrent runs share the counts, as
    before #145."""
    if not _RUN_LOCK.acquire(blocking=False):
        raise ConcurrentRunError(
            "another run is in progress in this process, and a per-run ceiling "
            "(MODERNIZER_MAX_RUN_*) is set: model usage and the ceilings are counted per "
            "process, so run one ceilinged assess() call at a time in a process (use "
            "separate processes to run several at once)"
        )
    try:
        yield
    finally:
        _RUN_LOCK.release()


@contextmanager
def phase(name: str) -> Iterator[None]:
    """Calls made inside the block are counted under ``name``."""
    previous = LEDGER.set_phase(name)
    try:
        yield
    finally:
        LEDGER.set_phase(previous)


def _combined_timings(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Each phase's seconds and engines, and the wall-clock seconds, over ``runs``. A
    run recorded before timings were kept (#145) has neither and adds nothing."""
    phases: dict[str, dict[str, Any]] = {}
    wall_clock = 0.0
    for run in runs:
        timed = run.get("phases")
        for name, row in (timed if isinstance(timed, dict) else {}).items():
            if not isinstance(row, dict):
                continue
            into = phases.setdefault(name, _timing())
            into["seconds"] = round(into["seconds"] + float(row.get("seconds") or 0), 3)
            _add_engines(into["engines"], row.get("engines"))
        wall_clock += float(run.get("wall_clock_seconds") or 0)
    return {"wall_clock_seconds": round(wall_clock, 3), "phases": phases}


def _combined(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals, by model and by phase, summed over ``runs`` (each a ledger snapshot)."""
    by_phase: dict[str, dict[str, dict[str, int]]] = {}
    for run in runs:
        for phase_name, models in (run.get("by_phase") or {}).items():
            for model, row in models.items():
                into = by_phase.setdefault(phase_name, {}).setdefault(model, _zero())
                for key, value in row.items():
                    into[key] = into.get(key, 0) + int(value or 0)
    by_model: dict[str, dict[str, int]] = {}
    total = _zero()
    for models in by_phase.values():
        for model, row in models.items():
            into = by_model.setdefault(model, _zero())
            for key, value in row.items():
                into[key] = into.get(key, 0) + value
                total[key] = total.get(key, 0) + value
    return {
        "totals": total,
        "by_model": dict(sorted(by_model.items())),
        "by_phase": by_phase,
        **_combined_timings(runs),
    }


def _earlier_runs(store: Any, key: str) -> list[dict[str, Any]]:
    """The runs a job's usage record already holds; a record written before runs were
    kept (#1675) is one run. An unreadable record is not added to (logged, not raised)."""
    try:
        if not store.exists(key):
            return []
        record = store.read_json(key)
    except Exception as exc:  # noqa: BLE001 - the new run is still recorded
        logger.warning(
            "model usage record %s unreadable (%s); starting it again", key, type(exc).__name__
        )
        return []
    if not isinstance(record, dict):
        return []
    runs = record.get("runs")
    if isinstance(runs, list):
        return [r for r in runs if isinstance(r, dict)]
    if "by_phase" in record:
        return [
            {"run": 1, **{k: record[k] for k in ("totals", "by_model", "by_phase") if k in record}}
        ]
    return []


def write_usage(
    store: Any, database_name: str, job_id: str, *, always: bool = False
) -> dict[str, Any] | None:
    """Add this run's ledger to the job's usage record; None (nothing written) for a run
    with no model call, unless ``always`` (``scripts/run_assessment.py``, whose every run
    is recorded with its timings and zero counts, #145).

    Every run of a job that called a model (an ``assess()`` call, a ``run_assessment.py``
    run, a later ``--job-id`` run) is kept under ``runs``, numbered in order, and the
    top-level ``totals``, ``by_model`` and ``by_phase`` are their sum, which
    ``scripts/usage_cost.py`` prices (#1675). A later run never overwrites an earlier one.
    Each run also carries its ``wall_clock_seconds``, its ``phases`` (seconds and engines
    per phase) and, when a per-run ceiling stopped it, ``ceiling_exceeded``; the top
    level sums the timings over the runs that have them (#145).
    """
    snapshot = LEDGER.snapshot()
    if not snapshot["totals"]["calls"] and not always:
        return None
    key = f"{database_name}/{job_id}/{USAGE_KEY}"
    runs = _earlier_runs(store, key)
    runs.append({"run": len(runs) + 1, **snapshot})
    record = {**_combined(runs), "runs": runs}
    store.write_json(key, record)
    return record
