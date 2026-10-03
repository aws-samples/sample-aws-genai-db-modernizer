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


def _drafts(store: LocalArtifactStore, groups: int = 2) -> None:
    for g in range(groups):
        store.write_json(f"{BASE}/schema_draft_group_{g}.json", {"group": g})


def _path(root: Path, key: str) -> str:
    """Paths in status lines carry the artifact root (cwd-relative by default)."""
    return f"{root}/{key}"


def _fake_merge(monkeypatch, violations=(), warnings=()):
    """Stand-in for run_schema_merge: writes the merged output, returns the report."""
    from src.agents.schema_design.handler import ScopeReport

    calls: list[dict] = []

    def fake(**kwargs):
        calls.append(kwargs)
        kwargs["store"].write_json(
            f"{BASE}/schema_output.json",
            {"validation_passed": not violations, "validation_failures": list(violations)},
        )
        return ScopeReport(list(violations), list(warnings))

    monkeypatch.setattr("src.agents.schema_design.handler.run_schema_merge", fake)
    return calls


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
    assert status["drafts_invalid"] == []
    assert [g["draft_exists"] for g in status["groups"]] == [False, True, False]
    assert status["groups"][0]["primary_tables"] == ["wordpress.t0"]
    assert status["groups"][0]["draft"] == _path(tmp_path, f"{BASE}/schema_draft_group_0.json")


def test_status_reports_unreadable_drafts_as_invalid(monkeypatch, capsys, tmp_path):
    # merge_schema_groups skips drafts it cannot read, so a malformed draft
    # would silently drop its group from the merge: report it like a missing one.
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _manifest(store, groups=3)
    _drafts(store, groups=3)
    (tmp_path / f"{BASE}/schema_draft_group_1.json").write_text('{"table_definitions": [')
    (tmp_path / f"{BASE}/schema_draft_group_2.json").write_text("[]")  # not an object

    code, status = _run(monkeypatch, capsys, tmp_path, "--status")

    assert code == 0
    assert status["status"] == "drafts_pending"
    assert status["drafts_missing"] == []
    assert status["drafts_invalid"] == [1, 2]
    assert [g["draft_state"] for g in status["groups"]] == ["ok", "invalid", "invalid"]


