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


# Issue #313: a headless run looped `for g in 0 1 2 3; do ... --check-costs
# ...; done` because `--check-costs` took exactly one draft and there was no
# single command to re-check every group. `--check-costs` now takes several
# paths, and `--check-costs-all` discovers every group draft of the job's
# current assignment version itself.


def test_check_costs_accepts_several_drafts_in_one_call(monkeypatch, capsys, tmp_path):
    passing = {"hot_partition_analysis": HOT_PARTITIONS}
    failing = {
        "hot_partition_analysis": [{**HOT_PARTITIONS[0], "at_risk": True, "utilization_pct": 95.0}]
    }
    p0 = _write_draft(tmp_path, passing, rel="v1/schema_draft_group_0.json")
    p1 = _write_draft(tmp_path, failing, rel="v1/schema_draft_group_1.json")

    code, status = _run(monkeypatch, capsys, tmp_path, "--check-costs", str(p0), str(p1))

    assert code == 0
    assert status["status"] == "complete"
    assert status["passed"] is False  # overall: not every group passed
    assert len(status["groups"]) == 2
    assert status["groups"][0]["draft"] == str(p0)
    assert status["groups"][0]["passed"] is True
    assert status["groups"][1]["draft"] == str(p1)
    assert status["groups"][1]["passed"] is False


def test_check_costs_multi_draft_errors_on_first_bad_path(monkeypatch, capsys, tmp_path):
    good = _write_draft(tmp_path, {"hot_partition_analysis": HOT_PARTITIONS})

    code, status = _run(
        monkeypatch, capsys, tmp_path, "--check-costs", str(good), str(tmp_path / "outside.json")
    )

    assert code == 1
    assert status["status"] == "error"


def test_check_costs_all_checks_every_group_draft_of_the_version(monkeypatch, capsys, tmp_path):
    passing = {"hot_partition_analysis": HOT_PARTITIONS}
    failing = {
        "hot_partition_analysis": [{**HOT_PARTITIONS[0], "at_risk": True, "utilization_pct": 95.0}]
    }
    _write_draft(tmp_path, passing, rel="v1/schema_draft_group_0.json")
    _write_draft(tmp_path, passing, rel="v1/schema_draft_group_1.json")
    _write_draft(tmp_path, failing, rel="v1/schema_draft_group_2.json")

    code, status = _run(
        monkeypatch, capsys, tmp_path, "--check-costs-all", "--assignment-version", "1"
    )

    assert code == 0
    assert status["status"] == "complete"
    assert status["passed"] is False
    assert [g["draft"].rsplit("_", 1)[-1] for g in status["groups"]] == [
        "0.json",
        "1.json",
        "2.json",
    ]


def test_check_costs_all_drops_results_but_keeps_the_summary_fields(monkeypatch, capsys, tmp_path):
    """Review of #375: on a sample with many groups and access patterns, printing
    every group's full "results" (the validated entry per access pattern, the one
    field that scales with entry count) made --check-costs-all's own stdout large
    enough that the harness persisted it to a file outside the repo -- no allowed
    tool in a headless session could then read it back to check the per-group
    "passed" flags the merge-fix task actually needed. "results" is dropped from
    each group here; "per_table" and "hot_partition_findings" already summarise
    everything a caller needs from it, so they stay."""
    _write_draft(
        tmp_path, {"hot_partition_analysis": HOT_PARTITIONS}, rel="v1/schema_draft_group_0.json"
    )
    _write_draft(
        tmp_path, {"hot_partition_analysis": HOT_PARTITIONS}, rel="v1/schema_draft_group_1.json"
    )

    code, status = _run(
        monkeypatch, capsys, tmp_path, "--check-costs-all", "--assignment-version", "1"
    )

    assert code == 0
    for group in status["groups"]:
        assert "results" not in group
        assert "per_table" in group
        assert "hot_partition_findings" in group
        assert "passed" in group
        assert "entry_count" in group


def test_check_costs_single_draft_still_prints_results_in_full(monkeypatch, capsys, tmp_path):
    """The single-draft shape is unchanged: a merge-fix pass reading one group's
    own draft still gets "results" in full."""
    path = _write_draft(tmp_path, {"hot_partition_analysis": HOT_PARTITIONS})

    code, status = _run(monkeypatch, capsys, tmp_path, "--check-costs", str(path))

    assert code == 0
    assert "results" in status
    assert len(status["results"]) == 1


def test_check_costs_all_is_dynamodb_only(monkeypatch, capsys, tmp_path):
    _write_draft(tmp_path, {"hot_partition_analysis": HOT_PARTITIONS})

    code, status = _run(
        monkeypatch,
        capsys,
        tmp_path,
        "--check-costs-all",
        "--assignment-version",
        "1",
        engine="documentdb",
    )

    assert code == 1
    assert status["status"] == "error"
    assert "dynamodb" in status["message"]


def test_check_costs_all_errors_when_no_group_drafts_exist(monkeypatch, capsys, tmp_path):
    code, status = _run(
        monkeypatch, capsys, tmp_path, "--check-costs-all", "--assignment-version", "1"
    )

    assert code == 1
    assert status["status"] == "error"


def test_check_costs_and_check_costs_all_are_mutually_exclusive(monkeypatch, capsys, tmp_path):
    path = _write_draft(tmp_path, {"hot_partition_analysis": HOT_PARTITIONS})

    code, status = _run(
        monkeypatch,
        capsys,
        tmp_path,
        "--check-costs",
        str(path),
        "--check-costs-all",
        "--assignment-version",
        "1",
    )

    assert code == 1
    assert status["status"] == "error"


def test_sandbox_contains_every_check_costs_path(tmp_path):
    from argparse import Namespace

    args = Namespace(check_costs=[str(tmp_path / "a.json"), str(tmp_path / "b.json")])
    message = _sandbox.sandbox_violation(
        args, environ={_sandbox.SANDBOX_ENV: "1"}, repo_root=tmp_path / "repo"
    )
    assert message is not None
    assert "--check-costs" in message
