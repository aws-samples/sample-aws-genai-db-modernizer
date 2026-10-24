"""Token usage of every model call in a run, by phase and model id (#1494).

Bedrock returns the usage of each call (input, output, cache read and cache write
tokens). ``src/shared/role_bedrock_model.py`` (the one model wrapper) and the two direct boto3 calls report it here, :func:`phase` names the
pipeline phase a call belongs to, and ``assess()`` writes the totals as
``<db>/<job>/_logs/model_usage.json`` (an internal file, ADR-008 Decision 1:
callers may not rely on its shape). ``scripts/usage_cost.py`` turns that file into
dollars with ``src/catalog/model_prices.toml``.

One run at a time per process: calls are added to the process-wide ledger, which
``assess()`` clears when a run starts.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

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


def write_usage(store: Any, database_name: str, job_id: str) -> dict[str, Any] | None:
    """Write the ledger as the job's usage record; None (nothing written) with no calls."""
    record = LEDGER.snapshot()
    if not record["totals"]["calls"]:
        return None
    store.write_json(f"{database_name}/{job_id}/{USAGE_KEY}", record)
    return record
