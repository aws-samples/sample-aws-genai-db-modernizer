"""The public result's ``summary.tables`` say what the deliverables say (#1182).

``assess()`` stops before synthesis, so it builds each table's owner line from the
assignment with the facts layer's own functions. On the designed wordpress job,
synthesis rendered ``facts.table_owner_lines`` from the same assignment: every line
it states must be the builder's line for that table.
"""

from __future__ import annotations

import json

import pytest

from src.assessment.result_builder import build_relational_engine, build_table_lines, load_run_state
from src.storage.local_store import LocalArtifactStore
from tests.e2e.pipeline import PipelineResult

pytestmark = pytest.mark.deterministic

FIELDS = ("owner", "owner_basis", "also_read_by", "cached_by")


@pytest.mark.parametrize("job", ["designed_run", "designed_partial_run"])
def test_summary_tables_match_the_reports_owner_lines(
    job: str, request: pytest.FixtureRequest
) -> None:
    run: PipelineResult = request.getfixturevalue(job)
    store = LocalArtifactStore(base_dir=str(run.artifact_root))
    state = load_run_state(store, run.db, run.job_id, "reality_check")
    relational = build_relational_engine(state)
    built = {
        line.table_id: line.model_dump()
        for line in build_table_lines(state, relational.engine if relational else None)
    }
    report = json.loads(run.report_json().read_text(encoding="utf-8"))
    stated = report["facts"]["table_owner_lines"]["table_lines"]
    assert stated
    routed = {t["table_id"] for t in state.assignment["table_assignments"]}
    compared = 0
    for line in stated:
        if line["table"] not in routed:
            # A table no statement reads, mapped only by a schema design: assess() has
            # no design, so it has no line for it.
            assert line["table"] not in built
            continue
        assert {k: built[line["table"]][k] for k in FIELDS} == {k: line[k] for k in FIELDS}
        compared += 1
    assert compared >= 10


@pytest.mark.parametrize("job", ["designed_run", "designed_partial_run"])
def test_engine_reasons_are_the_reports_why_text(job: str, request: pytest.FixtureRequest) -> None:
    """``summary.engines[].reason`` for an owner or the cache is the executive report's
    "why this engine" text (#1390): on the designed job, synthesis stated it in
    ``facts.engines`` from the same assignment, with its schema designs."""
    from src.assessment.models import AssessmentRequest
    from src.assessment.result_builder import build_statements, build_summary

    run: PipelineResult = request.getfixturevalue(job)
    store = LocalArtifactStore(base_dir=str(run.artifact_root))
    state = load_run_state(store, run.db, run.job_id, "reality_check")
    request_ = AssessmentRequest(database_name=run.db, collection={"x": 1})
    summary = build_summary(state, build_statements(state, request_))
    report = json.loads(run.report_json().read_text(encoding="utf-8"))
    why = {e["engine"]: e["rationale"] for e in report["facts"]["engines"]}
    kept = [e for e in summary.engines if e.role in ("owner", "cache_layer")]
    assert kept and {e.engine for e in kept} == set(why)
    for e in kept:
        assert e.reason == why[e.engine], e.engine
