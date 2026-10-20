"""Anti-pattern stage: score the pipeline's anti-pattern verdict against a key (#1084).

A case names one catalog anti-pattern (``src/catalog/patterns/<id>.toml``) and a
bundled sample, and its key says, per query, whether the query shows that
anti-pattern (``shows``) and, optionally, which engines are acceptable for it.
The stage runs the same deterministic chain as ``final_assignment`` (collect,
triage, analysis, assignment, reality check; no model, no AWS call) and returns,
per case query, the pipeline's verdict (some engine's analysis reported the
pattern for the query) and its final engine. ``scoring.score_anti_pattern``
compares the two.

The chain runs once per sample per benchmark run: the cases of one sample share
``<work_root>/_sample-<sample>``.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from benchmarks.runner.loader import Case
from benchmarks.runner.stage_final_assignment import (
    _JOB,
    final_assignment,
    run_pipeline_to_final_assignment,
)
from src.storage.local_store import LocalArtifactStore


def pattern_keys(pattern_id: str) -> frozenset[str]:
    """The pattern's id and its penalty aliases (``complex_aggregation``, ...)."""
    from src.shared.catalog.patterns import pattern

    return frozenset({pattern_id, *pattern(pattern_id).penalty_aliases})


# Detected from the collector by the scorer itself, not by an engine's analysis
# (``query_scoring.merge_substring_scan``, #493).
SUBSTRING_MATCH = "substring-match"


def pipeline_flags(
    analyses: Mapping[str, Mapping], pattern_id: str, collector: Mapping | None = None
) -> set[str]:
    """Query ids the pipeline reports with ``pattern_id``.

    Both lists the scorer reads (``query_scoring.build_anti_pattern_map``):
    ``anti_patterns_detected`` (a concern for that engine) and ``patterns_detected``
    (a fit for that engine, which penalises the others). ``substring-match`` comes
    from the collector instead: every leading-wildcard ``LIKE`` or regular
    expression that is not a word search on a text column.
    """
    if pattern_id == SUBSTRING_MATCH:
        from src.agents.referee.substring_match import substring_matches

        return {q for q, m in substring_matches(collector).items() if not m.word_search}
    keys = pattern_keys(pattern_id)
    flagged: set[str] = set()
    for analysis in analyses.values():
        wa = analysis.get("workload_analysis") or {}
        for ap in wa.get("anti_patterns_detected") or []:
            if ap.get("anti_pattern_type") in keys:
                flagged.update(ap.get("query_ids") or [])
        for p in wa.get("patterns_detected") or []:
            if p.get("pattern_type") in keys:
                flagged.update(p.get("query_ids") or [])
    return flagged


def read_analyses(store: LocalArtifactStore, prefix: str) -> dict[str, dict]:
    """``{engine: analysis}`` for every engine triage selected."""
    triage = store.read_json(f"{prefix}/referee-triage/triage.json")
    out = {}
    for agent in triage.get("selected_agents", []):
        engine = agent["agent_type"]
        out[engine] = store.read_json(f"{prefix}/analysis-{engine}/analysis.json")
    return out


def _sample_run(case: Case, work_dir: Path) -> tuple[LocalArtifactStore, str]:
    if not case.sample:
        raise ValueError(f"{case.case_id}: an anti_pattern case needs a 'sample' in index.json")
    shared = Path(work_dir).parent / f"_sample-{case.sample}"
    prefix = f"{case.sample}/{_JOB}"
    done = shared / "artifacts" / prefix / "reality-check" / "output.json"
    if done.is_file():
        return LocalArtifactStore(str(shared / "artifacts")), prefix
    return run_pipeline_to_final_assignment(case, shared)


def pipeline_redesign(record: Mapping | None, pattern_id: str) -> dict[str, str]:
    """``{query_id: "fixable" | "intrinsic"}``: how the pipeline labelled each query's
    ``pattern_id`` finding (#1231), from an access-pattern record. A query with no
    penalised finding of the pattern is absent. The label depends on the pattern's
    penalty basis and the query's own rows, never on the engine, so one finding of the
    pattern on the query gives it."""
    keys = pattern_keys(pattern_id) if pattern_id != SUBSTRING_MATCH else {SUBSTRING_MATCH}
    out: dict[str, str] = {}
    for row in (record or {}).get("queries") or []:
        for f in row.get("anti_pattern_findings") or []:
            if f.get("pattern") in keys:
                out.setdefault(str(row.get("query_id") or ""), str(f.get("kind") or ""))
    return out


def run_anti_pattern_case(case: Case, work_dir: Path) -> dict[str, dict]:
    """``{query_id: {"shows": bool, "engine": str, "redesign": str | None}}`` for the
    case's queries."""
    if not case.anti_pattern:
        raise ValueError(f"{case.case_id}: no 'anti_pattern' in index.json")
    store, prefix = _sample_run(case, work_dir)
    collector = store.read_json(f"{prefix}/collector/output.json")
    flagged = pipeline_flags(read_analyses(store, prefix), case.anti_pattern, collector)
    record = store.read_json(f"{prefix}/assignment/v1/access_patterns.json")
    redesign = pipeline_redesign(record, case.anti_pattern)
    final = {
        qa["query_id"]: qa["assigned_engine"]
        for qa in final_assignment(store, case.sample or "").get("query_assignments", [])
    }
    ids = case.queries or sorted(final)
    return {
        q: {"shows": q in flagged, "engine": final[q], "redesign": redesign.get(q)}
        for q in ids
        if q in final
    }
