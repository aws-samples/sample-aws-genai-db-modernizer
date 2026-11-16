# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#2143: the ADR invariants, as queries, hold on every e2e job.

``scripts/check_invariants.py`` loads the job's saved artifacts into DuckDB and runs one
query per invariant (``src/tools/invariants/checks.py``). Not marked ``deterministic``:
in e2e-llm it runs over the headless run's own job (``ci/e2e-llm.sh`` also runs the
script as its own required ``invariants`` check). The deterministic pipeline designs
nothing, so the design split checks run on the split copy
``test_design_split_units`` makes, and the design checks on the designed wordpress job.
"""

from __future__ import annotations

import json
import subprocess  # nosec B404 -- runs this repo's own script with fixed argv
import sys
from pathlib import Path
from typing import Any

import pytest

from tests.e2e.pipeline import REPO, PipelineResult
from tests.e2e.test_design_split_units import _designs_in_parts

COVERAGE = "design_covers_its_statements"
# What every job, deterministic or a model's, has to check: never "nothing to check".
ALWAYS_APPLIES = (
    "co_dependency_group_one_engine",
    "write_group_one_engine",
    "one_writer_per_table",
    "cache_never_owns",
    "ledger_explains_every_engine",
    "facts_recount",
)


def _check(job_dir: Path) -> dict[str, Any]:
    proc = subprocess.run(  # nosec B603 -- fixed interpreter and this repo's script
        [sys.executable, str(REPO / "scripts" / "check_invariants.py"), str(job_dir)],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.strip()]
    assert lines, proc.stderr[-2000:]
    result: dict[str, Any] = json.loads(lines[-1])
    assert result["status"] in ("pass", "fail"), result
    assert proc.returncode == (0 if result["status"] == "pass" else 1)
    return result


def _failures(result: dict[str, Any], skip: tuple[str, ...] = ()) -> dict[str, Any]:
    return {k: v for k, v in result["failures"].items() if k not in skip}


def test_every_invariant_holds_on_the_job(run: PipelineResult) -> None:
    result = _check(run.job_dir())
    assert _failures(result, skip=(COVERAGE,)) == {}
    for name in ALWAYS_APPLIES:
        assert result["checks"][name]["status"] == "pass", (name, result["checks"][name])


# replay_stale: the saved discourse run's DynamoDB design predates #2153's coverage check
# and leaves two statements unnamed (#2378 re-records it); a fresh run is checked here.
@pytest.mark.replay_stale
def test_each_design_names_every_statement_it_is_given(run: PipelineResult) -> None:
    result = _check(run.job_dir())
    assert result["failures"].get(COVERAGE) is None, result["failures"][COVERAGE]


def test_every_invariant_holds_on_the_jobs_design_split(
    run: PipelineResult, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """The split checks (no unrecorded unit cut, no write outside its owner's part) on
    the job's design split: the headless run's own, or a split copy of a deterministic
    job. The cut checks have something to check on every deterministic sample."""
    _, job_dir = _designs_in_parts(run, tmp_path_factory)
    result = _check(job_dir)
    assert _failures(result, skip=(COVERAGE,)) == {}
    if not run.external:
        assert result["checks"]["no_unrecorded_unit_cut"]["status"] == "pass"


@pytest.mark.deterministic
def test_every_invariant_holds_on_the_designed_job(designed_run: PipelineResult) -> None:
    """The design checks on the one deterministic job with a design for every engine."""
    result = _check(designed_run.job_dir())
    assert result["status"] == "pass", result["failures"]
    for name in (*ALWAYS_APPLIES, "design_names_only_own_statements", COVERAGE):
        assert result["checks"][name]["status"] == "pass", (name, result["checks"][name])
