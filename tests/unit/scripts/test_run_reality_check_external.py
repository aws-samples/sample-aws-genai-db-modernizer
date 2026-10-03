"""run_standard's external-mode output must report the path the handler actually writes."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from scripts import run_reality_check
from src.storage.local_store import LocalArtifactStore

JOB = "job1"
DB = "dynamodb"


def _fake_handler(job_id, db, store, assignment_version, llm_mode="external"):
    store.write_json(f"{db}/{job_id}/reality-check/llm_input.json", {"fake": "input"})
    return {"status": "awaiting_llm", "input_version": 1, "output_version": None}


def test_run_standard_external_reports_handler_request_path(tmp_path: Path, capsys) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))

    with patch(
        "src.agents.referee.reality_check_handler.run_reality_check_handler",
        side_effect=_fake_handler,
    ):
        run_reality_check.run_standard(store, JOB, DB, 1, "external")

    out = json.loads(capsys.readouterr().out)

    assert out["status"] == "awaiting_llm"
    assert out["llm_request"] == f"{DB}/{JOB}/reality-check/llm_input.json"
    assert out["llm_response"] == f"{DB}/{JOB}/llm_responses/reality_check.json"
