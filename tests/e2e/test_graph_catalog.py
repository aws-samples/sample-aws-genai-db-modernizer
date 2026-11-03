# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Every graph query catalog entry on the live pipeline runs (#307).

The job's graph is opened the way ``scripts/graph_query.py`` opens it
(``job_graph.open_job_graph``). Every entry runs with its parameters' choices,
trying the next one while a choice returns nothing, and each one returns rows within its bound, or nothing with a declared
need that names the stage the job did not run. No count is pinned: the figures
move with routing, and what this checks is that each question is answerable.

``test_every_entry_on_the_run`` runs on each deterministic sample (wordpress,
discourse, trailgear: the ``run`` fixture) and, under
e2e-llm, on the external job (with schema design and an LLM synthesis), so it
carries no ``deterministic`` marker. The designed wordpress run only exists in
the deterministic suite.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess  # nosec B404 -- runs this repo's own script with fixed argv
import sys
from pathlib import Path

import pytest

from src.graph import catalog, job_graph
from src.storage.local_store import LocalArtifactStore
from tests.e2e.pipeline import PipelineResult

REPO = Path(__file__).resolve().parents[2]

#: Parameter choices tried per entry before an empty result must be explained.
CHOICES_TRIED = 25


def _copy(result: PipelineResult, tmp_path: Path) -> Path:
    """A copy of the job, so the graph files the CLI writes stay out of the session's run."""
    root = tmp_path / "artifacts"
    shutil.copytree(result.job_dir(), root / result.db / result.job_id)
    return root


def _check_every_entry(result: PipelineResult, tmp_path: Path) -> dict[str, int]:
    """Run every entry on the job's graph; return each entry's row count."""
    root = _copy(result, tmp_path)
    store = LocalArtifactStore(base_dir=str(root))
    counts: dict[str, int] = {}
    with job_graph.open_job_graph(store, str(root), result.db, result.job_id) as (graph, _):
        for entry in catalog.entries():
            unmet = catalog.unmet_needs(graph, entry)
            choices = [list(catalog.param_choices(graph, p))[:CHOICES_TRIED] for p in entry.params]
            if any(not c for c in choices):
                assert unmet, f"{entry.id}: a parameter has no choices and no need explains it"
                counts[entry.id] = 0
                continue
            rows: list[dict] = []
            # Try each choice (the first CHOICES_TRIED of each parameter, in step) until
            # one returns rows: a routing change may list a value with no rows first.
            for i in range(max((len(c) for c in choices), default=1)):
                params = {
                    p.name: c[min(i, len(c) - 1)]
                    for p, c in zip(entry.params, choices, strict=True)
                }
                out = catalog.run_entry(graph, entry, params)
                assert len(out.rows) <= entry.max_rows, entry.id
                rows = out.rows
                if rows or not entry.params:
                    break
            if not rows:
                assert unmet, f"{entry.id}: empty on {result.db} and no declared need explains it"
            for row in rows:
                assert not any(isinstance(v, float) and math.isnan(v) for v in row.values()), (
                    entry.id,
                    row,
                )
            counts[entry.id] = len(rows)
    return counts


def test_every_entry_on_the_run(run: PipelineResult, tmp_path: Path) -> None:
    counts = _check_every_entry(run, tmp_path)
    # Questions every finished job can answer, whatever the routing.
    for entry_id in (
        "node_census",
        "engine_distribution",
        "table_engines",
        "top_traffic_queries",
        "version_chain",
    ):
        assert counts[entry_id] > 0, entry_id


@pytest.mark.deterministic
def test_access_pattern_entries_on_the_designed_run(
    designed_run: PipelineResult, tmp_path: Path
) -> None:
    counts = _check_every_entry(designed_run, tmp_path)
    for entry_id in ("access_patterns_per_engine", "access_pattern_queries"):
        assert counts[entry_id] > 0, entry_id


def test_the_cli_runs_an_entry_on_the_run(run: PipelineResult, tmp_path: Path) -> None:
    root = _copy(run, tmp_path)
    proc = subprocess.run(  # nosec B603 -- fixed argv
        [
            sys.executable,
            str(REPO / "scripts" / "graph_query.py"),
            "run",
            "engine_distribution",
            "--job-id",
            run.job_id,
            "--db",
            run.db,
            "--artifact-root",
            str(root),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip())
    assert out["status"] == "ok" and out["row_count"] > 0
