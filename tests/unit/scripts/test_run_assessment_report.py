"""``run_assessment.py --all`` renders the deliverables after synthesis.

The CLI (``--llm-mode none``) and Bedrock (``--llm-mode bedrock``) runs used to
stop at ``report.json``; only ``/modernize`` (via ``run_report.py``) rendered the
PDF and the rest. ``phase_report`` runs the same render step. In ``external``
mode synthesis is not final yet (Claude Code still writes the summary and runs
``--finalize``), so the render waits for ``/synthesize``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

FIXTURE = Path(__file__).resolve().parents[1] / "report" / "fixtures" / "e2e09_report.json"
DB, JOB = "discourse", "job-x"


@pytest.fixture
def seeded(tmp_path: Path) -> Path:
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(f"{DB}/{JOB}/synthesis/v2/report.json", json.loads(FIXTURE.read_text()))
    return tmp_path


@pytest.mark.parametrize("llm_mode", ["none", "bedrock"])
def test_phase_report_renders_the_deliverables_folder(seeded: Path, llm_mode, capsys) -> None:
    from scripts import run_assessment

    status = run_assessment.phase_report(str(seeded), JOB, DB, llm_mode)

    assert status["status"] == "complete", status
    folder = seeded / DB / JOB / "deliverables"
    assert (folder / "executive-report.pdf").exists()
    assert (folder / "index.md").exists()
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert line["phase"] == "report"
    assert Path(line["deliverables_dir"]).resolve() == folder.resolve()


def test_phase_report_waits_for_synthesize_in_external_mode(seeded: Path, capsys) -> None:
    from scripts import run_assessment

    status = run_assessment.phase_report(str(seeded), JOB, DB, "external")

    assert status["status"] == "skipped"
    assert "run_report.py" in status["reason"]
    assert not (seeded / DB / JOB / "deliverables").exists()


def test_phase_report_error_is_reported(tmp_path: Path) -> None:
    from scripts import run_assessment

    with pytest.raises(SystemExit):
        run_assessment.phase_report(str(tmp_path), JOB, DB, "none")
