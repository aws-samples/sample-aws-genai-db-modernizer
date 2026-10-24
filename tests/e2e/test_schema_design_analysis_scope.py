"""Each schema designer's analysis input stays inside its own assignment (#162).

The collector input of a design call holds only the tables and queries assigned to its
engine (``filter_collector_for_assignment``). Its analysis input is narrowed to the same
scope (``filter_analysis_for_assignment``): every table recommendation, aggregate, pattern
and anti-pattern it reads names a table or a query of its own input, so a designer is
never given a classification for a table another engine serves, and every table it
designs keeps its recommendation. Checked on every bundled sample, for every engine the
latest assignment version designs (owners and the ElastiCache overlay, which owns no
query and designs the reads it fronts), on the external request and on the Bedrock
input. Deterministic pipeline; no model call.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from src.agents.referee.table_resolution import TableNameResolver
from src.agents.schema_design import handler
from src.storage.assignment_versioning import schema_design_engines
from src.storage.collector_for_models import read_collector_for_models
from src.storage.local_store import LocalArtifactStore
from tests.e2e.pipeline import PipelineResult

CACHE = "elasticache"


def _latest_version(job: Path) -> int:
    versions = [int(p.parent.name[1:]) for p in (job / "assignment").glob("v*/assignment.json")]
    assert versions, f"no assignment version under {job}"
    return max(versions)


class _Case:
    def __init__(self, run: PipelineResult, engine: str, version: int) -> None:
        self.run, self.engine, self.version = run, engine, version
        self.store = LocalArtifactStore(base_dir=str(run.artifact_root))
        self.collector = read_collector_for_models(self.store, run.db, run.job_id)
        self.resolver = TableNameResolver.from_collector(self.collector)
        assignment = self.store.read_json(
            f"{run.db}/{run.job_id}/assignment/v{version}/assignment.json"
        )
        # The engine's tables, read from the assignment itself: the collected tables
        # among the source tables of every in-scope query it owns or (a cache) fronts
        # (a pseudo table such as ``unknown`` names no collected table).
        collected = {t["table_id"] for t in self.collector["database_schema"]["tables"]}
        self.tables = {
            self.canonical(t)
            for qa in assignment.get("query_assignments") or []
            if qa.get("in_scope", True)
            and engine in (qa.get("assigned_engine"), qa.get("cache_engine"))
            for t in qa.get("source_tables") or []
        } & collected

    def canonical(self, name: str) -> str:
        return (self.resolver.resolve(name) if self.resolver else None) or name

    def check(self, scoped: dict, analysis: dict) -> None:
        where = (self.run.db, self.engine)
        assert _out_of_scope(self, scoped, analysis) == [], where
        kept = {self.canonical(r["table_id"]) for r in analysis["table_recommendations"]}
        assert kept == self.tables, where
        if self.engine == CACHE:
            context = scoped.get("cache_invalidation_context") or {}
            assert context.get("write_queries"), where


def _out_of_scope(case: _Case, scoped: dict, analysis: dict) -> list[str]:
    """Every analysis entry that names no table or query of the scoped collector input."""
    canonical = case.canonical
    tables = {t["table_id"] for t in scoped["database_schema"]["tables"]}
    queries = {str(q["query_id"]) for q in scoped["queries"]["query_patterns"]}
    out = [
        f"table_recommendations: {r['table_id']}"
        for r in analysis.get("table_recommendations") or []
        if canonical(r["table_id"]) not in tables
    ]
    out += [
        f"aggregate_recommendations: {a.get('root_table')}"
        for a in analysis.get("aggregate_recommendations") or []
        if not {canonical(m) for m in a.get("member_tables") or []} & tables
    ]
    workload = analysis.get("workload_analysis") or {}
    for key, id_key in (
        ("patterns_detected", "pattern_id"),
        ("anti_patterns_detected", "anti_pattern_id"),
    ):
        for p in workload.get(key) or []:
            stray_q = [q for q in p.get("query_ids") or [] if str(q) not in queries]
            stray_t = [t for t in p.get("table_ids") or [] if canonical(t) not in tables]
            if stray_q or stray_t:
                out.append(f"{key} {p.get(id_key)}: queries {stray_q}, tables {stray_t}")
    return out


def _cases(run: PipelineResult) -> list[_Case]:
    version = _latest_version(run.job_dir())
    store = LocalArtifactStore(base_dir=str(run.artifact_root))
    engines = [
        e
        for e in schema_design_engines(store, run.db, run.job_id, version)
        if (run.job_dir() / f"analysis-{e}" / "analysis.json").exists()
    ]
    assert engines, f"{run.db}: no engine designed in assignment v{version}"
    return [_Case(run, engine, version) for engine in engines]


def test_the_external_request_names_only_the_engines_own_tables(run: PipelineResult) -> None:
    for case in _cases(run):
        request: dict[str, Any] = handler.prepare_schema_design_input(
            run.job_id, run.db, case.engine, case.store, assignment_version=case.version
        )
        case.check(request["collector_output"], request["analysis_output"])


def test_the_bedrock_input_names_only_the_engines_own_tables(
    run: PipelineResult, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    def fake_dispatch(target_type, collector_path, analysis_path, **_kw):
        seen["collector"] = json.loads(Path(collector_path).read_text())
        seen["analysis"] = json.loads(Path(analysis_path).read_text())
        raise RuntimeError("stop before any model call")

    monkeypatch.setattr(handler, "require_valid_guardrail_config", lambda: None)
    monkeypatch.setattr(handler, "_dispatch_schema_agent", fake_dispatch)
    for case in _cases(run):
        seen.clear()
        with pytest.raises(RuntimeError, match="stop before"):
            handler.run_schema_design(run.job_id, run.db, case.engine, case.store, case.version)
        case.check(seen["collector"], seen["analysis"])


@pytest.mark.deterministic
def test_the_cache_overlay_is_checked_on_every_sample(run: PipelineResult) -> None:
    """ElastiCache owns no query, so it is found through ``cache_engine`` (#296)."""
    if run.external:
        pytest.skip("a headless run may not route a read to the cache")
    assert CACHE in {c.engine for c in _cases(run)}, run.db
