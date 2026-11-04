# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Concurrent ``assess()`` calls with no per-run ceiling set behave as on main (#145).

The per-run ceilings are opt-in: only a run with one set takes the process's run lock
(see ``test_run_usage_and_ceilings.py``). This file imports nothing #145 added, so it
also runs on main's code, where it passes.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.assessment import assess, runner

DB = "wordpress"
CEILINGS = (
    "MODERNIZER_MAX_RUN_TOKENS",
    "MODERNIZER_MAX_RUN_MODEL_CALLS",
    "MODERNIZER_MAX_RUN_SECONDS",
)


@pytest.mark.passes_on_main  # no ceiling set: assess() keeps main's behaviour
def test_two_concurrent_runs_with_no_ceiling_both_succeed(monkeypatch) -> None:
    """Without a ceiling no lock is taken: two deterministic ``assess()`` calls running
    at once in one process both return their result, as on main."""
    import threading

    from tests.unit.assessment.conftest import collection

    for name in CEILINGS:
        monkeypatch.delenv(name, raising=False)
    first_phase = runner.PHASES[0][1]
    both_inside = threading.Barrier(2, timeout=60)

    def analyse(job: Any) -> None:
        both_inside.wait()  # the two runs are in a phase at the same time
        first_phase(job)

    monkeypatch.setattr(runner, "PHASES", (("analysis", analyse), *runner.PHASES[1:]))
    request = {
        "database_name": DB,
        "collection": collection(DB),
        "options": {"run_through": "analysis"},
    }
    results: dict[str, Any] = {}

    def run(job_id: str) -> None:
        try:
            results[job_id] = assess(request, job_id=job_id)
        except BaseException as exc:  # noqa: BLE001 - asserted below
            results[job_id] = exc

    threads = [threading.Thread(target=run, args=(j,)) for j in ("twin0001", "twin0002")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=120)
    assert set(results) == {"twin0001", "twin0002"}
    for result in results.values():
        assert not isinstance(result, BaseException), result
        assert result.statements
