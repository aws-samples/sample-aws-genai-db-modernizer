"""``run_assessment.py --llm-mode none --all`` never calls a model (#281).

``LocalOrchestrator._run_schema_design`` called ``run_schema_design_auto``
without the LLM mode, so schema design fell back to its Bedrock default and the
designer agents invoked the model on a run the user asked to be deterministic.
Synthesis had the same gap (``run_synthesis`` defaults to ``bedrock``).

These tests run the whole pipeline on the WordPress sample with every model
entry point patched to raise and count, and assert:

* zero model calls in any phase;
* schema design is skipped with a clear message (no designer runs, no
  ``schema_output.json`` is written -- same shape as the deterministic e2e
  pipeline, which stops before schema design), and the run still completes
  through synthesis.
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
WORDPRESS_ZIP = REPO / "docs" / "examples" / "wordpress" / "wordpress.zip"


class _ModelCalled(AssertionError):
    pass


@pytest.fixture
def model_calls(monkeypatch):
    """Patch every model entry point to record the call and raise."""
    import botocore.client
    import strands.agent.agent
    import strands.models.bedrock

    calls: list[str] = []

    def _forbid(name):
        def _raise(*args, **kwargs):
            calls.append(name)
            raise _ModelCalled(f"model entry point called in llm_mode=none: {name}")

        return _raise

    monkeypatch.setattr(strands.agent.agent.Agent, "__init__", _forbid("strands.Agent"))
    monkeypatch.setattr(
        strands.models.bedrock.BedrockModel, "__init__", _forbid("strands BedrockModel")
    )
    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", _forbid("botocore API call"))
    # No real credentials may leak into the run either.
    for var in (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "AWS_PROFILE",
        "AWS_DEFAULT_PROFILE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    return calls


def _wordpress_collection(tmp_path: Path) -> Path:
    with zipfile.ZipFile(WORDPRESS_ZIP) as z:
        z.extractall(tmp_path / "input")
    return tmp_path / "input" / "wordpress-collection.json"


def test_run_assessment_all_with_llm_mode_none_makes_no_model_calls(
    monkeypatch, tmp_path, capsys, model_calls
):
    from scripts import run_assessment

    collection = _wordpress_collection(tmp_path)
    artifacts = tmp_path / "artifacts"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_assessment.py",
            "--file",
            str(collection),
            "--db",
            "wordpress",
            "--llm-mode",
            "none",
            "--all",
            "-y",
            "--artifact-root",
            str(artifacts),
        ],
    )

    run_assessment.main()

    assert model_calls == []

    state = json.loads((tmp_path / run_assessment.STATE_FILE).read_text())
    assert state["current_phase"] == "done"
    assert state["phase_status"]["schema_design"] == "complete"
    assert state["phase_status"]["synthesis"] == "complete"

    job_dir = artifacts / "wordpress" / state["job_id"]
    # Schema design was skipped, not half-run: no designer output, no group split.
    assert list(job_dir.glob("schema-*/**/*.json")) == []
    out = capsys.readouterr()
    assert "schema design skipped (llm_mode=none)" in (out.out + out.err)
    # Synthesis still produced its deterministic report.
    assert list(job_dir.glob("synthesis/v*/report.json"))


@pytest.fixture
def prepared_job(tmp_path, monkeypatch, model_calls):
    """WordPress job run through reality check with the deterministic scripts."""
    from scripts import run_assessment
    from src.storage.local_store import LocalArtifactStore

    monkeypatch.chdir(tmp_path)
    store = LocalArtifactStore(base_dir=str(tmp_path / "artifacts"))
    job_id, db = run_assessment.phase_collect(
        str(_wordpress_collection(tmp_path)), "wordpress", store
    )
    engines = run_assessment.phase_triage(store, job_id, db)
    run_assessment.phase_analysis(store, job_id, db, engines, llm_mode="none")
    run_assessment.phase_assignment(store, job_id, db)
    assert run_assessment.phase_reality_check(store, job_id, db, "none") == "complete"
    return store, job_id, db


def test_orchestrator_passes_llm_mode_none_to_schema_design_and_synthesis(
    prepared_job, model_calls, capsys
):
    from src.contracts.phase_models import Phase, PhaseStatus
    from src.orchestrator.local_orchestrator import LocalOrchestrator

    store, job_id, db = prepared_job
    orch = LocalOrchestrator(store=store, llm_mode="none")
    progression = orch.get_progression(job_id)
    for phase in (
        Phase.COLLECT_TRIAGE,
        Phase.ANALYSIS,
        Phase.ASSIGNMENT,
        Phase.REALITY_CHECK,
        Phase.ASSIGNMENT_REVIEW,
    ):
        orch._set_phase_status(progression, phase, PhaseStatus.COMPLETED)
    orch._save_progression(progression)

    orch.resume(job_id, Phase.SCHEMA_DESIGN)
    orch._run_post_schema_routing(job_id, db)
    orch.confirm_schema_design(job_id)
    orch.resume(job_id, Phase.SYNTHESIS)

    assert model_calls == []
    phases = orch.get_progression(job_id).phases
    assert phases[Phase.SCHEMA_DESIGN].status == PhaseStatus.COMPLETED
    assert phases[Phase.SYNTHESIS].status == PhaseStatus.COMPLETED
    assert "schema design skipped (llm_mode=none)" in capsys.readouterr().out


@pytest.mark.parametrize("llm_mode", ["none", "bedrock", "external"])
@pytest.mark.parametrize(
    "phase",
    ["ANALYSIS", "REALITY_CHECK", "SCHEMA_DESIGN", "SYNTHESIS"],
)
def test_orchestrator_forwards_its_llm_mode_to_every_llm_capable_phase(
    phase, llm_mode, tmp_path, monkeypatch
):
    """Each phase handler that accepts an LLM mode receives the orchestrator's.

    Collect, triage, assignment and load test take no LLM mode: their handlers
    are deterministic and never build a model client.
    """
    from src.contracts.phase_models import Phase
    from src.orchestrator.local_orchestrator import LocalOrchestrator
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=str(tmp_path))
    orch = LocalOrchestrator(store=store, llm_mode=llm_mode)
    seen: list[str] = []

    def _record(*args, **kwargs):
        seen.append(kwargs.get("llm_mode", "<default>"))

    monkeypatch.setattr(orch, "_get_selected_engines", lambda j, d: ["dynamodb"])
    monkeypatch.setattr(orch, "_get_assignment_version", lambda j, d: 1)
    monkeypatch.setattr(orch, "_get_engines_with_in_scope_queries", lambda j, d, v: {"dynamodb"})
    targets = {
        "ANALYSIS": "src.agents.analysis.handler.run_analysis",
        "REALITY_CHECK": "src.agents.referee.reality_check_handler.run_reality_check_handler",
        "SCHEMA_DESIGN": "src.agents.schema_design.handler.run_schema_design_auto",
        "SYNTHESIS": "src.agents.referee.synthesis_handler.run_synthesis",
    }
    monkeypatch.setattr(targets[phase], _record)

    orch._run_phase("job-1", Phase[phase], config={"database_name": "wordpress"})

    assert seen == [llm_mode]


def test_run_schema_design_with_injected_none_mode_skips_the_designer(tmp_path, model_calls):
    from src.agents.schema_design.handler import run_schema_design_with_injected
    from src.storage.local_store import LocalArtifactStore

    store = LocalArtifactStore(base_dir=str(tmp_path))
    run_schema_design_with_injected(
        "job-1", "wordpress", "dynamodb", store, {"q1"}, assignment_version=1, llm_mode="none"
    )
    assert model_calls == []
    assert list(tmp_path.rglob("*.json")) == []
