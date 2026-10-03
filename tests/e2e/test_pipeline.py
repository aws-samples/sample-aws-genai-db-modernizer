"""Every phase completes and the deliverables are all produced."""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from tests.e2e.pipeline import PipelineResult

EXPECTED_ENGINES = {
    "wordpress": {"dynamodb", "elasticache", "documentdb", "opensearch", "aurora_mysql"},
    "discourse": {"dynamodb", "elasticache", "documentdb", "opensearch", "aurora_postgresql"},
}


@pytest.mark.deterministic
def test_every_phase_completes(run: PipelineResult) -> None:
    bad = {k: v for k, v in run.steps.items() if v.get("status") != "complete"}
    assert bad == {}


@pytest.mark.deterministic
def test_triage_selects_the_expected_engines(run: PipelineResult) -> None:
    assert set(run.steps["triage"]["selected"]) == EXPECTED_ENGINES[run.db]


@pytest.mark.deterministic
def test_report_journeys_match_collected_queries(run: PipelineResult) -> None:
    assert run.report["journeys"] == run.steps["collect"]["queries"]


def test_report_has_all_deliverables(run: PipelineResult) -> None:
    r = run.report
    assert r["errors"] == [] and r["warnings"] == []
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
    """Validate whichever artifacts the job actually has on disk, read from files only
    (not ``run.steps``, which only the deterministic pipeline populates for every
    phase) -- so this works for a job produced elsewhere too, e.g. a headless Claude
    run wrapped by ``from_existing_job``."""
    from src.contracts.analysis_output import AnalysisOutputContract
    from src.contracts.assignment_models import Assignment
    from src.contracts.aurora_mysql_model_output import AuroraMySQLModelOutputContract
    from src.contracts.aurora_postgresql_model_output import (
        AuroraPostgresqlModelOutputContract,
    )
    from src.contracts.collector_output import CollectorOutputContract
    from src.contracts.documentdb_model_output import DocumentDBModelOutputContract
    from src.contracts.dynamodb_model_output import DynamoDBModelOutputContract
    from src.contracts.elasticache_model_output import ElastiCacheModelOutputContract
    from src.contracts.opensearch_model_output import OpenSearchModelOutputContract
    from src.contracts.reality_check_output import RealityCheckOutputContract
    from src.contracts.synthesis_output import SynthesisOutputContract
    from src.contracts.triage_output import TriageOutputContract

    # engine -> its schema design output contract (schema-<engine>/v<N>/schema_output.json)
    schema_contracts: dict[str, type[BaseModel]] = {
        "dynamodb": DynamoDBModelOutputContract,
        "documentdb": DocumentDBModelOutputContract,
        "opensearch": OpenSearchModelOutputContract,
        "elasticache": ElastiCacheModelOutputContract,
        "aurora_mysql": AuroraMySQLModelOutputContract,
        "aurora_postgresql": AuroraPostgresqlModelOutputContract,
    }

    def _read(path):
        return json.loads(path.read_text())

    job_dir = run.job_dir()

    CollectorOutputContract.model_validate(_read(job_dir / "collector" / "output.json"))

    triage = _read(job_dir / "referee-triage" / "triage.json")
    TriageOutputContract.model_validate(triage)

    # Every phase below is required, not "validate if present": a headless run
    # that silently skipped a phase must fail here, not pass vacuously.
    for agent in triage["selected_agents"]:
        engine = agent["agent_type"]
        analysis_path = job_dir / f"analysis-{engine}" / "analysis.json"
        assert analysis_path.is_file(), f"triage selected {engine} but {analysis_path} is missing"
        AnalysisOutputContract.model_validate(_read(analysis_path))

    assert (job_dir / "assignment" / "v1" / "assignment.json").is_file(), "assignment/v1 missing"
    for assignment_path in sorted(job_dir.glob("assignment/v*/assignment.json")):
        Assignment.model_validate(_read(assignment_path))

    reality_check_path = job_dir / "reality-check" / "output.json"
    assert reality_check_path.is_file(), f"{reality_check_path} missing"
    RealityCheckOutputContract.model_validate(_read(reality_check_path))

    # Schema design only runs with an LLM (the deterministic pipeline stops
    # before it), so it is required only for a job produced outside the
    # deterministic runner (external mode: E2E_ARTIFACT_ROOT/E2E_DB/E2E_JOB).
    if run.external:
        from src.storage.assignment_versioning import (
            engines_with_in_scope_queries,
            resolve_downstream_assignment_version,
        )
        from src.storage.local_store import LocalArtifactStore

        store = LocalArtifactStore(base_dir=str(run.artifact_root))
        version = resolve_downstream_assignment_version(store, run.db, run.job_id)
        in_scope = engines_with_in_scope_queries(store, run.db, run.job_id, version)
        surviving = [a["agent_type"] for a in triage["selected_agents"]]
        surviving = [e for e in surviving if e in in_scope] if in_scope else surviving
        for engine in surviving:
            outputs = list(job_dir.glob(f"schema-{engine}/v*/schema_output.json"))
            assert outputs, (
                f"{engine} survived reality check (assignment v{version}) but has no "
                f"schema-{engine}/v*/schema_output.json"
            )

    for schema_output in sorted(job_dir.glob("schema-*/v*/schema_output.json")):
        engine = schema_output.parent.parent.name.removeprefix("schema-")
        contract = schema_contracts.get(engine)
        if contract is not None:
            contract.model_validate(_read(schema_output))

    # The report step always runs (both the deterministic pipeline and
    # from_existing_job run scripts/run_report.py), so run.report is safe here.
    SynthesisOutputContract.model_validate(_read(run.artifact_root / run.report["report"]))

    # In external mode (a real headless run), a successful job must have produced
    # journeys and a clean report -- these are not pipeline-shape assumptions, they
    # are the bar any real run has to clear.
    assert run.report["warnings"] == []
    assert run.report["errors"] == []
