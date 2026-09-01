"""Unit tests for the consolidated deterministic-core runner (ADR-025).

``run_deterministic_core`` runs the whole deterministic front-half in one
process: Collect -> Triage -> Analyze (every selected engine) -> Assign, by
composing the unchanged per-phase cores. It replaces the four separate
collector / triage / analysis / assignment subagents. These tests drive it
end-to-end against the bundled WordPress sample (deterministic, no LLM, no AWS)
and assert:

  1. every phase writes the SAME artifact key it wrote as a separate agent, so
     Schema Design / Synthesis are unaffected,
  2. the top-level phase callbacks fire for collector, triage, analysis and
     assignment (what the WebApp plan steps key on),
  3. the nested per-engine analysis callbacks fire (the Analysis box sub-steps),
  4. the returned ``summary_for_chat`` carries the signals, selected engines and
     query distribution the orchestrator narrates.

Stays SDK-free (imports only ``core`` + the local store), so it runs both
locally and in CI where ``agent_builder_sdk`` is absent.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src.atx_orchestrator.core import _ANALYSIS_ENGINES, default_input_key, run_deterministic_core
from src.atx_orchestrator.runtime.store import upgrade_store
from src.storage.local_store import LocalArtifactStore

SAMPLE_ZIP = Path(__file__).resolve().parents[3] / "docs/examples/wordpress/wordpress.zip"
SAMPLE_MEMBER = "wordpress-collection.json"


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
    job, db = "job-det", "wordpress"
    store.write_json(default_input_key(job, db), _load_sample())
    return store, job, db


def test_runs_all_phases_and_writes_same_artifacts(seeded_store) -> None:
    store, job, db = seeded_store
    phase_starts: list[str] = []
    phase_dones: list[str] = []
    engine_dones: list[str] = []

    result = run_deterministic_core(
        job,
        db,
        store=store,
        on_phase_start=lambda p: phase_starts.append(p),
        on_phase_done=lambda p, s, d: phase_dones.append(p),
        on_engine_done=lambda e, p, s: engine_dones.append(e),
    )

    # 1. Each phase wrote the same artifact key it wrote as a separate agent.
    assert store.exists(f"{db}/{job}/collector/output.json")
    assert store.exists(f"{db}/{job}/referee-triage/triage.json")
    assert store.exists(f"{db}/{job}/assignment/v1/assignment.json")
    analyzed = result["analysis"]["engines_analyzed"]
    assert analyzed, "triage should select at least one engine for the sample"
    for engine in analyzed:
        target_db = _ANALYSIS_ENGINES[engine].target_database
        assert store.exists(f"{db}/{job}/analysis-{target_db}/analysis.json")

    # 2. Top-level phase callbacks fire for all four plan steps.
    for phase in ("collector", "triage", "analysis", "assignment"):
        assert phase in phase_starts, f"{phase} start not reported"
        assert phase in phase_dones, f"{phase} done not reported"

    # 3. Nested per-engine analysis callbacks fire for the analyzed engines.
    assert set(engine_dones) == set(analyzed)


def test_summary_for_chat_carries_narration_fields(seeded_store) -> None:
    store, job, db = seeded_store

    result = run_deterministic_core(job, db, store=store)

    chat = result["summary_for_chat"]
    # WordPress is MySQL: triage detects signals and selects engines.
    assert isinstance(chat["signals"], list) and chat["signals"]
    assert chat["selected_engines"]
    assert chat["engines_analyzed"] == result["analysis"]["engines_analyzed"]
    # Assignment routed queries across engines.
    assert isinstance(chat["queries_per_engine"], dict)
    assert chat["tables"] and chat["queries"]


def test_missing_input_raises(tmp_path) -> None:
    """With neither an explicit input_key nor a seed, the collect phase raises."""
    store = upgrade_store(LocalArtifactStore(base_dir=str(tmp_path)))
    with pytest.raises(FileNotFoundError):
        run_deterministic_core("j", "wordpress", store=store)
