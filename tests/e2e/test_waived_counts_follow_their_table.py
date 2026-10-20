"""#1151: a key-scoped COUNT on an engine only its waiver lets serve sits on its
table's source of truth, in every assignment version the pipeline writes.

ADR-031 Decision 2 Amendment 12 lifts DynamoDB's ``aggregation`` gate for such a count
only while the table's writes are on DynamoDB. The first assignment and the reality
check place counts with the waiver; the customer gate and the post-schema reroutes
re-place a count when they move its table's writes (unit-tested in
``tests/unit/agents/referee/test_waived_count_replacement.py``). Here, every version
of every sample is checked against the rule itself.
"""

from __future__ import annotations

import json

from src.agents.referee.query_scoring import ScoringContext
from src.agents.referee.waived_counts import lapsed, version_sources
from tests.e2e.pipeline import PipelineResult


def _read(path) -> dict:
    out: dict = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    return out


def test_no_waived_count_sits_off_its_tables_source_of_truth(run: PipelineResult) -> None:
    job = run.job_dir()
    collector = _read(job / "collector" / "output.json")
    queries = {q["query_id"]: q for q in collector["queries"]["query_patterns"]}
    analyses = {
        p.parent.name.removeprefix("analysis-"): _read(p)
        for p in job.glob("analysis-*/analysis.json")
    }
    ctx = ScoringContext.from_pipeline(
        analyses, _read(job / "referee-triage" / "triage.json") or None, collector
    )
    versions = sorted(
        (job / "assignment").glob("v*/assignment.json"), key=lambda p: int(p.parent.name[1:])
    )
    assert versions, run.db
    for path in versions:
        assignment = _read(path)
        qas = assignment["query_assignments"]
        sources = version_sources(qas, assignment.get("write_groups") or {})
        stranded = sorted(
            qa["query_id"]
            for qa in qas
            if qa.get("in_scope", True)
            and qa["query_id"] in queries
            and lapsed(queries[qa["query_id"]], qa["assigned_engine"], ctx, sources)
        )
        assert stranded == [], (run.db, path.parent.name, stranded)
