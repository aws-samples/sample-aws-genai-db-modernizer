#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The Bedrock smoke's pass check (#2085).

Reads the status lines ``scripts/run_assessment.py --all -y --llm-mode bedrock`` prints
(one JSON object per line; any other line is ignored) and passes when every phase ended
well, the phases a full run needs are there, and the report phase lists no errors.

A phase ends well on ``complete`` or ``unchanged``. ``wave_redesign`` also ends well on
each status its loop ends on when it ran as designed (``LOOP_ENDINGS`` in
``src/agents/schema_design/wave_redesign.py``: rounds can run out with a wave still held,
the outcome the report states), and on ``skipped`` with a reason. Anything else, such as
``failed`` or ``error``, fails the check, as does a missing phase or a report error.

Usage: ``uv run python ci/bedrock_smoke.py smoke/status.jsonl``. Exit 0 on pass, 1 on
failure (the reason on stderr), 2 when the file cannot be read.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterable, Mapping
from typing import Any

from src.agents.schema_design.wave_redesign import LOOP_ENDINGS, SKIPPED

#: The statuses every phase may end on.
PASSING = frozenset({"complete", "unchanged"})
#: The phases a full run must print a status line for.
REQUIRED_PHASES = ("schema_design", "synthesis", "report")
#: Per phase, the statuses it may end on besides ``PASSING``.
PHASE_ENDINGS: Mapping[str, frozenset[str]] = {"wave_redesign": LOOP_ENDINGS}
#: Per phase, the statuses it may end on only when the line gives a reason.
PHASE_SKIPS: Mapping[str, frozenset[str]] = {"wave_redesign": frozenset({SKIPPED})}


def status_lines(lines: Iterable[str]) -> dict[str, dict[str, Any]]:
    """Each phase's last status line, from the run's stdout lines."""
    phases: dict[str, dict[str, Any]] = {}
    for line in lines:
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("phase"), str):
            phases[data["phase"]] = data
    return phases


def ended_well(phase: str, line: Mapping[str, Any]) -> bool:
    """Whether ``phase``'s status line is an outcome, not a failure."""
    status = line.get("status")
    if status in PASSING or status in PHASE_ENDINGS.get(phase, frozenset()):
        return True
    reason = line.get("reason")
    return status in PHASE_SKIPS.get(phase, frozenset()) and bool(
        isinstance(reason, str) and reason.strip()
    )


def check(phases: Mapping[str, Mapping[str, Any]]) -> str | None:
    """None when the run passes, else the failure line."""
    bad = [p for p, line in phases.items() if not ended_well(p, line)]
    need = [p for p in REQUIRED_PHASES if p not in phases]
    errors = (phases.get("report") or {}).get("errors")
    if bad or need or errors:
        return (
            f"bedrock-smoke failed: phases not complete {bad}, missing {need}, "
            f"report errors {errors}"
        )
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: ci/bedrock_smoke.py <status.jsonl>", file=sys.stderr)
        return 2
    try:
        with open(argv[0], encoding="utf-8") as f:
            phases = status_lines(f)
    except OSError as exc:
        print(f"bedrock-smoke: cannot read {argv[0]}: {exc}", file=sys.stderr)
        return 2
    summary = {p: d.get("status") for p, d in phases.items()}
    print(json.dumps({"phases": summary, "report_errors": phases.get("report", {}).get("errors")}))
    failure = check(phases)
    if failure:
        print(failure, file=sys.stderr)
        return 1
    print("bedrock-smoke: pass")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
