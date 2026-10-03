"""``run_schema_design.py --finalize --engine dynamodb`` (issue #197).

DynamoDB designs through split -> per-group drafts -> ``--merge``; ``--merge``
writes the final ``schema-dynamodb/v{N}/schema_output.json`` and nothing ever
writes ``llm_responses/schema_design_dynamodb.json``. A headless run that also
called ``--finalize`` crashed with a FileNotFoundError traceback. ``--finalize``
for DynamoDB must instead report the merged output or explain that
``--merge`` is the final step, always as one JSON status line.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from scripts import run_schema_design
from src.storage.local_store import LocalArtifactStore

DB, JOB = "wordpress", "job-001"


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


def test_finalize_reports_complete_when_merged_output_exists(monkeypatch, capsys, tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    output_key = f"{DB}/{JOB}/schema-dynamodb/v1/schema_output.json"
    store.write_json(output_key, {"validation_passed": True})

    code, status = _run(monkeypatch, capsys, tmp_path, "--finalize")

    assert code == 0
    assert status["status"] == "complete"
    assert status["output_path"] == output_key
    assert status["assignment_version"] == 1
    # No LLM response is needed (or created) for DynamoDB.
    assert not store.exists(f"{DB}/{JOB}/llm_responses/schema_design_dynamodb.json")


def test_finalize_uses_effective_assignment_version(monkeypatch, capsys, tmp_path):
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(f"{DB}/{JOB}/assignment/v2/assignment.json", {"query_assignments": []})
    store.write_json(f"{DB}/{JOB}/schema-dynamodb/v1/schema_output.json", {})

    code, status = _run(monkeypatch, capsys, tmp_path, "--finalize")

    # v1 output alone does not satisfy a v2 finalize.
    assert code == 1
    assert status["status"] == "error"


def test_finalize_without_merged_output_is_a_clear_error(monkeypatch, capsys, tmp_path):
    code, status = _run(monkeypatch, capsys, tmp_path, "--finalize")

    assert code == 1
    assert status == {
        "status": "error",
        "message": (
            "DynamoDB schema design finalizes with --merge; run --merge after the group drafts"
        ),
    }
