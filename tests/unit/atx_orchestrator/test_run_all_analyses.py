"""Unit tests for the consolidated analysis runner (ADR-024).

``run_all_analyses`` replaces the six per-engine analysis subagents: it runs
every triage-selected engine in one process via the unchanged
``run_analysis_core``, writing the same ``analysis-<target_database>/analysis.json``
artifacts the Assign phase reads. These tests drive it end-to-end against the
bundled WordPress sample (deterministic, ~50ms, no LLM, no AWS) and assert:

  1. every selected engine's artifact lands under the same key as before,
  2. the per-engine progress callbacks fire once each with the
     ``analysis_<target_database>`` label (what the WebApp sub-steps key on),
  3. an explicit engine list overrides triage and unknown tokens are skipped,
  4. a missing triage output raises when engines are not supplied.

These stay SDK-free (import only ``core`` + the local store), so they run both
locally and in CI where ``agent_builder_sdk`` is absent.
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src.agents.referee.triage_handler import run_triage
from src.atx_orchestrator.core import _ANALYSIS_ENGINES, ingest_offline_collection, run_all_analyses
from src.atx_orchestrator.runtime.store import upgrade_store
from src.storage.local_store import LocalArtifactStore

# Only the .zip is committed (the README has you unzip it), so read the sample
# straight from the archive — the unzipped .json is not present in a fresh checkout.
SAMPLE_ZIP = Path(__file__).resolve().parents[3] / "docs/examples/wordpress/wordpress.zip"
SAMPLE_MEMBER = "wordpress-collection.json"


def _load_sample() -> dict:
    """Load the offline-collection sample from the committed zip, stripping its
    ``collection_output`` header line (a header line followed by the JSON body)."""
    with zipfile.ZipFile(SAMPLE_ZIP) as zf:
        content = zf.read(SAMPLE_MEMBER).decode("utf-8").strip()
    if not content.startswith("{") and "\n" in content:
        content = content[content.index("\n") + 1 :]
    data = json.loads(content)
    assert isinstance(data, dict)
    return data


@pytest.fixture
def prepared_store(tmp_path):
    """A store seeded with collector + triage output for the WordPress sample."""
    # Wrap the local store the same way the ATX runtime does (make_store ->
    # upgrade_store), so it has the write_text capability run_analysis_core uses
    # for ER diagrams.
    store = upgrade_store(LocalArtifactStore(base_dir=str(tmp_path)))
    raw = _load_sample()
    job, db = "job-test", "wordpress"
    ingest_offline_collection(store, job, db, raw)
    run_triage(job, db, store)
    return store, job, db


def test_runs_all_selected_engines_and_writes_same_artifacts(prepared_store) -> None:
    store, job, db = prepared_store
    starts: list[tuple[str, str]] = []
    dones: list[tuple[str, str]] = []
    errors: list[tuple[str, str, str]] = []

    summary = run_all_analyses(
        job,
        db,
        store=store,
        on_engine_start=lambda e, p: starts.append((e, p)),
        on_engine_done=lambda e, p, s: dones.append((e, p)),
        on_engine_error=lambda e, p, r: errors.append((e, p, r)),
    )

    analyzed = summary["engines_analyzed"]
    assert analyzed, "triage should select at least one engine for the sample"
    assert summary["engines_failed"] == []
    assert not errors

    # Each engine writes the SAME per-engine artifact key the six agents used,
    # so the Assign phase is unaffected.
    for engine in analyzed:
        target_db = _ANALYSIS_ENGINES[engine].target_database
        assert store.exists(f"{db}/{job}/analysis-{target_db}/analysis.json")

    # Progress callbacks fire once per engine, with the analysis_<target_database>
    # label the WebApp sub-steps key on.
    assert [e for e, _ in starts] == analyzed
    assert [e for e, _ in dones] == analyzed
    for engine, phase in dones:
        assert phase == f"analysis_{_ANALYSIS_ENGINES[engine].target_database}"


def test_explicit_engines_override_triage_and_skip_unknown(prepared_store) -> None:
    store, job, db = prepared_store

    summary = run_all_analyses(job, db, engines=["dynamodb", "bogus-engine"], store=store)

    assert summary["engines_analyzed"] == ["dynamodb"]
    assert "bogus-engine" not in summary["engines_analyzed"]
    assert store.exists(f"{db}/{job}/analysis-dynamodb/analysis.json")


def test_missing_triage_raises_when_engines_not_supplied(tmp_path) -> None:
    store = upgrade_store(LocalArtifactStore(base_dir=str(tmp_path)))
    raw = _load_sample()
    # Collector output only — no triage.
    ingest_offline_collection(store, "j", "wordpress", raw)

    with pytest.raises(FileNotFoundError):
        run_all_analyses("j", "wordpress", store=store)


def test_resolves_aurora_postgresql_token_to_engine_key(prepared_store) -> None:
    """Regression: triage emits the ``target_database`` token ("aurora_postgresql"),
    but ``_ANALYSIS_ENGINES`` is keyed "aurora_pg". The token must resolve to the
    engine key and run, not be dropped as unknown (which silently skipped Aurora
    PostgreSQL analysis for PostgreSQL sources)."""
    store, job, db = prepared_store

    summary = run_all_analyses(job, db, engines=["aurora_postgresql"], store=store)

    assert summary["engines_analyzed"] == ["aurora_pg"]
    assert store.exists(f"{db}/{job}/analysis-aurora_postgresql/analysis.json")


def test_reports_triage_skips_with_reasons(prepared_store) -> None:
    """When engines come from triage, each candidate engine triage chose not to
    analyze is reported via ``on_engine_skipped`` and in ``engines_skipped`` with
    triage's reason. The WordPress sample is MySQL, so Aurora PostgreSQL is skipped
    as the non-matching Aurora variant."""
    store, job, db = prepared_store
    skips: list[tuple[str, str, str]] = []

    summary = run_all_analyses(
        job,
        db,
        store=store,
        on_engine_skipped=lambda e, p, r: skips.append((e, p, r)),
    )

    # Aurora PostgreSQL is the non-matching variant for a MySQL source.
    assert "aurora_pg" in summary["engines_skipped"]
    assert (
        summary["engines_skipped"]["aurora_pg"]
        == "Source engine does not match this Aurora variant"
    )
    # It is not among the analyzed engines.
    assert "aurora_pg" not in summary["engines_analyzed"]

    # The callback fired with the analysis_<target_database> label and the reason.
    skipped_engines = {e for e, _, _ in skips}
    assert "aurora_pg" in skipped_engines
    for engine, phase, reason in skips:
        assert phase == f"analysis_{_ANALYSIS_ENGINES[engine].target_database}"
        assert reason
