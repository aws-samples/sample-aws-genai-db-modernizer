# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""No saved assignment or report names a placeholder table (#2147).

The collector writes ``tables_accessed: ["unknown"]`` for a statement it names no table
for. That placeholder (or a dummy ``DUAL``, ``placeholder_tables.PLACEHOLDER_TABLE_NAMES``)
is no table: no assignment version, access-pattern record or report may list it where
a table goes, and no engine choice may be named for it. Checked on every bundled
sample's run, and on the DNN Platform sample, whose two constant ``SELECT`` statements
over a derived one-row table tie on every engine: they used to make an engine choice
for a table named ``unknown``. Each is now a choice of its own, "No source table", that
``--decide-for-me`` settles and leaves nothing open; a job saved before #2147 reads the
same way.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess  # nosec B404 -- runs this repo's own scripts/ entry points with fixed argv
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from src.shared.placeholder_tables import PLACEHOLDER_TABLE_NAMES
from tests.e2e.engine_choice_checks import (
    assert_decide_for_me_leaves_nothing_open,
    assert_every_open_tie_is_one_choice,
)
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


# replay_stale: the replay's saved jobs were written before #2147, so their assignments
# still list the collector's ``unknown`` placeholder; a fresh e2e-llm job runs current
# code and is checked here.
@pytest.mark.replay_stale
def test_no_saved_assignment_or_report_names_a_placeholder_table(run: PipelineResult) -> None:
    """``replay_stale``: the replay fixtures' assignments predate #2147's placeholder
    filter; drop the marker when they are refreshed."""
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


def _latest(job: Path) -> Path:
    return max(job.glob("assignment/v*/assignment.json"), key=lambda p: int(p.parent.name[1:]))


def _report(job: Path) -> dict:
    latest = max(job.glob("synthesis/v*/report.json"), key=lambda p: int(p.parent.name[1:]))
    report: dict = json.loads(latest.read_text())
    return report


def _resynthesize(cwd: Path, root: Path, db: str, job: str) -> None:
    common = ["--job-id", job, "--db", db, "--artifact-root", str(root)]
    _script(cwd, "scripts/run_synthesis.py", *common, "--llm-mode", "none")
    _script(cwd, "scripts/run_report.py", *common)


NO_TABLE_DECIDED = (
    "Placed on Aurora PostgreSQL: it tied with DynamoDB, and this statement reads no table. "
    "Decided automatically, review if needed."
)


def _as_saved_before_2147(assignment_file: Path, qids: list[str]) -> None:
    """Rewrite a decided version the way the code before #2147 wrote it: one --auto
    decision for the table ``unknown`` over both statements, their tables ``unknown``
    and their reasons naming it."""
    saved = json.loads(assignment_file.read_text())
    mine = [d for d in saved["auto_decided_choices"] if d["choice_id"].startswith("query:")]
    rest = [d for d in saved["auto_decided_choices"] if d not in mine]
    saved["auto_decided_choices"] = rest + [
        {**mine[0], "choice_id": "unknown", "table": "unknown", "query_ids": sorted(qids)}
    ]
    for qa in saved["query_assignments"]:
        if qa["query_id"] in qids:
            qa["source_tables"] = ["unknown"]
            qa["assignment_reason"] = (
                "Placed on Aurora PostgreSQL: it tied with DynamoDB on table unknown. "
                f"{mine[0]['reason']} Decided automatically, review if needed."
            )
    assignment_file.write_text(json.dumps(saved))


@pytest.mark.deterministic
def test_the_dnn_sample_names_no_placeholder_table_before_or_after_decide_for_me(
    dnn_run: PipelineResult, tmp_path: Path
) -> None:
    job = dnn_run.job_dir()
    assert _findings(job) == []
    report = _report(job)
    # The two constant SELECTs that read no table: each its own choice, for no table.
    no_table = [c for c in report["pending_engine_choices"] if c["table"] == ""]
    qids = sorted(q for c in no_table for q in c["query_ids"])
    assert len(qids) == 2
    assert [c["choice_id"] for c in no_table] == [f"query:{q}" for q in qids]
    assert_every_open_tie_is_one_choice(report)
    md = (job / "deliverables" / "engineering-report.md").read_text()
    for q in qids:
        assert f"| No source table (`{q}`) | 1 |" in md
    # The deliverables list the statements under "No source table" with their reason.
    assert re.search(r"^\| No source table \| 2 \| [0-9.]+ \| .*pending your choice", md, re.M)

    root = tmp_path / "artifacts"
    shutil.copytree(job, root / dnn_run.db / "auto")
    common = ["--job-id", "auto", "--db", dnn_run.db, "--artifact-root", str(root)]
    lines = _script(tmp_path, "scripts/run_assessment.py", *common, "--decide-for-me")
    decided = next(x for x in lines if x.get("phase") == "engine_choices")
    assert not [d for d in decided.get("decided") or [] if d["table"] in PLACEHOLDER_TABLE_NAMES]
    assert sorted(d["choice"] for d in decided["decided"] if d["table"] == "No source table") == [
        f"query:{q}" for q in qids
    ]
    _resynthesize(tmp_path, root, dnn_run.db, "auto")
    auto = root / dnn_run.db / "auto"
    assert _findings(auto) == []
    after = json.loads(_latest(auto).read_text())
    assert_decide_for_me_leaves_nothing_open(after, _report(auto))
    reasons = {qa["query_id"]: qa["assignment_reason"] for qa in after["query_assignments"]}
    assert [reasons[q] for q in qids] == [NO_TABLE_DECIDED] * 2
    md = (auto / "deliverables" / "engineering-report.md").read_text()
    assert "| No source table | 2 | " in md and NO_TABLE_DECIDED in md

    # A job saved before #2147 (one decision for ``unknown``) re-synthesizes to the same
    # per-statement decisions, its reasons worded to match.
    shutil.copytree(auto, root / dnn_run.db / "old")
    _as_saved_before_2147(_latest(root / dnn_run.db / "old"), qids)
    _resynthesize(tmp_path, root, dnn_run.db, "old")
    old_report = _report(root / dnn_run.db / "old")

    def placed(r: dict) -> list[tuple[str, str, str]]:
        return sorted((d["choice_id"], d["table"], d["engine"]) for d in r["auto_decided_choices"])

    assert placed(old_report) == placed(_report(auto))
    old_md = (root / dnn_run.db / "old" / "deliverables" / "engineering-report.md").read_text()
    assert "`unknown`" not in old_md and "an unresolved table" not in old_md
    assert NO_TABLE_DECIDED in old_md
