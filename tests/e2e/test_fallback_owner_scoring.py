"""``fallback_owner`` must agree with the resolver's own score, on real
pipeline artifacts (#549 round-2 review).

Two checks, both read straight off a deterministic pipeline run's own
``triage.json``/``collector/output.json``/``analysis-*/analysis.json`` --
the same artifacts ``tests/e2e/routing_snapshot.py`` reads:

- a sweep over every in-scope query of both samples: among the SAME
  candidate set ``fallback_owner`` itself computes, its pick's score must
  equal the best score among them -- it may legitimately pick a different
  engine than the resolver's own first assignment (the resolver also
  applies co-dependency grouping, signal overrides and the Aurora
  homogeneous guarantee, none of which ``fallback_owner`` replicates: it is
  a narrower, legacy-migration/customer-cache-request owner picker, not a
  second resolver), but it must never pick a worse-scoring, or an
  unserviceable, candidate over a better one it had the data to see.
- the discourse capability example the round-2 review found by hand (query
  ``9038857162851530990``): the resolver scores only ``aurora_postgresql``
  as serviceable for it; ``fallback_owner``, fed the same triage and
  collector, must agree.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.agents.referee.cache_overlay import fallback_owner, owner_candidates
from src.agents.referee.query_scoring import ScoringContext, score_query_for_engine
from tests.e2e.pipeline import PipelineResult

pytestmark = pytest.mark.e2e


def _load_pipeline_artifacts(job_dir: Path) -> tuple[dict, dict, dict[str, dict]]:
    triage = json.loads((job_dir / "referee-triage" / "triage.json").read_text())
    collector_output = json.loads((job_dir / "collector" / "output.json").read_text())
    selected = [a["agent_type"] for a in triage.get("selected_agents", [])]
    analysis_outputs = {
        engine: json.loads((job_dir / f"analysis-{engine}" / "analysis.json").read_text())
        for engine in selected
        if (job_dir / f"analysis-{engine}" / "analysis.json").exists()
    }
    return triage, collector_output, analysis_outputs


def _source_engine(collector_output: dict) -> str:
    return collector_output.get("metadata", {}).get("source_database", {}).get("engine", "") or ""


@pytest.mark.deterministic
def test_fallback_owner_never_beats_its_own_best_score(run: PipelineResult) -> None:
    triage, collector_output, analysis_outputs = _load_pipeline_artifacts(run.job_dir())
    queries: list[dict[str, Any]] = collector_output.get("queries", {}).get("query_patterns", [])
    source_engine = _source_engine(collector_output)
    ctx = ScoringContext.from_pipeline(analysis_outputs, triage, collector_output)

    mismatches = []
    for query in queries:
        qid = query["query_id"]
        candidates = owner_candidates(set(analysis_outputs), query)
        scores: dict[str, float] = {}
        for engine in candidates:
            result = score_query_for_engine(query, engine, ctx)
            if result.score is not None:
                scores[engine] = result.score
        if not scores:
            continue  # no serviceable candidate at all: fallback_owner's own documented case
        owner, _tied, _fallback_reason = fallback_owner(
            query, analysis_outputs, source_engine, {}, triage=triage, collector=collector_output
        )
        best = max(scores.values())
        if owner not in scores or scores[owner] != best:
            mismatches.append(
                {"query_id": qid, "picked": owner, "picked_score": scores.get(owner), "best": best}
            )

    assert not mismatches, (
        f"{run.db}: fallback_owner picked a worse-scoring (or unserviceable) candidate than its "
        f"own best score for {len(mismatches)} queries: {mismatches[:10]}"
    )


@pytest.mark.deterministic
def test_discourse_capability_signal_excludes_dynamodb(discourse_run: PipelineResult) -> None:
    # Discourse only (the query id is from that sample): the per-sample `run`
    # fixture would skip on wordpress and trip --fail-on-skip.
    run = discourse_run

    triage, collector_output, analysis_outputs = _load_pipeline_artifacts(run.job_dir())
    queries = collector_output.get("queries", {}).get("query_patterns", [])
    qid = "9038857162851530990"
    query = next((q for q in queries if str(q.get("query_id")) == qid), None)
    assert query is not None, f"query {qid} not found in the discourse sample's collector output"

    ctx = ScoringContext.from_pipeline(analysis_outputs, triage, collector_output)
    serviceable = set()
    for engine in analysis_outputs:
        result = score_query_for_engine(query, engine, ctx)
        if result.score is not None:
            serviceable.add(engine)

    assert "dynamodb" not in serviceable, (
        f"query {qid} should be unserviceable for dynamodb (round-2 review: the resolver scores "
        "only aurora_postgresql for it), but the capability gate let it through"
    )
    assert "aurora_postgresql" in serviceable

    owner, _tied, _fallback_reason = fallback_owner(
        query,
        analysis_outputs,
        _source_engine(collector_output),
        {},
        triage=triage,
        collector=collector_output,
    )
    assert owner == "aurora_postgresql"
