"""Every phase completes and the deliverables are all produced."""

from __future__ import annotations

import json

from tests.e2e.pipeline import PipelineResult

EXPECTED_ENGINES = {
    "wordpress": {"dynamodb", "elasticache", "documentdb", "opensearch", "aurora_mysql"},
    "discourse": {"dynamodb", "elasticache", "documentdb", "opensearch", "aurora_postgresql"},
}


def test_every_phase_completes(run: PipelineResult) -> None:
    bad = {k: v for k, v in run.steps.items() if v.get("status") != "complete"}
    assert bad == {}


def test_triage_selects_the_expected_engines(run: PipelineResult) -> None:
    assert set(run.steps["triage"]["selected"]) == EXPECTED_ENGINES[run.db]


def test_report_has_all_deliverables_and_journeys(run: PipelineResult) -> None:
    r = run.report
    assert r["errors"] == [] and r["warnings"] == []
    assert r["journeys"] == run.steps["collect"]["queries"]
    names = sorted(p.rsplit("/", 1)[-1] for p in r["files"])
    assert "summary-executive-report.pdf" in names
    assert "summary-executive-report.pptx" in names
    for kind, ext in (
        ("decision-report", ".html"),
        ("analysis-report", ".html"),
        ("engineering-report", ".md"),
    ):
        assert any(f"_{kind}_" in n and n.endswith(ext) for n in names), (kind, names)


def test_contract_artifacts_validate(run: PipelineResult) -> None:
    from src.contracts.analysis_output import AnalysisOutputContract
    from src.contracts.assignment_models import Assignment
    from src.contracts.collector_output import CollectorOutputContract
    from src.contracts.reality_check_output import RealityCheckOutputContract
    from src.contracts.synthesis_output import SynthesisOutputContract
    from src.contracts.triage_output import TriageOutputContract

    def _read(path):
        return json.loads(path.read_text())

    job_dir = run.job_dir()

    CollectorOutputContract.model_validate(_read(job_dir / "collector" / "output.json"))
    TriageOutputContract.model_validate(_read(job_dir / "referee-triage" / "triage.json"))
    for engine in run.steps["triage"]["selected"]:
        AnalysisOutputContract.model_validate(
            _read(job_dir / f"analysis-{engine}" / "analysis.json")
        )
    Assignment.model_validate(_read(job_dir / "assignment" / "v1" / "assignment.json"))
    RealityCheckOutputContract.model_validate(_read(job_dir / "reality-check" / "output.json"))
    SynthesisOutputContract.model_validate(_read(run.artifact_root / run.report["report"]))
