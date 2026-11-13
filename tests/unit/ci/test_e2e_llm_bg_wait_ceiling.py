# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""ci/e2e-llm.sh gives background subagents 30 minutes in `claude -p` (#2175).

Claude Code in print mode stops background subagents 10 minutes after the main turn
ends; the slowest design subagent measured took 13.5 minutes.
"""

from __future__ import annotations

import subprocess  # nosec B404 -- runs a line of this repo's own ci/e2e-llm.sh in bash
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "ci" / "e2e-llm.sh"
VAR = "CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS"


def _exported(caller_value: str | None) -> str:
    """Run the script's own line that sets VAR and print what a child process sees."""
    lines = [ln.strip() for ln in SCRIPT.read_text().splitlines() if VAR in ln and "export" in ln]
    assert len(lines) == 1, lines
    env = {"PATH": "/usr/bin:/bin"}
    if caller_value is not None:
        env[VAR] = caller_value
    proc = subprocess.run(  # nosec B603 B607 -- fixed argv
        ["bash", "-c", lines[0] + f"; bash -c 'printf %s \"${VAR}\"'"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == 0, proc
    return proc.stdout


def test_the_ceiling_defaults_to_thirty_minutes() -> None:
    assert _exported(None) == "1800000"


def test_a_callers_ceiling_is_kept() -> None:
    assert _exported("900000") == "900000"
