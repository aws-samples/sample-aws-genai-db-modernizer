# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Rule-catalog defects: analyses that contradict each other on a sample (#1994).

A query one engine's analysis points at another engine, while that engine's analysis
points it back, is settled by the single scorer before synthesis
(``src/agents/referee/analysis_consistency.py``), so the deliverables never carry both
statements. Each such pair on a bundled sample (the benchmark cases are built on the same
samples) is still a defect in the rule catalog: two pattern entries disagree about the
same access pattern. This test lists them, and the list is expected to be empty. A new
entry here is fixed in the catalog rule, never by editing the expectation.

One pair is accepted, not a defect (maintainer decision of 2026-11-08 on #1994, option
(a)): a whole-table ``ORDER BY <pk> LIMIT 1`` with a literal 1 is a key-value lookup that
retrieves all the columns (#692, #857), so Aurora's whole-workload anti-patterns keep it
while DynamoDB's ordered-scan points it back; the guard settles it on the routed engine.
"""

from __future__ import annotations

import json

import pytest

from src.agents.referee.analysis_consistency import Contradiction, find_contradictions
from src.agents.referee.query_shape import is_primary_key_first_row_read
from tests.e2e.pipeline import PipelineResult

pytestmark = pytest.mark.e2e

# The accepted pair: Aurora's whole-workload rules against DynamoDB's ordered-scan.
_FIRST_ROW_PAIR = {
    ("aurora_mysql", "no-relational-need"),
    ("aurora_postgresql", "no-relational-need"),
    ("aurora_mysql", "single-access-pattern-table"),
    ("aurora_postgresql", "single-access-pattern-table"),
    ("dynamodb", "ordered-scan"),
}


def _accepted_first_row_read(c: Contradiction, collector: dict) -> bool:
    if not {(cl.engine, cl.pattern_type) for cl in c.claims} <= _FIRST_ROW_PAIR:
        return False
    queries = {q.get("query_id"): q for q in collector["queries"]["query_patterns"]}
    tables = {t.get("table_id"): t for t in collector["database_schema"]["tables"]}
    source = collector.get("metadata", {}).get("source_database", {}).get("engine")
    query = queries.get(c.query_id) or {}
    accessed = query.get("tables_accessed") or []
    if len(accessed) != 1:
        return False
    primary_key = (tables.get(accessed[0]) or {}).get("primary_key") or []
    return is_primary_key_first_row_read(query.get("query_text"), primary_key, None, source)


def test_no_two_analyses_point_a_query_at_each_other(run: PipelineResult) -> None:
    analyses = {
        path.parent.name.removeprefix("analysis-"): json.loads(path.read_text())
        for path in sorted(run.job_dir().glob("analysis-*/analysis.json"))
    }
    assert analyses, f"{run.db}: no analysis artifacts"
    collector = json.loads((run.job_dir() / "collector" / "output.json").read_text())
    defects = [
        f"{c.query_id}: "
        + " vs ".join(f"{cl.engine} {cl.pattern_type} -> {cl.better_engine}" for cl in c.claims)
        for c in find_contradictions(analyses)
        if not _accepted_first_row_read(c, collector)
    ]
    assert defects == [], f"{run.db}: {len(defects)} rule-catalog defect(s):\n" + "\n".join(
        defects[:20]
    )
