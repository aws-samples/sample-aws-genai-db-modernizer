"""``run_schema_design.py --engine dynamodb --check-costs <draft>`` (issue #198).

External-mode group subagents have no Strands ``compute_performances_and_costs``
tool, so every draft ended ``validation_passed: false``. ``--check-costs`` runs
the same computation on a group draft and prints the result as JSON. It only
reads drafts inside the job's ``schema-dynamodb/`` directory, and never writes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts import _sandbox, run_schema_design

DB, JOB = "wordpress", "job-001"

HOT_PARTITIONS = [
    {
        "table_name": "Options",
        "operation": "read",
        "rcu_or_wcu_per_second": 300.0,
        "partition_limit": 3000,
        "utilization_pct": 10.0,
        "at_risk": False,
        "contributing_patterns": ["q1"],
    }
]


def _run(monkeypatch, capsys, root: Path, *extra: str, engine: str = "dynamodb"):
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_schema_design.py",
            "--job-id",
            JOB,
            "--db",
            DB,
            "--engine",
            engine,
            "--artifact-root",
            str(root),
            *extra,
        ],
    )
    code = 0
    try:
        run_schema_design.main()
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    return code, json.loads(lines[-1])


def _write_draft(root: Path, draft: dict, rel: str = "v1/schema_draft_group_0.json") -> Path:
    path = root / DB / JOB / "schema-dynamodb" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(draft))
    return path


def test_check_costs_passes_and_leaves_the_draft_untouched(monkeypatch, capsys, tmp_path):
    draft = {"hot_partition_analysis": HOT_PARTITIONS, "validation_passed": False}
    path = _write_draft(tmp_path, draft)
    before = path.read_text()

    code, status = _run(monkeypatch, capsys, tmp_path, "--check-costs", str(path))

    assert code == 0
    assert status["status"] == "complete"
    assert status["passed"] is True
    assert status["errors"] == []
    assert status["draft"] == str(path)
    assert len(status["results"]) == 1
    assert path.read_text() == before


def test_check_costs_reports_failure_without_erroring(monkeypatch, capsys, tmp_path):
    bad = [{**HOT_PARTITIONS[0], "at_risk": True, "utilization_pct": 95.0}]
    path = _write_draft(tmp_path, {"hot_partition_analysis": bad})

    code, status = _run(monkeypatch, capsys, tmp_path, "--check-costs", str(path))

    assert code == 0
    assert status["status"] == "complete"
    assert status["passed"] is False
    assert "mitigation is required" in status["errors"][0]["error"]


@pytest.mark.parametrize(
    "rel",
    [
        "outside.json",  # artifact root, not the job
        f"{DB}/other-job/schema-dynamodb/v1/schema_draft_group_0.json",  # another job
        f"{DB}/{JOB}/schema-documentdb/v1/schema_draft_group_0.json",  # another engine
        f"{DB}/{JOB}/schema-dynamodb/../collector/output.json",  # traversal
    ],
)
def test_check_costs_rejects_paths_outside_the_job_schema_dir(monkeypatch, capsys, tmp_path, rel):
    (tmp_path / DB / JOB / "schema-dynamodb").mkdir(parents=True)
    target = tmp_path / rel
    target.resolve().parent.mkdir(parents=True, exist_ok=True)
    target.resolve().write_text(json.dumps({"hot_partition_analysis": HOT_PARTITIONS}))

    code, status = _run(monkeypatch, capsys, tmp_path, "--check-costs", str(target))

    assert code == 1
    assert status["status"] == "error"
    assert "schema-dynamodb" in status["message"]


def test_check_costs_rejects_symlink_escaping_the_job_dir(monkeypatch, capsys, tmp_path):
    outside = tmp_path / "secret.json"
    outside.write_text(json.dumps({"hot_partition_analysis": HOT_PARTITIONS}))
    link = tmp_path / DB / JOB / "schema-dynamodb" / "v1" / "schema_draft_group_0.json"
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)

    code, status = _run(monkeypatch, capsys, tmp_path, "--check-costs", str(link))

    assert code == 1
    assert status["status"] == "error"


def test_check_costs_requires_dynamodb(monkeypatch, capsys, tmp_path):
    path = _write_draft(tmp_path, {"hot_partition_analysis": HOT_PARTITIONS})

    code, status = _run(
        monkeypatch, capsys, tmp_path, "--check-costs", str(path), engine="documentdb"
    )

    assert code == 1
    assert status["status"] == "error"
    assert "dynamodb" in status["message"]


@pytest.mark.parametrize("content", [None, "{not json", "[]"])
def test_check_costs_missing_or_unreadable_draft_is_an_error(
    monkeypatch, capsys, tmp_path, content
):
    path = tmp_path / DB / JOB / "schema-dynamodb" / "v1" / "schema_draft_group_0.json"
    path.parent.mkdir(parents=True)
    if content is not None:
        path.write_text(content)

    code, status = _run(monkeypatch, capsys, tmp_path, "--check-costs", str(path))

    assert code == 1
    assert status["status"] == "error"


def test_sandbox_contains_the_check_costs_path(tmp_path):
    from argparse import Namespace

    args = Namespace(check_costs=str(tmp_path / "draft.json"))
    message = _sandbox.sandbox_violation(
        args, environ={_sandbox.SANDBOX_ENV: "1"}, repo_root=tmp_path / "repo"
    )
    assert message is not None
    assert "--check-costs" in message
