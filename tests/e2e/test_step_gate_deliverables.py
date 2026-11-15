# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's gate evidence reaches every deliverable after ``/move``'s gate (#1801).

The deterministic wordpress job, copied, gets a saved step 1 gate file (synthetic: as
``/move``'s read-only gate writes ``data-movement/step-1/gate.json``), then
``scripts/run_report.py`` renders its deliverables again, as the gate script does after
each read. The engineering report, the executive report, the assessment data and the
results payload state the same ``facts.step_gate``, the cutover message verbatim with
the evidence, and no cutover advice. Without the gate file, nothing changes.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # nosec B404 -- runs this repo's own scripts/ entry point with fixed argv
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src.report.escaping import md_text
from src.shared.cutover_message import CUTOVER_DISCLAIMER, REPLICATING_MESSAGE
from src.shared.report_facts import step_gate_facts, step_gate_key
from src.storage.local_store import LocalArtifactStore
from tests.e2e.pipeline import REPO, PipelineResult, _env
from tests.executive_pdf import body_text, normalize
from tests.unit.docs.test_no_cutover_advice import _findings
from tests.unit.report import step_gate_fixtures as fx

HEADING = "## Step 1 gate evidence"


def _report(root: Path, run: PipelineResult) -> dict:
    proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
        [sys.executable, "scripts/run_report.py", "--job-id", run.job_id, "--db", run.db,
         "--artifact-root", str(root)],
        cwd=REPO, capture_output=True, text=True, env=_env(), timeout=900,
    )  # fmt: skip
    assert (
        proc.returncode == 0
    ), f"run_report.py failed:\n{proc.stdout[-2000:]}\n{proc.stderr[-2000:]}"
    payload: dict = json.loads(proc.stdout.strip().splitlines()[-1])
    assert payload["status"] in ("complete", "partial"), payload
    return payload


def _copy(run: PipelineResult, tmp_path: Path) -> Path:
    root = tmp_path / "artifacts"
    shutil.copytree(run.job_dir(), root / run.db / run.job_id)
    return root


def _without_front_matter(md: str) -> str:
    return md.split("\n---\n", 1)[1] if md.startswith("---") else md


@pytest.mark.deterministic
@pytest.mark.parametrize("gate", ["passed", "failed"])
def test_the_gate_evidence_reaches_every_deliverable(
    wordpress_run: PipelineResult, tmp_path: Path, gate: str
) -> None:
    run = wordpress_run
    root = _copy(run, tmp_path)
    store = LocalArtifactStore(base_dir=str(root))
    saved = getattr(fx, gate)()
    store.write_json(step_gate_key(run.db, run.job_id), saved)
    expected = step_gate_facts(saved)
    assert expected is not None
    _report(root, run)
    folder = root / run.db / run.job_id / "deliverables"

    # The assessment data: the block itself.
    data = json.loads((folder / "assessment-data.json").read_text())
    assert data["facts"]["step_gate"] == expected

    # The engineering report: its own section, after the roadmap, the evidence as worded.
    md = (folder / "engineering-report.md").read_text()
    assert md.count(HEADING) == 1
    headings = [line for line in md.splitlines() if line.startswith("## ")]
    assert headings[headings.index(HEADING) - 1].startswith("## Migration roadmap")
    section = md.split(HEADING + "\n", 1)[1].split("\n## ", 1)[0]
    assert f"**{expected['status_text']}.** {md_text(expected['result_line'])}" in section
    assert section.count(f"*{CUTOVER_DISCLAIMER}*") == 1
    for key in ("load_line", "lag_line", "validation_line"):
        assert f"- {md_text(expected[key])}" in section, key
    assert (REPLICATING_MESSAGE in section) == (gate == "passed")

    # The executive report: the result, the time of the read, the same lines, the message.
    text = normalize(body_text(folder / "executive-report.pdf", run.db))
    block = text.split(f"Step 1 gate, read {expected['read_at_text']}:", 1)[1]
    assert block.lstrip().startswith(f"{expected['status_text']}. {expected['result_line']}")
    for key in ("load_line", "lag_line", "validation_line"):
        assert normalize(expected[key]) in block, key
    assert CUTOVER_DISCLAIMER in block

    # No cutover advice anywhere the evidence now appears.
    assert _findings(f"{run.db} engineering-report.md", section, deliverable=True) == []
    assert _findings(f"{run.db} executive-report.pdf", block, deliverable=True) == []

    # The results page reads the same block from the results payload.
    from src.api.main import app
    from src.api.routes import results
    from src.api.services.local_s3 import LocalS3Service

    sfn = MagicMock()
    sfn.describe_execution.return_value = {
        "status": "SUCCEEDED",
        "input": {"database_name": run.db},
    }
    results.sfn_service, results.s3_service = sfn, LocalS3Service(store)
    try:
        response = TestClient(app).get(f"/api/v1/assessments/{run.job_id}/results")
    finally:
        results.sfn_service = results.s3_service = None
    assert response.status_code == 200, response.text[:500]
    assert response.json()["synthesis"]["facts"]["step_gate"] == expected


@pytest.mark.deterministic
def test_without_a_gate_file_the_deliverables_are_unchanged(
    wordpress_run: PipelineResult, tmp_path: Path
) -> None:
    run = wordpress_run
    root = _copy(run, tmp_path)
    _report(root, run)
    folder = root / run.db / run.job_id / "deliverables"
    md = (folder / "engineering-report.md").read_text()
    assert HEADING not in md and CUTOVER_DISCLAIMER not in md
    assert _without_front_matter(md) == _without_front_matter(
        run.deliverable("engineering-report.md").read_text()
    )
    data = json.loads((folder / "assessment-data.json").read_text())
    assert (data.get("facts") or {}).get("step_gate") is None
    original = json.loads(run.deliverable("assessment-data.json").read_text())
    assert {k: v for k, v in data.items() if k != "_artifact"} == {
        k: v for k, v in original.items() if k != "_artifact"
    }
