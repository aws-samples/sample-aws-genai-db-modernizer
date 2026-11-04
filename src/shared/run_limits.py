# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Opt-in per-run ceilings on tokens, model calls and wall-clock time (#145).

Each ceiling is its own environment variable, and every one is off unless set (no
default cap, as decided on #384):

* ``MODERNIZER_MAX_RUN_TOKENS``: the model tokens one run may use, counting every
  counter a call reports (input, output, cache read and cache write);
* ``MODERNIZER_MAX_RUN_MODEL_CALLS``: the model calls one run may make;
* ``MODERNIZER_MAX_RUN_SECONDS``: the run's wall-clock seconds.

Only ``assess()`` and ``scripts/run_assessment.py`` read them, when a run starts, so
a bad value (not a number, zero or negative) fails before any phase runs
(:class:`RunLimitConfigError`, a ``ValueError``); the other ``scripts/run_*.py``
entry points do not. The figures are the ones the run's usage ledger keeps
(``src/shared/model_usage.py``), which is process-wide: one run at a time per process
(with a ceiling set, ``assess()`` refuses a second concurrent call), and only model
calls made in the process count, so with ``--llm-mode external`` (Claude Code answers outside it) the
token and call ceilings have nothing to count. The seconds ceiling is the run's wall
clock from when the run started (``wall_clock_seconds``; for ``run_assessment.py`` its
own set-up before the first phase is included), so it can exceed the sum of the
phases' seconds.

The ceilings are checked when each phase ends, the clean point where a run can stop:
a phase that has started finishes, so a run can go over by what one phase spends.
A run over a ceiling, also after its last phase, fails with
:class:`RunLimitExceeded`, whose message names the ceiling, its limit and the
observed figure; nothing is truncated.
"""

from __future__ import annotations

import math
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

ENV_MAX_RUN_TOKENS = "MODERNIZER_MAX_RUN_TOKENS"
ENV_MAX_RUN_MODEL_CALLS = "MODERNIZER_MAX_RUN_MODEL_CALLS"
ENV_MAX_RUN_SECONDS = "MODERNIZER_MAX_RUN_SECONDS"

#: Ceiling name -> (environment variable, the figure's unit in messages).
CEILINGS: dict[str, tuple[str, str]] = {
    "tokens": (ENV_MAX_RUN_TOKENS, "tokens"),
    "model_calls": (ENV_MAX_RUN_MODEL_CALLS, "model calls"),
    "seconds": (ENV_MAX_RUN_SECONDS, "seconds"),
}


class RunLimitConfigError(ValueError):
    """A per-run ceiling variable is set to a value that is not a positive number."""


class RunLimitExceeded(RuntimeError):
    """A run went over a per-run ceiling in ``phase``; it stops when that phase ends."""

    def __init__(self, ceiling: str, limit: float, observed: float, phase: str) -> None:
        self.ceiling = ceiling
        self.env_var, unit = CEILINGS[ceiling]
        self.limit = limit
        self.observed = observed
        self.phase = phase
        if ceiling == "seconds":
            # The run's wall clock (``wall_clock_seconds``), from when the run started
            # (script set-up included), not the phase's own seconds.
            cap = f"{limit:,g}"
            used = f"the run's wall-clock time since it started was {observed:,.1f} seconds"
        else:
            cap = f"{int(limit):,}"
            used = f"the run used {int(observed):,} {unit}"
        super().__init__(
            f"per-run ceiling exceeded in the {phase} phase: {used}, over the limit of "
            f"{cap} ({self.env_var}={_shown_limit(limit)}). The "
            f"phase's output was kept and the run is marked failed; no later phase runs. "
            f"Raise or unset {self.env_var} to let it go on"
        )

    def as_record(self) -> dict[str, Any]:
        """The ``ceiling_exceeded`` entry of the run's usage record."""
        return {
            "ceiling": self.ceiling,
            "env_var": self.env_var,
            "limit": self.limit,
            "observed": self.observed,
            "phase": self.phase,
        }


def _shown_limit(limit: float) -> str:
    return str(int(limit)) if float(limit).is_integer() else str(limit)


def _whole(name: str, raw: str) -> int:
    hint = f"{name}={raw!r} is not a valid ceiling: set a positive whole number, or unset it"
    try:
        value = int(raw.strip())
    except ValueError:
        try:
            float(raw)
        except ValueError:
            raise RunLimitConfigError(f"{hint} for no ceiling") from None
        raise RunLimitConfigError(f"{hint} for no ceiling (a whole number is needed)") from None
    if value <= 0:
        raise RunLimitConfigError(f"{hint} for no ceiling")
    return value


def _seconds(name: str, raw: str) -> float:
    hint = (
        f"{name}={raw!r} is not a valid ceiling: set a positive number of seconds, "
        "or unset it for no ceiling"
    )
    try:
        value = float(raw.strip())
    except ValueError:
        raise RunLimitConfigError(hint) from None
    if not math.isfinite(value) or value <= 0:
        raise RunLimitConfigError(hint)
    return value


@dataclass(frozen=True)
class RunLimits:
    """The ceilings set for one run; ``None`` is no ceiling."""

    tokens: int | None = None
    model_calls: int | None = None
    seconds: float | None = None

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> RunLimits:
        """Read the three variables; unset or empty is no ceiling, a bad value raises
        :class:`RunLimitConfigError`."""
        env = os.environ if environ is None else environ

        def raw(name: str) -> str | None:
            value = env.get(name)
            return value if value and value.strip() else None

        tokens, calls, seconds = (
            raw(ENV_MAX_RUN_TOKENS),
            raw(ENV_MAX_RUN_MODEL_CALLS),
            raw(ENV_MAX_RUN_SECONDS),
        )
        return cls(
            tokens=None if tokens is None else _whole(ENV_MAX_RUN_TOKENS, tokens),
            model_calls=None if calls is None else _whole(ENV_MAX_RUN_MODEL_CALLS, calls),
            seconds=None if seconds is None else _seconds(ENV_MAX_RUN_SECONDS, seconds),
        )

    @property
    def any_set(self) -> bool:
        return any(v is not None for v in (self.tokens, self.model_calls, self.seconds))

    def check(self, ledger: Any, phase: str) -> None:
        """Raise :class:`RunLimitExceeded` when the run in ``ledger`` (a
        ``model_usage.UsageLedger``) is over a ceiling at the end of ``phase``; being
        exactly at a limit is within it. The exceeded ceiling is noted in the ledger,
        so the run's usage record says why it stopped."""
        if not self.any_set:
            return
        from src.shared.model_usage import FIELDS

        totals = ledger.snapshot()["totals"]
        observed: dict[str, float] = {
            "tokens": sum(int(totals.get(field) or 0) for field in FIELDS),
            "model_calls": int(totals.get("calls") or 0),
            "seconds": round(ledger.elapsed(), 3),
        }
        limits = {"tokens": self.tokens, "model_calls": self.model_calls, "seconds": self.seconds}
        for ceiling, limit in limits.items():
            if limit is not None and observed[ceiling] > limit:
                exceeded = RunLimitExceeded(ceiling, limit, observed[ceiling], phase)
                ledger.note_ceiling_exceeded(exceeded.as_record())
                raise exceeded
