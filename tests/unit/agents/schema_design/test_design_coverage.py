# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A first design names every statement its input lists, or fails its check (#2153).

Before #2153 only a revision ran the coverage check (``revision_verifier.check_coverage``):
on every saved discourse run a merged DynamoDB design (and once a single-pass DocumentDB
design) left 1-3 assigned statements out of every access pattern and still passed, and the
report showed them only as ungrouped. ``handler.apply_schema_scope`` now runs the check on
finalize, ``--merge`` and the Bedrock designers alike (``design_coverage``).
"""

from __future__ import annotations

from pathlib import Path

from src.agents.schema_design.design_coverage import (
    check_design_coverage,
    coverage_violations,
    required_query_ids,
)
from src.agents.schema_design.handler import apply_schema_scope, run_schema_merge
from src.agents.schema_design.scope import COVERAGE_PREFIX, SCOPE_PREFIX
from src.storage.local_store import LocalArtifactStore
from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output
from tests.unit.agents.schema_design.test_design_per_access_pattern import (
    BASE,
    PRODUCTS,
    _draft_for,
    _entries,
    _members,
    _q,
    _split,
)

JOB = "ecommerce/j1"
UNION = (
    "(SELECT $1 FROM products WHERE category_id = $2 LIMIT $3) "
    "UNION (SELECT $4 FROM products WHERE brand = $5 LIMIT $6)"
)


def _assignment(query_ids: list[str], engine: str = "dynamodb") -> dict:
    return {
        "version": 1,
        "query_assignments": [
            {
                "query_id": qid,
                "assigned_engine": engine,
                "source_tables": [PRODUCTS],
                "in_scope": True,
            }
            for qid in query_ids
        ],
    }


def _job(store: LocalArtifactStore, queries: list[dict], engine: str = "dynamodb") -> None:
    collector = get_ecommerce_collector_output()
    collector["queries"]["query_patterns"] = queries
    store.write_json(f"{JOB}/collector/output.json", collector)
    store.write_json(
        f"{JOB}/assignment/v1/assignment.json",
        _assignment([q["query_id"] for q in queries], engine),
    )


def _split_job(tmp_path: Path, queries: list[dict]) -> tuple[LocalArtifactStore, dict, dict]:
    store, manifest, inputs = _split(tmp_path, queries)
    _job(store, queries)
    return store, manifest, inputs


def _write_drafts(store: LocalArtifactStore, inputs: dict, drop: str | None = None) -> None:
    """One draft per part naming every member of every entry, except ``drop``."""
    for n, data in inputs.items():
        draft = _draft_for(data, members_too=True)
        draft["access_patterns"] = [
            {**ap, "query_ids": [q for q in ap["query_ids"] if q != drop]}
            for ap in draft["access_patterns"]
        ]
        draft["access_patterns"] = [ap for ap in draft["access_patterns"] if ap["query_ids"]]
        store.write_json(f"{BASE}/schema_draft_group_{n}.json", draft)


def _part_of(inputs: dict, qid: str) -> int:
    return int(
        next(n for n, data in inputs.items() if qid in {q["query_id"] for q in _entries(data)})
    )


def _workload() -> list[dict]:
    """24 three-statement patterns (two parts) and one one-statement pattern."""
    return [
        *(q for p in range(24) for q in _members(p, 3)),
        _q("single", "DELETE FROM products WHERE brand = ?", 0.1, query_type="DELETE"),
    ]


def test_a_merged_design_that_drops_a_one_statement_pattern_fails_coverage(
    tmp_path: Path,
) -> None:
    """The discourse case: a part's draft leaves a one-statement pattern out of every
    access pattern (the merge's member fix-up only completes patterns it serves). The
    merge fails its check, names the part whose input lists the statement, and writes
    the design with validation_passed false; fixing the draft clears it."""
    store, _, inputs = _split_job(tmp_path, _workload())
    part = _part_of(inputs, "single")
    _write_drafts(store, inputs, drop="single")

    report = run_schema_merge("j1", "ecommerce", "dynamodb", store, assignment_version=1)

    coverage = [v for v in report.violations if v.startswith(COVERAGE_PREFIX)]
    assert len(coverage) == 1
    assert "'single'" in coverage[0] and f"group {part}'s input" in coverage[0]
    written = store.read_json(f"{BASE}/schema_output.json")
    assert written["validation_passed"] is False
    assert coverage[0] in written["validation_failures"]

    _write_drafts(store, inputs)
    fixed = run_schema_merge("j1", "ecommerce", "dynamodb", store, assignment_version=1)
    assert not [v for v in fixed.violations if v.startswith(COVERAGE_PREFIX)]
    rewritten = store.read_json(f"{BASE}/schema_output.json")
    assert not [f for f in rewritten["validation_failures"] if f.startswith(COVERAGE_PREFIX)]


def test_a_parenthesized_union_is_designed_not_skipped(tmp_path: Path) -> None:
    """A parenthesized UNION is typed OTHER by the collector today; routed to DynamoDB, it
    is still in a part's input and a design must name it (the skill no longer says to
    skip OTHER). Leaving it out fails coverage; naming it passes."""
    union = _q("union", UNION, 0.2, query_type="OTHER")
    queries = [*(q for p in range(24) for q in _members(p, 3)), union]
    store, _, inputs = _split_job(tmp_path, queries)
    assert any("union" in {q["query_id"] for q in _entries(d)} for d in inputs.values())

    _write_drafts(store, inputs, drop="union")
    dropped = run_schema_merge("j1", "ecommerce", "dynamodb", store, assignment_version=1)
    assert [v for v in dropped.violations if "'union'" in v and v.startswith(COVERAGE_PREFIX)]

    _write_drafts(store, inputs)
    named = run_schema_merge("j1", "ecommerce", "dynamodb", store, assignment_version=1)
    assert not [v for v in named.violations if v.startswith(COVERAGE_PREFIX)]

    skill = Path("src/skills/dynamodb-data-modeling.md").read_text()
    assert "Skip `query_type = OTHER`" not in skill
    assert "excluding `OTHER`" not in skill
    assert "excluding `query_type=OTHER`" not in skill


def _documentdb_design(query_ids: list[str]) -> dict:
    return {
        "collections": [{"collection_name": "products", "source_tables": [PRODUCTS]}],
        "access_patterns": [
            {"pattern_id": "DOC-AP-1", "source_tables": [PRODUCTS], "source_query_ids": query_ids}
        ],
        "unsupported_patterns": [],
        "validation_passed": True,
        "validation_failures": [],
    }


def test_a_single_pass_design_that_drops_a_statement_fails_coverage(tmp_path: Path) -> None:
    """The DocumentDB case: one design call, one of its statements in no access pattern
    (an unsupported_patterns entry does not serve it either)."""
    store = LocalArtifactStore(str(tmp_path))
    queries = [
        _q("a", "SELECT * FROM products WHERE id = ?", 1.0),
        _q("b", "SELECT * FROM products WHERE brand = ?", 1.0),
    ]
    _job(store, queries, engine="documentdb")
    design = _documentdb_design(["a"])
    design["unsupported_patterns"] = [{"source_query_ids": ["b"], "reason": "r"}]

    checked, report = apply_schema_scope(store, "ecommerce", "j1", "documentdb", design, 1)

    assert report.violations == [
        f"{COVERAGE_PREFIX}documentdb: query 'b' is assigned to documentdb and listed in "
        "this engine's design input, but no access pattern names it. Design it in an "
        "access pattern (whatever its query_type); a trade-off or an unsupported_patterns "
        "entry does not serve it."
    ]
    assert checked["validation_passed"] is False
    again, report = apply_schema_scope(
        store, "ecommerce", "j1", "documentdb", {**checked, **_documentdb_design(["a", "b"])}, 1
    )
    assert report.violations == []


def test_statements_no_design_serves_are_not_required(tmp_path: Path) -> None:
    """A statement with no source table (``excluded_queries``) and a statement another
    engine owns are not asked of the design; every other engine and version keeps its
    rules: Aurora and a legacy version 0 are not checked."""
    catalog = {**_q("catalog", "SELECT pg_get_serial_sequence($1, $2)", 0.1), "tables_accessed": []}
    queries = [_q("a", "SELECT * FROM products WHERE id = ?", 1.0), catalog]
    assignment = _assignment(["a", "catalog"])
    collector = get_ecommerce_collector_output()
    collector["queries"]["query_patterns"] = queries
    assert required_query_ids(collector, assignment, "dynamodb") == ["a"]
    assert required_query_ids(collector, assignment, "documentdb") == []
    design = {"access_patterns": [], "excluded_queries": [{"query_id": "a"}]}
    assert coverage_violations("dynamodb", design, ["a"]) == []
    assert coverage_violations("aurora_postgresql", {"access_patterns": []}, ["a"]) == []

    store = LocalArtifactStore(str(tmp_path))
    _job(store, queries)
    assert (
        check_design_coverage(store, "ecommerce", "j1", "dynamodb", {"access_patterns": []}, 0)
        == []
    )


def test_an_old_saved_design_still_loads_and_is_rechecked(tmp_path: Path) -> None:
    """A design saved before #2153 (passed, no coverage message) is re-checked on its next
    finalize or --merge: its other failures are kept, a stale coverage message is
    replaced, and a skipped placeholder (no access patterns) is never checked."""
    store = LocalArtifactStore(str(tmp_path))
    queries = [_q("a", "SELECT * FROM products WHERE id = ?", 1.0)]
    _job(store, queries, engine="documentdb")
    saved = {
        **_documentdb_design(["a"]),
        "validation_passed": False,
        "validation_failures": [
            "design_metrics: kept",
            f"{COVERAGE_PREFIX}documentdb: query 'gone' is stale",
            f"{SCOPE_PREFIX}documentdb: stale",
        ],
    }
    checked, report = apply_schema_scope(store, "ecommerce", "j1", "documentdb", saved, 1)
    assert report.violations == []
    assert checked["validation_failures"] == ["design_metrics: kept"]
    assert checked["validation_passed"] is False

    skipped = {"status": "skipped", "reason": "No queries or tables assigned to this engine"}
    assert check_design_coverage(store, "ecommerce", "j1", "documentdb", skipped, 1) == []
