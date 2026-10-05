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


@pytest.mark.deterministic
def test_dynamodb_check_costs_on_a_real_split_group(run: PipelineResult, tmp_path) -> None:
    """External-mode DynamoDB group drafts can pass the skill's cost check (#198).

    Splits a copy of the deterministic job, writes a minimal contract-valid
    draft for a real group, and runs ``--check-costs`` on it. Then ``--finalize``
    must refuse cleanly (no merged output yet) instead of crashing (#197).
    """
    import shutil
    import subprocess  # nosec B404 -- runs this repo's own scripts/run_schema_design.py with fixed argv
    import sys

    from src.contracts.dynamodb_model_output import DynamoDBModelOutputContract
    from tests.e2e.pipeline import REPO, _env

    root = tmp_path / "artifacts"
    shutil.copytree(run.job_dir(), root / run.db / run.job_id)
    common = ["--job-id", run.job_id, "--db", run.db, "--engine", "dynamodb"]
    common += ["--artifact-root", str(root)]

    def script(*extra: str) -> tuple[int, dict]:
        proc = subprocess.run(  # nosec B603 -- fixed interpreter plus this repo's own script args
            [sys.executable, "scripts/run_schema_design.py", *common, *extra],
            cwd=REPO,
            capture_output=True,
            text=True,
            env=_env(),
            timeout=300,
        )
        return proc.returncode, json.loads(proc.stdout.strip().splitlines()[-1])

    code, split = script("--split")
    assert code == 0 and split["status"] == "split", split
    manifest_path = root / split["manifest"]
    group_dir = manifest_path.parent
    group = json.loads(manifest_path.read_text())["groups"][0]
    group_input = json.loads((group_dir / group["input_file"]).read_text())
    query_id = group_input["collector_output"]["queries"]["query_patterns"][0]["query_id"]
    source_table = group["primary_tables"][0]

    draft = {
        "job_id": run.job_id,
        "source_database": run.db,
        "access_patterns": [
            {
                "pattern_id": "DDB-AP-1",
                "pattern_group": "Reads",
                "query_ids": [query_id],
                "source_tables": [source_table],
                "description": "Get item by key",
                "operation": "GetItem",
                "table_name": "Main",
                "key_condition": "PK=id",
                "design_rps": 5.0,
                "item_size_bytes": 200,
            }
        ],
        "table_definitions": [
            {
                "table_name": "Main",
                "aggregate_pattern": "separate",
                "source_tables": [source_table],
                "partition_key": {"attribute_name": "id", "attribute_type": "S"},
                "attributes": [
                    {"name": "id", "type": "S", "source_table": source_table, "source_column": "id"}
                ],
                "item_count": 100,
                "item_size_bytes": 200,
            }
        ],
        "hot_partition_analysis": [
            {
                "table_name": "Main",
                "operation": "read",
                "rcu_or_wcu_per_second": 5.0,
                "partition_limit": 3000,
                "utilization_pct": 0.17,
                "at_risk": False,
                "contributing_patterns": [query_id],
            }
        ],
        "trade_offs": [
            {
                "description": "Single-table key lookup",
                "impact": "Lookups by key only",
                "source_tables": [source_table],
                "target_tables": ["Main"],
                "query_ids": [query_id],
                "engine": "dynamodb",
            }
        ],
        "validation_passed": False,
    }
    DynamoDBModelOutputContract.model_validate(draft)
    draft_path = group_dir / f"schema_draft_group_{group['group_index']}.json"
    draft_path.write_text(json.dumps(draft))

    code, check = script("--check-costs", str(draft_path))
    assert code == 0, check
    assert check["status"] == "complete"
    assert check["passed"] is True, check["errors"]
    assert check["per_table"][0]["table_name"] == "Main"

    code, finalize = script("--finalize")
    assert code == 1
    assert finalize["status"] == "error"
    assert "--merge" in finalize["message"]