def test_status_merge_pending_when_all_drafts_exist(monkeypatch, capsys, tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _manifest(store)
    _drafts(store)

    code, status = _run(monkeypatch, capsys, tmp_path, "--status")

    assert code == 0
    assert status["status"] == "merge_pending"
    assert status["drafts_missing"] == [] and status["drafts_invalid"] == []
    assert "--merge" in status["next"]


def test_status_merged_after_a_passing_merge(monkeypatch, capsys, tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _manifest(store)
    _drafts(store)
    _fake_merge(monkeypatch, warnings=["scope warning"])
    _run(monkeypatch, capsys, tmp_path, "--merge")

    code, status = _run(monkeypatch, capsys, tmp_path, "--status")

    assert code == 0
    assert status["status"] == "merged"
    assert status["output_path"] == f"{BASE}/schema_output.json"
    assert status["warnings"] == ["scope warning"]


def test_status_merge_failed_after_a_failing_merge(monkeypatch, capsys, tmp_path):
    # --merge writes schema_output.json even on validation_failed; --status must
    # not report that output as merged.
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _manifest(store)
    _drafts(store)
    _fake_merge(monkeypatch, violations=["DynamoDB merge: table Posts designed twice"])
    _, merge = _run(monkeypatch, capsys, tmp_path, "--merge")
    assert merge["status"] == "validation_failed"

    code, status = _run(monkeypatch, capsys, tmp_path, "--status")

    assert code == 0
    assert status["status"] == "merge_failed"
    assert status["errors"] == ["DynamoDB merge: table Posts designed twice"]


def test_status_merge_failed_when_merged_output_fails_validation(monkeypatch, capsys, tmp_path):
    # Belt and braces: a current merged output with validation_passed false is
    # never "merged", even if the recorded merge outcome says otherwise.
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _manifest(store)
    _drafts(store)
    _fake_merge(monkeypatch)
    _run(monkeypatch, capsys, tmp_path, "--merge")
    store.write_json(
        f"{BASE}/schema_output.json",
        {"validation_passed": False, "validation_failures": ["Out of scope for dynamodb: q9"]},
    )

    _, status = _run(monkeypatch, capsys, tmp_path, "--status")

    assert status["status"] == "merge_failed"
    assert status["errors"] == ["Out of scope for dynamodb: q9"]


def test_editing_a_later_group_draft_flips_merged_to_merge_pending(monkeypatch, capsys, tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _manifest(store, groups=3)
    _drafts(store, groups=3)
    _fake_merge(monkeypatch)
    _run(monkeypatch, capsys, tmp_path, "--merge")
    assert _run(monkeypatch, capsys, tmp_path, "--status")[1]["status"] == "merged"

    store.write_json(f"{BASE}/schema_draft_group_2.json", {"group": 2, "edited": True})

    _, status = _run(monkeypatch, capsys, tmp_path, "--status")
    assert status["status"] == "merge_pending"


def test_status_merge_pending_when_merge_record_is_missing(monkeypatch, capsys, tmp_path):
    # An output written by an older --merge (no record of its inputs) is re-merged.
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _manifest(store)
    _drafts(store)
    store.write_json(f"{BASE}/schema_output.json", {"validation_passed": True})

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
    assert status["groups"][1]["input_file"] == _path(tmp_path, f"{BASE}/input_group_1.json")
    assert status["groups"][1]["draft"] == _path(tmp_path, f"{BASE}/schema_draft_group_1.json")


def test_split_paths_are_cwd_relative_under_the_default_artifact_root(
    monkeypatch, capsys, tmp_path
):
    # The default root is ./artifacts; the paths a subagent Reads/Writes must
    # be cwd-relative (`artifacts/...`) so they match Write(artifacts/**).
    def fake_split(*, job_id, database_name, target_type, store, assignment_version):
        _manifest(store)

    monkeypatch.setattr("src.agents.schema_design.handler.run_schema_split", fake_split)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run_schema_design.py", "--job-id", JOB, "--db", DB, "--engine", "dynamodb", "--split"],
    )

    run_schema_design.main()
    status = json.loads(capsys.readouterr().out.splitlines()[-1])

    assert status["groups"][0]["draft"] == f"artifacts/{BASE}/schema_draft_group_0.json"
    assert status["groups"][0]["input_file"] == f"artifacts/{BASE}/input_group_0.json"
    assert status["manifest"] == f"artifacts/{BASE}/groups_manifest.json"
    assert (tmp_path / "artifacts" / BASE / "schema_draft_group_0.json").parent.is_dir()


def test_merge_refuses_while_group_drafts_are_missing(monkeypatch, capsys, tmp_path):
    # Merging a partial set silently drops the missing groups' queries; the
    # orchestrator must re-dispatch those groups instead (#246).
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _manifest(store, groups=3)
    store.write_json(f"{BASE}/schema_draft_group_1.json", {})
    calls = _fake_merge(monkeypatch)

    code, status = _run(monkeypatch, capsys, tmp_path, "--merge")

    assert code == 0
    assert status["status"] == "drafts_pending"
    assert status["missing_groups"] == [0, 2]
    assert status["invalid_groups"] == []
    assert status["assignment_version"] == 1
    assert "errors" not in status  # not a validation failure
    assert calls == []
    assert not store.exists(f"{BASE}/schema_output.json")
    assert not store.exists(f"{BASE}/design_trace.json")
    assert not store.exists(f"{BASE}/merge_record.json")


def test_merge_refuses_on_a_malformed_draft(monkeypatch, capsys, tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _manifest(store)
    _drafts(store)
    (tmp_path / f"{BASE}/schema_draft_group_0.json").write_text("{not json")
    calls = _fake_merge(monkeypatch)

    code, status = _run(monkeypatch, capsys, tmp_path, "--merge")

    assert code == 0
    assert status["status"] == "drafts_pending"
    assert status["missing_groups"] == []
    assert status["invalid_groups"] == [0]
    assert calls == []
    assert not store.exists(f"{BASE}/schema_output.json")


def test_merge_runs_when_every_group_draft_exists(monkeypatch, capsys, tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    _manifest(store)
    _drafts(store)
    calls = _fake_merge(monkeypatch)

    code, status = _run(monkeypatch, capsys, tmp_path, "--merge")

    assert code == 0
    assert status["status"] == "complete"
    assert len(calls) == 1
    record = store.read_json(f"{BASE}/merge_record.json")
    assert record["status"] == "complete"
    assert set(record["draft_hashes"]) == {"0", "1"}
