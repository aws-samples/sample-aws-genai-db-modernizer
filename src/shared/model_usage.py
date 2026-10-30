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
``assess()`` and ``scripts/run_assessment.py`` clear when a run starts. The job's
record keeps every run (``runs``) and their sum (:func:`write_usage`).
"""

from __future__ import annotations

import logging
import threading
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


def _zero() -> dict[str, int]:
    return {"calls": 0, **dict.fromkeys(FIELDS, 0)}


class UsageLedger:
    """Thread-safe token totals keyed by phase, then model id."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._phase = UNKNOWN_PHASE
        self._totals: dict[str, dict[str, dict[str, int]]] = {}

    def reset(self) -> None:
        with self._lock:
            self._phase = UNKNOWN_PHASE
            self._totals = {}

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
            by_phase = {
                phase: {model: dict(row) for model, row in sorted(models.items())}
                for phase, models in self._totals.items()
            }
        by_model: dict[str, dict[str, int]] = {}
        total = _zero()
        for models in by_phase.values():
            for model, row in models.items():
                into = by_model.setdefault(model, _zero())
                for key, value in row.items():
                    into[key] += value
                    total[key] += value
        return {"totals": total, "by_model": by_model, "by_phase": by_phase}


LEDGER = UsageLedger()


def record_usage(model_id: str, usage: Mapping[str, Any] | None) -> None:
    """Report one call's Converse-style ``usage`` (a missing one counts nothing)."""
    if usage:
        LEDGER.record(model_id, usage)


def record_invoke_model_usage(model_id: str, body: Mapping[str, Any]) -> None:
    """Report the ``usage`` of an ``invoke_model`` response body (Anthropic names)."""
    usage = body.get("usage") or {}
    record_usage(model_id, {_INVOKE_NAMES[k]: v for k, v in usage.items() if k in _INVOKE_NAMES})


@contextmanager
def phase(name: str) -> Iterator[None]:
    """Calls made inside the block are counted under ``name``."""
    previous = LEDGER.set_phase(name)
    try:
        yield
    finally:
        LEDGER.set_phase(previous)


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
    return {"totals": total, "by_model": dict(sorted(by_model.items())), "by_phase": by_phase}


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


def write_usage(store: Any, database_name: str, job_id: str) -> dict[str, Any] | None:
    """Add this run's ledger to the job's usage record; None (nothing written) with no calls.

    Every run of a job that called a model (an ``assess()`` call, a ``run_assessment.py``
    run, a later ``--job-id`` run) is kept under ``runs``, numbered in order, and the
    top-level ``totals``, ``by_model`` and ``by_phase`` are their sum, which
    ``scripts/usage_cost.py`` prices (#1675). A later run never overwrites an earlier one.
    """
    snapshot = LEDGER.snapshot()
    if not snapshot["totals"]["calls"]:
        return None
    key = f"{database_name}/{job_id}/{USAGE_KEY}"
    runs = _earlier_runs(store, key)
    runs.append({"run": len(runs) + 1, **snapshot})
    record = {**_combined(runs), "runs": runs}
    store.write_json(key, record)
    return record
