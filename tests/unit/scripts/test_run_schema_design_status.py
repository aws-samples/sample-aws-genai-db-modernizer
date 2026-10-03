"""``run_schema_design.py --status`` and ``--split`` group listing (issue #246).

A headless ``/modernize`` run ended with no result: DynamoDB group subagents
were nested one level too deep, their completion reached the orchestrator, and
nothing ran ``--merge``. The orchestrator now dispatches the group subagents
itself, so it needs the group list from ``--split`` stdout and a ``--status``
line that says which group drafts exist and whether ``--merge`` is pending,
without reading artifacts or listing directories.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from scripts import run_schema_design
from src.storage.local_store import LocalArtifactStore

DB, JOB = "wordpress", "job-001"
BASE = f"{DB}/{JOB}/schema-dynamodb/v1"


def _run(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture, root: Path, *extra: str):
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
            "dynamodb",
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


def _manifest(store: LocalArtifactStore, groups: int = 2) -> None:
    store.write_json(
        f"{BASE}/groups_manifest.json",
        {
            "job_id": JOB,
            "database_name": DB,
            "target_engine": "dynamodb",
            "total_queries": 3 * groups,
            "total_groups": groups,
            "groups": [
                {
                    "group_index": g,
                    "group_name": f"group_{g}",
                    "primary_tables": [f"wordpress.t{g}"],
                    "query_count": 3,
                    "table_count": 1,
                    "input_file": f"input_group_{g}.json",  # relative, as the splitter writes it
                }
                for g in range(groups)
            ],
        },
    )


def _touch(root: Path, key: str, mtime: float) -> None:
    os.utime(root / key, (mtime, mtime))


def test_status_before_split(monkeypatch, capsys, tmp_path):
    code, status = _run(monkeypatch, capsys, tmp_path, "--status")

    assert code == 0
    assert status["status"] == "not_split"
    assert status["assignment_version"] == 1


def test_status_lists_missing_group_drafts(monkeypatch, capsys, tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _manifest(store, groups=3)
    store.write_json(f"{BASE}/schema_draft_group_1.json", {})

    code, status = _run(monkeypatch, capsys, tmp_path, "--status")

    assert code == 0
    assert status["status"] == "drafts_pending"
    assert status["drafts_missing"] == [0, 2]
    assert [g["draft_exists"] for g in status["groups"]] == [False, True, False]
    assert status["groups"][0]["primary_tables"] == ["wordpress.t0"]
    assert status["groups"][0]["draft"] == f"{BASE}/schema_draft_group_0.json"


def test_status_merge_pending_when_all_drafts_exist(monkeypatch, capsys, tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _manifest(store)
    for g in range(2):
        store.write_json(f"{BASE}/schema_draft_group_{g}.json", {})

    code, status = _run(monkeypatch, capsys, tmp_path, "--status")

    assert code == 0
    assert status["status"] == "merge_pending"
    assert status["drafts_missing"] == []
    assert "--merge" in status["next"]


def test_status_merged_when_output_is_newer_than_every_draft(monkeypatch, capsys, tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _manifest(store)
    for g in range(2):
        store.write_json(f"{BASE}/schema_draft_group_{g}.json", {})
        _touch(tmp_path, f"{BASE}/schema_draft_group_{g}.json", 1_000)
    store.write_json(f"{BASE}/schema_output.json", {})
    _touch(tmp_path, f"{BASE}/schema_output.json", 2_000)

    code, status = _run(monkeypatch, capsys, tmp_path, "--status")

    assert code == 0
    assert status["status"] == "merged"
    assert status["output_path"] == f"{BASE}/schema_output.json"


def test_status_merge_pending_again_after_a_draft_is_edited(monkeypatch, capsys, tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _manifest(store)
    for g in range(2):
        store.write_json(f"{BASE}/schema_draft_group_{g}.json", {})
        _touch(tmp_path, f"{BASE}/schema_draft_group_{g}.json", 1_000)
    store.write_json(f"{BASE}/schema_output.json", {})
    _touch(tmp_path, f"{BASE}/schema_output.json", 2_000)
    _touch(tmp_path, f"{BASE}/schema_draft_group_1.json", 3_000)

    _, status = _run(monkeypatch, capsys, tmp_path, "--status")

    assert status["status"] == "merge_pending"


def test_status_is_dynamodb_only(monkeypatch, capsys, tmp_path):
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
            "elasticache",
            "--artifact-root",
            str(tmp_path),
            "--status",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        run_schema_design.main()
    status = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert exc.value.code == 1
    assert status["status"] == "error"


def test_split_prints_the_groups_for_the_dispatcher(monkeypatch, capsys, tmp_path):
    def fake_split(*, job_id, database_name, target_type, store, assignment_version):
        _manifest(store)

    monkeypatch.setattr("src.agents.schema_design.handler.run_schema_split", fake_split)

    code, status = _run(monkeypatch, capsys, tmp_path, "--split")

    assert code == 0
    assert status["status"] == "split"
    assert [g["group_index"] for g in status["groups"]] == [0, 1]
    assert status["groups"][1]["primary_tables"] == ["wordpress.t1"]
    assert status["groups"][1]["input_file"] == f"{BASE}/input_group_1.json"
    assert status["groups"][1]["draft"] == f"{BASE}/schema_draft_group_1.json"


def test_merge_refuses_while_group_drafts_are_missing(monkeypatch, capsys, tmp_path):
    # Merging a partial set silently drops the missing groups' queries; the
    # orchestrator must re-dispatch those groups instead (#246).
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _manifest(store, groups=3)
    store.write_json(f"{BASE}/schema_draft_group_1.json", {})
    merged = []
    monkeypatch.setattr(
        "src.agents.schema_design.handler.run_schema_merge",
        lambda **kwargs: merged.append(kwargs),
    )

    code, status = _run(monkeypatch, capsys, tmp_path, "--merge")

    assert code == 0
    assert status["status"] == "drafts_pending"
    assert status["missing_groups"] == [0, 2]
    assert status["assignment_version"] == 1
    assert "errors" not in status  # not a validation failure
    assert merged == []
    assert not store.exists(f"{BASE}/schema_output.json")
    assert not store.exists(f"{BASE}/design_trace.json")


def test_merge_runs_when_every_group_draft_exists(monkeypatch, capsys, tmp_path):
    from src.agents.schema_design.handler import ScopeReport

    store = LocalArtifactStore(base_dir=str(tmp_path))
    _manifest(store)
    for g in range(2):
        store.write_json(f"{BASE}/schema_draft_group_{g}.json", {})
    monkeypatch.setattr(
        "src.agents.schema_design.handler.run_schema_merge",
        lambda **kwargs: ScopeReport([], []),
    )

    code, status = _run(monkeypatch, capsys, tmp_path, "--merge")

    assert code == 0
    assert status["status"] == "complete"
