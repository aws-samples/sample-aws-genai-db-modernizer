# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""No saved assignment or report names a placeholder table (#2147).

The collector writes ``tables_accessed: ["unknown"]`` for a statement it names no table
for. That placeholder (or a dummy ``DUAL``, ``placeholder_tables.PLACEHOLDER_TABLE_NAMES``)
is no table: no assignment version, access-pattern record or report may list it where
a table goes, and no engine choice may be built for it. Checked on every bundled
sample's deterministic run, and on the DNN Platform sample, whose two constant
``SELECT`` statements over a derived one-row table tie on every engine: they used to
make an engine choice for a table named ``unknown``, also once ``--decide-for-me``
settled the open choices.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 -- runs this repo's own scripts/ entry points with fixed argv
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from src.shared.placeholder_tables import PLACEHOLDER_TABLE_NAMES
from tests.e2e.pipeline import REPO, PipelineResult, _env

# The keys whose value is a table name, a list of them, or a choice's id (a table's).
_TABLE_KEYS = frozenset(
    {"table", "table_id", "table_name", "tables", "source_tables", "choice_id", "home_table"}
)


def _placeholders(node: Any, path: str = "") -> Iterator[str]:
    """Every ``path`` in a JSON document where a table name is a placeholder."""
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}"
            if key == "write_groups" and isinstance(value, dict):
                yield from (f"{here}[{k!r}]" for k in value if k in PLACEHOLDER_TABLE_NAMES)
            if key in _TABLE_KEYS:
                names = value if isinstance(value, list) else [value]
                yield from (
                    f"{here}={n!r}"
                    for n in names
                    if isinstance(n, str) and n in PLACEHOLDER_TABLE_NAMES
                )
            yield from _placeholders(value, here)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            yield from _placeholders(item, f"{path}[{i}]")


def _saved_documents(job: Path) -> list[Path]:
    return sorted(
        [
            *job.glob("assignment/v*/assignment.json"),
            *job.glob("assignment/v*/access_patterns.json"),
            *job.glob("synthesis/v*/report.json"),
        ]
    )


def _findings(job: Path) -> list[str]:
    out = []
    for doc in _saved_documents(job):
        rel = doc.relative_to(job)
        out += [f"{rel}: {p}" for p in _placeholders(json.loads(doc.read_text()))]
    report = job / "deliverables" / "engineering-report.md"
    if report.exists():
        text = report.read_text()
        out += [
            f"engineering-report.md: `{n}`" for n in PLACEHOLDER_TABLE_NAMES if f"`{n}`" in text
        ]
    return out


# Deterministic only: an external job (a saved headless run, ci/replay.sh) holds the
# assignments the code that ran it wrote, which predate #2147 for every saved one.
@pytest.mark.deterministic
def test_no_saved_assignment_or_report_names_a_placeholder_table(run: PipelineResult) -> None:
    job = run.job_dir()
    assert _saved_documents(job), f"no assignment or report under {job}"
    assert _findings(job) == []


def _script(cwd: Path, *args: str) -> list[dict]:
    env = {**_env(), "PYTHONPATH": str(REPO)}
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [sys.executable, str(REPO / args[0]), *args[1:]],
        cwd=cwd,
        capture_output=True,
        text=True,
        env=env,
        timeout=600,
    )
    assert proc.returncode == 0, f"{args[0]} failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
    return [json.loads(x) for x in proc.stdout.strip().splitlines() if x.startswith("{")]


@pytest.mark.deterministic
def test_the_dnn_sample_names_no_placeholder_table_before_or_after_decide_for_me(
    dnn_run: PipelineResult, tmp_path: Path
) -> None:
    job = dnn_run.job_dir()
    assert _findings(job) == []
    latest = max(job.glob("assignment/v*/assignment.json"), key=lambda p: int(p.parent.name[1:]))
    unresolved = [
        qa
        for qa in json.loads(latest.read_text())["query_assignments"]
        if not qa["source_tables"] and len(qa.get("tied_engines") or []) >= 2
    ]
    # The two constant SELECTs: still on their tie placeholder, listed as open ties.
    assert len(unresolved) == 2

    root = tmp_path / "artifacts"
    shutil.copytree(job, root / dnn_run.db / "auto")
    common = ["--job-id", "auto", "--db", dnn_run.db, "--artifact-root", str(root)]
    decided = _script(tmp_path, "scripts/run_assessment.py", *common, "--decide-for-me")[-1]
    assert not [d for d in decided.get("decided") or [] if d["table"] in PLACEHOLDER_TABLE_NAMES]
    _script(tmp_path, "scripts/run_synthesis.py", *common, "--llm-mode", "none")
    _script(tmp_path, "scripts/run_report.py", *common)
    assert _findings(root / dnn_run.db / "auto") == []
