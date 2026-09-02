"""Unit tests for the consolidated assessment-core runner (ADR-025, ADR-026).

``run_assessment_core`` runs the whole assessment front-half in one process:
Collect -> Triage -> Analyze (every selected engine) -> Assign -> Reality Check,
by composing the unchanged per-phase cores. It replaced the four separate
collector / triage / analysis / assignment subagents (ADR-025) and now also runs
Reality Check (ADR-026). These tests drive it end-to-end against the bundled
WordPress sample and assert:

  1. every phase writes the SAME artifact key it wrote as a separate agent, so
     Schema Design / Synthesis are unaffected,
  2. the top-level phase callbacks fire for collector, triage, analysis,
     assignment, and reality_check (what the WebApp plan steps key on),
  3. the nested per-engine analysis callbacks fire (the Analysis box sub-steps),
  4. the returned ``summary_for_chat`` carries the narration fields, and
     ``effective_assignment_version`` is resolved.

Reality Check runs with ``REALITY_CHECK_LLM_MODE=none`` here so the suite stays
deterministic and needs no Bedrock. Stays SDK-free (imports only ``core`` + the
local store), so it runs both locally and in CI where ``agent_builder_sdk`` is
absent.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src.atx_orchestrator.core import (
    _ANALYSIS_ENGINES,
    _resolve_assignment_version,
    default_input_key,
    run_assessment_core,
)
from src.atx_orchestrator.runtime.store import upgrade_store
from src.storage.local_store import LocalArtifactStore

SAMPLE_ZIP = Path(__file__).resolve().parents[3] / "docs/examples/wordpress/wordpress.zip"
SAMPLE_MEMBER = "wordpress-collection.json"


@pytest.fixture(autouse=True)
def _deterministic_reality_check(monkeypatch):
    """Run Reality Check without Bedrock so the suite is deterministic + offline."""
    monkeypatch.setenv("REALITY_CHECK_LLM_MODE", "none")


def _load_sample() -> dict:
    with zipfile.ZipFile(SAMPLE_ZIP) as zf:
        content = zf.read(SAMPLE_MEMBER).decode("utf-8").strip()
    if not content.startswith("{") and "\n" in content:
        content = content[content.index("\n") + 1 :]
    data = json.loads(content)
    assert isinstance(data, dict)
    return data


@pytest.fixture
def seeded_store(tmp_path):
    """A store seeded with the raw offline collection at the collector seed key."""
    store = upgrade_store(LocalArtifactStore(base_dir=str(tmp_path)))
    job, db = "job-assess", "wordpress"
    store.write_json(default_input_key(job, db), _load_sample())
    return store, job, db


def test_runs_all_phases_and_writes_same_artifacts(seeded_store) -> None:
    store, job, db = seeded_store
    phase_starts: list[str] = []
    phase_dones: list[str] = []
    engine_dones: list[str] = []

    result = run_assessment_core(
        job,
        db,
        store=store,
        on_phase_start=lambda p: phase_starts.append(p),
        on_phase_done=lambda p, s, d: phase_dones.append(p),
        on_engine_done=lambda e, p, s: engine_dones.append(e),
    )

    # 1. Each phase wrote the same artifact key it wrote as a separate agent,
    #    including Reality Check's output.
    assert store.exists(f"{db}/{job}/collector/output.json")
    assert store.exists(f"{db}/{job}/referee-triage/triage.json")
    assert store.exists(f"{db}/{job}/assignment/v1/assignment.json")
    assert store.exists(f"{db}/{job}/reality-check/output.json")
    analyzed = result["analysis"]["engines_analyzed"]
    assert analyzed, "triage should select at least one engine for the sample"
    for engine in analyzed:
        target_db = _ANALYSIS_ENGINES[engine].target_database
        assert store.exists(f"{db}/{job}/analysis-{target_db}/analysis.json")

    # 2. Top-level phase callbacks fire for all five plan steps.
    for phase in ("collector", "triage", "analysis", "assignment", "reality_check"):
        assert phase in phase_starts, f"{phase} start not reported"
        assert phase in phase_dones, f"{phase} done not reported"

    # 3. Nested per-engine analysis callbacks fire for the analyzed engines.
    assert set(engine_dones) == set(analyzed)

    # 4. Effective version is resolved and matches the latest assignment on the
    #    store (2 if Reality Check consolidated, else 1).
    latest = _resolve_assignment_version(store, job, db)
    assert result["effective_assignment_version"] == latest
    assert latest >= 1


def test_summary_for_chat_carries_narration_fields(seeded_store) -> None:
    store, job, db = seeded_store

    result = run_assessment_core(job, db, store=store)

    chat = result["summary_for_chat"]
    # WordPress is MySQL: triage detects signals and selects engines.
    assert isinstance(chat["signals"], list) and chat["signals"]
    assert chat["selected_engines"]
    assert chat["engines_analyzed"] == result["analysis"]["engines_analyzed"]
    assert isinstance(chat["queries_per_engine"], dict)
    assert chat["tables"] and chat["queries"]
    # Reality Check narration fields are present.
    assert "reality_check_consolidations" in chat
    assert "after_distribution" in chat


def test_missing_input_raises(tmp_path) -> None:
    """With neither an explicit input_key nor a seed, the collect phase raises."""
    store = upgrade_store(LocalArtifactStore(base_dir=str(tmp_path)))
    with pytest.raises(FileNotFoundError):
        run_assessment_core("j", "wordpress", store=store)


def test_resolve_assignment_version(tmp_path) -> None:
    """The version resolver returns the highest vN present, or 0 when none."""
    store = upgrade_store(LocalArtifactStore(base_dir=str(tmp_path)))
    job, db = "j", "d"
    assert _resolve_assignment_version(store, job, db) == 0
    store.write_json(f"{db}/{job}/assignment/v1/assignment.json", {"version": 1})
    assert _resolve_assignment_version(store, job, db) == 1
    store.write_json(f"{db}/{job}/assignment/v2/assignment.json", {"version": 2})
    assert _resolve_assignment_version(store, job, db) == 2
