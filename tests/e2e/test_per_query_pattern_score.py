# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The per-query access-pattern score on the bundled samples (#728, ADR-031 Decision 14 Amendment 5).

- Every query of every sample gets exactly one access-pattern label.
- The ownership gate holds through the switch (ADR-031 Decision 2 step 3, #920): in
  every assignment version a sample writes, no write and no locking read is owned by
  an engine that is not a system of record (OpenSearch, the cache overlay).
- #885: discourse's key-anchored ``topics`` + ``categories`` joins are key-scoped reads
  scored on their own class, and stay on Aurora PostgreSQL in the first and the final
  assignment (#885's strongest-pattern rule had sent 29 of them to DynamoDB).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.agents.referee.access_pattern import KEY_SCOPED, SCORED_CLASSES, UNPARSED, UTILITY
from src.agents.referee.cache_overlay import NON_SYSTEM_OF_RECORD_ENGINES, can_own
from src.agents.referee.query_pattern import QueryPatternModel
from src.agents.referee.query_shape import locking_clause
from tests.e2e.pipeline import PipelineResult

pytestmark = pytest.mark.e2e

_WRITES = frozenset({"INSERT", "UPDATE", "DELETE", "MERGE", "REPLACE"})


def _collector(job_dir: Path) -> dict:
    collector: dict = json.loads((job_dir / "collector" / "output.json").read_text())
    return collector


def _versions(job_dir: Path) -> list[tuple[str, dict[str, dict]]]:
    """Every assignment version the run wrote, v1 first, as ``(name, query_id -> qa)``."""
    paths = sorted(
        job_dir.glob("assignment/v*/assignment.json"), key=lambda p: int(p.parent.name[1:])
    )
    return [
        (
            p.parent.name,
            {qa["query_id"]: qa for qa in json.loads(p.read_text())["query_assignments"]},
        )
        for p in paths
    ]


@pytest.mark.deterministic
def test_every_query_has_one_access_pattern_label(run: PipelineResult) -> None:
    collector = _collector(run.job_dir())
    model = QueryPatternModel.from_pipeline(collector, {})
    labels = {model.classification(q).name for q in collector["queries"]["query_patterns"]}
    assert labels <= {*SCORED_CLASSES, UTILITY, UNPARSED}
    assert labels & set(SCORED_CLASSES)


@pytest.mark.deterministic
def test_no_write_or_locking_read_is_owned_by_an_engine_that_is_not_a_system_of_record(
    run: PipelineResult,
) -> None:
    job_dir = run.job_dir()
    queries = {q["query_id"]: q for q in _collector(job_dir)["queries"]["query_patterns"]}
    owned = []
    for version, assignments in _versions(job_dir):
        for qid, qa in assignments.items():
            query = queries[qid]
            engine = qa["assigned_engine"]
            writes = str(query.get("query_type") or "").upper() in _WRITES
            locks = locking_clause(query.get("query_text")) is not None
            if (writes or locks) and engine in NON_SYSTEM_OF_RECORD_ENGINES:
                owned.append((version, qid, engine))
            assert can_own(engine, query), (version, qid, engine)
    assert not owned, owned[:10]


@pytest.mark.deterministic
def test_885_key_anchored_topics_and_categories_joins_stay_on_aurora_postgresql(
    discourse_run: PipelineResult,
) -> None:
    job_dir = discourse_run.job_dir()
    collector = _collector(job_dir)
    model = QueryPatternModel.from_pipeline(collector, {})
    joins = [
        q["query_id"]
        for q in collector["queries"]["query_patterns"]
        if {t.rsplit(".", 1)[-1] for t in q.get("tables_accessed") or []}
        == {"topics", "categories"}
        and model.classification(q).name == KEY_SCOPED
    ]
    assert len(joins) >= 20, "the #885 repro needs discourse's key-anchored joins"
    versions = _versions(job_dir)
    for version, assignments in (versions[0], versions[-1]):
        engines = {qid: assignments[qid]["assigned_engine"] for qid in joins}
        assert set(engines.values()) == {"aurora_postgresql"}, (version, engines)


@pytest.mark.deterministic
def test_every_assignment_version_has_its_access_patterns_record(run: PipelineResult) -> None:
    """ADR-031 Decision 14 Amendment 7: ``assignment/v<N>/access_patterns.json`` next to
    every version a writer creates (the first assignment and the reality check here)."""
    from src.agents.referee.query_pattern import ACCESS_PATTERNS_FILE

    job_dir = run.job_dir()
    collector = _collector(job_dir)
    query_ids = [q["query_id"] for q in collector["queries"]["query_patterns"]]
    versions = sorted(job_dir.glob("assignment/v*/assignment.json"))
    assert len(versions) >= 2, "the sample's reality check writes a version of its own"
    for assignment in versions:
        record = json.loads((assignment.parent / ACCESS_PATTERNS_FILE).read_text())
        assert [r["query_id"] for r in record["queries"]] == query_ids, assignment.parent.name
