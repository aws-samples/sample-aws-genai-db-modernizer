"""Query tags change no routing (#1180).

``QueryPattern.query_tags`` is the caller's join key, never a routing input. Each
sample runs collect, triage, the deterministic analyses and the assignment in this
process twice, once as collected and once with tags on every query (several callers
per shape, some dropped by the caps), and every per-query routing decision is the
same: the triage, every engine's analysis, every query's engine and ties, its
access-pattern class (``assignment/v1/access_patterns.json``), and the resolver's
trace of each candidate engine's score through each step (the routing snapshot's own
fingerprint, ``tests/e2e/routing_snapshot.py``).
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from src.agents.referee.assignment_resolver import AssignmentResolver, RoutingTrace
from src.storage.local_store import LocalArtifactStore
from src.tools.database.collection_text import extract_json_object

_REPO = Path(__file__).resolve().parents[4]
_VOLATILE = {
    "job_id",
    "timestamp",
    "analysis_timestamp",
    "analysis_duration_seconds",
    "created_at",
    "generated_at",
    "duration_ms",
    "duration_seconds",
}


def _sample(name: str) -> dict:
    with zipfile.ZipFile(_REPO / "docs" / "examples" / name / f"{name}.zip") as z:
        member = next(n for n in z.namelist() if n.endswith("-collection.json"))
        data: dict = json.loads(extract_json_object(z.read(member).decode("utf-8")))
    return data


def _stable(value):
    """``value`` without the run's clock readings."""
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items() if k not in _VOLATILE}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    return value


def _route(tmp: Path, name: str, raw: dict, tag: bool) -> dict:
    import scripts.run_assessment as run_assessment

    tmp.mkdir()
    collection = tmp / f"{name}-collection.json"
    collection.write_text(json.dumps(raw), encoding="utf-8")
    store = LocalArtifactStore(base_dir=str(tmp / "artifacts"))
    mp = pytest.MonkeyPatch()
    mp.chdir(tmp)  # phase_collect writes .modernizer-state.json here
    try:
        job_id, db = run_assessment.phase_collect(str(collection), name, store)
        base = f"{db}/{job_id}"
        collector = store.read_json(f"{base}/collector/output.json")
        patterns = collector["queries"]["query_patterns"]
        assert all(q["query_tags"] == [] for q in patterns)  # these samples have none
        if tag:
            for i, q in enumerate(patterns):
                q["query_tags"] = [
                    f"controller='c{i % 7}',action='run'",
                    f"traceparent='00-{i:032x}-{i:016x}-01'",
                ]
                q["query_tags_dropped"] = i % 3
            store.write_json(f"{base}/collector/output.json", collector)
        selected = run_assessment.phase_triage(store, job_id, db)
        run_assessment.phase_analysis(store, job_id, db, selected)
        run_assessment.phase_assignment(store, job_id, db)
    finally:
        mp.undo()
    triage = store.read_json(f"{base}/referee-triage/triage.json")
    collector = store.read_json(f"{base}/collector/output.json")
    analyses = {e: store.read_json(f"{base}/analysis-{e}/analysis.json") for e in selected}
    trace = RoutingTrace()
    AssignmentResolver().resolve(triage, analyses, collector, routing_trace=trace)
    assignment = store.read_json(f"{base}/assignment/v1/assignment.json")
    access_patterns = store.read_json(f"{base}/assignment/v1/access_patterns.json")
    return {
        "triage": _stable(triage),
        "analyses": _stable(analyses),
        "engines": {
            qa["query_id"]: (qa["assigned_engine"], qa.get("tied_engines"), qa.get("in_scope"))
            for qa in assignment["query_assignments"]
        },
        "trace": _stable(trace.by_query),
        # each query's access-pattern class and class scores, as routing scored them
        "access_patterns": _stable(access_patterns),
    }


@pytest.mark.parametrize("name", ["trailgear", "wordpress"])
def test_tags_change_no_routing(tmp_path: Path, name: str) -> None:
    raw = _sample(name)
    plain = _route(tmp_path / "plain", name, raw, tag=False)
    tagged = _route(tmp_path / "tagged", name, raw, tag=True)
    assert plain["engines"]  # the run routed every query
    for key in ("engines", "trace", "access_patterns", "triage", "analyses"):
        assert tagged[key] == plain[key], key
