"""No collected literal value reaches any file a run writes (#1625).

A SQL Server, an Oracle and a Db2 collection seeded with personal values in their
literals (``tests/fixtures/pii_collections.py``) run through the deterministic
pipeline, and through the external-mode steps that write the model inputs (the
reality check's ``llm_input.json``, schema design's requests, synthesis'
``llm_input.json``). No file under either job, the deliverables included (the PDF's
text too), holds any of the seeded values.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.e2e.pipeline import PipelineResult, _run, run_pipeline_from_file
from tests.fixtures.pii_collections import ENGINES, leaked, pii_collection

pytestmark = [pytest.mark.e2e, pytest.mark.deterministic]


def _text(path: Path) -> str:
    if path.suffix == ".pdf":
        from pypdf import PdfReader

        return "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    return path.read_bytes().decode("utf-8", errors="ignore")


def _leaks(job_dir: Path) -> dict[str, list[str]]:
    return {
        str(path.relative_to(job_dir)): found
        for path in sorted(job_dir.rglob("*"))
        if path.is_file() and (found := leaked(_text(path)))
    }


def _model_inputs_job(src: Path, db: str, root: Path) -> PipelineResult:
    """The steps up to the placement, then the model-facing steps in external mode."""
    result = PipelineResult(db=db, job_id="ext1", artifact_root=root)
    common = ["--job-id", "ext1", "--db", db, "--artifact-root", str(root)]
    _run("collect", ["scripts/run_collect.py", "--file", str(src), *common], result)
    triage = _run("triage", ["scripts/run_triage.py", *common], result)
    for engine in triage["selected"]:
        _run(
            f"analysis-{engine}",
            ["scripts/run_analysis.py", *common, "--engine", engine, "--llm-mode", "none"],
            result,
        )
    _run("assignment", ["scripts/run_assignment.py", *common], result)
    _run(
        "reality-check", ["scripts/run_reality_check.py", *common, "--llm-mode", "external"], result
    )
    assignment = json.loads(
        (result.job_dir() / "assignment" / "v1" / "assignment.json").read_text()
    )
    placed = {q["assigned_engine"] for q in assignment["query_assignments"]}
    for engine in sorted(placed & set(triage["selected"])):
        _run(
            f"schema-{engine}",
            ["scripts/run_schema_design.py", *common, "--engine", engine, "--llm-mode", "external"],
            result,
        )
    return result


@pytest.fixture(scope="module", params=ENGINES)
def runs(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> tuple[PipelineResult, PipelineResult]:
    engine = request.param
    root = tmp_path_factory.mktemp(f"redaction-{engine}")
    src = root / "collection.json"
    src.write_text(json.dumps(pii_collection(engine)))
    db = f"pii{engine}"
    full = run_pipeline_from_file(src, db, root / "artifacts", "job1")
    common = ["--job-id", "job1", "--db", db, "--artifact-root", str(root / "artifacts")]
    _run(
        "synthesis-external", ["scripts/run_synthesis.py", *common, "--llm-mode", "external"], full
    )
    return full, _model_inputs_job(src, db, root / "artifacts")


def test_the_deterministic_run_writes_no_seeded_value(
    runs: tuple[PipelineResult, PipelineResult],
) -> None:
    full, _ = runs
    job = full.job_dir()
    for name in (
        "collector/output.json",
        "collector/sql_shadow.json",
        "uploads/collector-output.json",
    ):
        assert (job / name).is_file(), name
    assert full.report_json().is_file()
    assert full.deliverable("engineering-report.md").is_file()
    assert full.deliverable("executive-report.pdf").is_file()
    assert list(job.glob("synthesis/v*/llm_input.json"))
    assert _leaks(job) == {}


def test_the_model_inputs_hold_no_seeded_value(
    runs: tuple[PipelineResult, PipelineResult],
) -> None:
    _, ext = runs
    job = ext.job_dir()
    assert (job / "reality-check" / "llm_input.json").is_file()
    assert list(job.glob("llm_requests/schema_design_*.json"))
    assert _leaks(job) == {}
