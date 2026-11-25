# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""
Property-based tests for the Assignment Handler.

Tests correctness properties from the design document:
- Property 1: Assignment Completeness — every query from collector appears in assignment
- Property 6: Assignment Version Monotonicity — version numbers increase monotonically
- Property 19: Versioned Artifact Immutability — versioned artifacts written to /v{N}/ paths
- The #2152 refusal: a statement placed on Aurora with no Aurora analysis is
  refused and nothing is written (#2368)

**Validates: Requirements 2.1, 4.2, 4.3**
"""

from __future__ import annotations

import tempfile

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

from src.agents.referee.assignment_handler import run_assignment_resolver
from src.agents.referee.assignment_validator import InvalidAssignment
from src.agents.referee.table_resolution import SYSTEM_SCHEMAS, is_engine_system_object
from src.storage.local_store import LocalArtifactStore

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_engine = st.sampled_from(["dynamodb", "aurora", "opensearch", "documentdb"])
# Application tables only (#2368). A statement that reads only an engine's own
# catalog (``sys.users``, ``mysql.db``) scores on no engine and falls back to
# Aurora, which the validator refuses when Aurora was not analysed (#2152). The
# refusal has its own property below; these jobs must be ones it accepts.
_table_id = st.from_regex(r"[a-z]{2,6}\.[a-z_]{2,8}", fullmatch=True).filter(
    lambda table_id: not is_engine_system_object(table_id)
)
_system_table_id = st.builds(
    "{}.{}".format,
    st.sampled_from(sorted(SYSTEM_SCHEMAS)),
    st.from_regex(r"[a-z_]{2,8}", fullmatch=True),
)
_query_id = st.from_regex(r"q-[0-9]{6}", fullmatch=True)


@st.composite
def query_strategy(draw: st.DrawFn, table_ids: list[str]) -> dict:
    """Generate a query dict accessing some of the given tables."""
    n_tables = draw(st.integers(min_value=1, max_value=max(1, min(3, len(table_ids)))))
    accessed = draw(
        st.lists(
            st.sampled_from(table_ids),
            min_size=n_tables,
            max_size=n_tables,
            unique=True,
        )
    )
    qid = draw(_query_id)
    join_count = draw(st.integers(min_value=0, max_value=5))
    has_aggregation = draw(st.booleans())
    filter_tables = draw(
        st.lists(st.sampled_from(accessed), min_size=0, max_size=len(accessed), unique=True)
    )
    return {
        "query_id": qid,
        "query_text": "SELECT ... FROM ...",
        "query_type": "SELECT",
        "tables_accessed": accessed,
        "join_count": join_count,
        "has_joins": join_count > 0 or len(accessed) > 1,
        "has_aggregations": has_aggregation,
        "filter_tables": filter_tables,
        "calls_per_second": 1.0,
        "rows_returned_avg": 10,
    }


@st.composite
def analysis_output_strategy(draw: st.DrawFn, table_ids: list[str]) -> dict:
    """Generate a minimal analysis output dict with table recommendations."""
    recs = []
    for tid in table_ids:
        recs.append(
            {
                "table_id": tid,
                "confidence_score": draw(st.integers(min_value=0, max_value=100)),
                "rationale": "test recommendation",
                "score_breakdown": {
                    "pattern_match_score": draw(st.integers(min_value=0, max_value=100)),
                    "complexity_score": draw(st.integers(min_value=0, max_value=100)),
                    "performance_score": draw(st.integers(min_value=0, max_value=100)),
                    "cost_score": draw(st.integers(min_value=0, max_value=100)),
                },
            }
        )
    return {
        "table_recommendations": recs,
        "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
    }


@st.composite
def handler_fixture_strategy(draw: st.DrawFn) -> tuple[list[dict], dict, dict[str, dict], dict]:
    """Generate (queries, collector_output, analysis_outputs, triage) for the handler.

    Returns a tuple of (queries, collector_output, analysis_outputs, triage).
    """
    n_tables = draw(st.integers(min_value=1, max_value=4))
    table_ids = draw(st.lists(_table_id, min_size=n_tables, max_size=n_tables, unique=True))

    n_queries = draw(st.integers(min_value=1, max_value=6))
    queries: list[dict] = []
    used_qids: set[str] = set()
    for _ in range(n_queries):
        q = draw(query_strategy(table_ids))
        while q["query_id"] in used_qids:
            q = draw(query_strategy(table_ids))
        used_qids.add(q["query_id"])
        queries.append(q)

    collector_output = {
        "job_id": "test-job",
        "database_schema": {
            "tables": [{"table_id": tid, "table_name": tid.split(".")[-1]} for tid in table_ids]
        },
        "queries": {"query_patterns": queries},
    }

    n_engines = draw(st.integers(min_value=1, max_value=3))
    engines = draw(st.lists(_engine, min_size=n_engines, max_size=n_engines, unique=True))
    analysis_outputs: dict[str, dict] = {}
    for engine in engines:
        analysis_outputs[engine] = draw(analysis_output_strategy(table_ids))

    triage = {
        "selected_agents": [{"agent_type": e, "reasons": ["signal"]} for e in engines],
    }

    return queries, collector_output, analysis_outputs, triage


def _seed_artifacts(
    store: LocalArtifactStore,
    db: str,
    job_id: str,
    collector_output: dict,
    analysis_outputs: dict[str, dict],
    triage: dict,
) -> None:
    """Write prerequisite artifacts so the handler can read them."""
    store.write_json(f"{db}/{job_id}/collector/output.json", collector_output)
    store.write_json(f"{db}/{job_id}/referee-triage/triage.json", triage)
    for engine, analysis in analysis_outputs.items():
        store.write_json(f"{db}/{job_id}/analysis-{engine}/analysis.json", analysis)


# ---------------------------------------------------------------------------
# Property 1: Assignment Completeness
# ---------------------------------------------------------------------------


class TestAssignmentCompleteness:
    """**Validates: Requirements 2.1**

    Property 1: every query from collector appears in assignment.
    """

    @given(data=handler_fixture_strategy())
    @settings(deadline=None)
    def test_handler_produces_complete_assignment(
        self,
        data: tuple[list[dict], dict, dict[str, dict], dict],
    ) -> None:
        """Running the handler must produce an assignment artifact where
        every query_id from the collector output appears exactly once."""
        queries, collector_output, analysis_outputs, triage = data
        db = "testdb"
        job_id = "test-job"

        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalArtifactStore(tmpdir)
            _seed_artifacts(store, db, job_id, collector_output, analysis_outputs, triage)

            run_assignment_resolver(job_id, db, store)

            # Read the written assignment artifact
            assignment_data = store.read_json(f"{db}/{job_id}/assignment/v1/assignment.json")

            collector_qids = {q["query_id"] for q in queries}
            assigned_qids = {qa["query_id"] for qa in assignment_data["query_assignments"]}

            assert assigned_qids == collector_qids, (
                f"Mismatch: collector has {collector_qids}, " f"assignment has {assigned_qids}"
            )

            # Each query_id appears exactly once
            all_qids = [qa["query_id"] for qa in assignment_data["query_assignments"]]
            assert len(all_qids) == len(set(all_qids)), (
                f"Duplicate query_ids: " f"{[qid for qid in all_qids if all_qids.count(qid) > 1]}"
            )


# ---------------------------------------------------------------------------
# Property 6: Assignment Version Monotonicity
# ---------------------------------------------------------------------------


class TestAssignmentVersionMonotonicity:
    """**Validates: Requirements 4.2**

    Property 6: version numbers increase monotonically.
    """

    @given(data=handler_fixture_strategy())
    @settings(deadline=None)
    def test_successive_runs_increment_version(
        self,
        data: tuple[list[dict], dict, dict[str, dict], dict],
    ) -> None:
        """Running the handler twice must produce version 1 then version 2,
        with the second version strictly greater than the first."""
        queries, collector_output, analysis_outputs, triage = data
        db = "testdb"
        job_id = "test-job"

        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalArtifactStore(tmpdir)
            _seed_artifacts(store, db, job_id, collector_output, analysis_outputs, triage)

            # First run → version 1
            run_assignment_resolver(job_id, db, store)
            v1 = store.read_json(f"{db}/{job_id}/assignment/v1/assignment.json")

            # Second run → version 2
            run_assignment_resolver(job_id, db, store)
            v2 = store.read_json(f"{db}/{job_id}/assignment/v2/assignment.json")

            assert (
                v2["version"] > v1["version"]
            ), f"Version did not increase: v1={v1['version']}, v2={v2['version']}"
            assert v1["version"] == 1
            assert v2["version"] == 2


# ---------------------------------------------------------------------------
# Property 19: Versioned Artifact Immutability
# ---------------------------------------------------------------------------


class TestVersionedArtifactImmutability:
    """**Validates: Requirements 4.3**

    Property 19: versioned artifacts written to /v{N}/ paths.
    """

    @given(data=handler_fixture_strategy())
    @settings(deadline=None)
    def test_artifacts_written_to_versioned_paths(
        self,
        data: tuple[list[dict], dict, dict[str, dict], dict],
    ) -> None:
        """The handler must write assignment and validation artifacts
        under /v{N}/ versioned paths."""
        queries, collector_output, analysis_outputs, triage = data
        db = "testdb"
        job_id = "test-job"

        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalArtifactStore(tmpdir)
            _seed_artifacts(store, db, job_id, collector_output, analysis_outputs, triage)

            run_assignment_resolver(job_id, db, store)

            assignment_path = f"{db}/{job_id}/assignment/v1/assignment.json"
            validation_path = f"{db}/{job_id}/assignment/v1/validation.json"

            assert store.exists(
                assignment_path
            ), f"Assignment artifact not found at versioned path: {assignment_path}"
            assert store.exists(
                validation_path
            ), f"Validation artifact not found at versioned path: {validation_path}"

            # Verify paths contain /v{N}/ pattern
            assert "/v1/" in assignment_path
            assert "/v1/" in validation_path

    @given(data=handler_fixture_strategy())
    @settings(deadline=None)
    def test_previous_version_not_overwritten(
        self,
        data: tuple[list[dict], dict, dict[str, dict], dict],
    ) -> None:
        """Running the handler a second time must NOT overwrite the v1
        artifact — it must create v2 instead."""
        queries, collector_output, analysis_outputs, triage = data
        db = "testdb"
        job_id = "test-job"

        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalArtifactStore(tmpdir)
            _seed_artifacts(store, db, job_id, collector_output, analysis_outputs, triage)

            # First run
            run_assignment_resolver(job_id, db, store)
            v1_data = store.read_json(f"{db}/{job_id}/assignment/v1/assignment.json")

            # Second run
            run_assignment_resolver(job_id, db, store)

            # v1 must still exist and be unchanged
            v1_after = store.read_json(f"{db}/{job_id}/assignment/v1/assignment.json")
            assert v1_data["version"] == v1_after["version"] == 1
            assert v1_data["query_assignments"] == v1_after["query_assignments"]

            # v2 must exist at a separate path
            assert store.exists(f"{db}/{job_id}/assignment/v2/assignment.json")


# ---------------------------------------------------------------------------
# The #2152 refusal: a statement on an Aurora engine that was not analysed
# ---------------------------------------------------------------------------

# The job Hypothesis drew in #2368: one statement on a ``sys`` table, and only
# DynamoDB analysed. The statement falls back to Aurora.
_SYSTEM_TABLE_JOB: tuple[str, tuple[list[dict], dict, dict[str, dict], dict]] = (
    "sys.users",
    (
        [],
        {
            "job_id": "test-job",
            "database_schema": {"tables": [{"table_id": "ab.cd", "table_name": "cd"}]},
            "queries": {"query_patterns": []},
        },
        {
            "dynamodb": {
                "table_recommendations": [],
                "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
            }
        },
        {"selected_agents": [{"agent_type": "dynamodb", "reasons": ["signal"]}]},
    ),
)


def _without_aurora(
    data: tuple[list[dict], dict, dict[str, dict], dict],
) -> tuple[list[dict], dict, dict[str, dict], dict]:
    """The job with Aurora neither selected nor analysed (DynamoDB stands in
    when Aurora was the only engine)."""
    queries, collector_output, analysis_outputs, triage = data
    analysis = {e: a for e, a in analysis_outputs.items() if e != "aurora"}
    if not analysis:
        analysis = {"dynamodb": {"table_recommendations": [], "workload_analysis": {}}}
    triage = {"selected_agents": [{"agent_type": e, "reasons": ["signal"]} for e in analysis]}
    return queries, collector_output, analysis, triage


@pytest.mark.passes_on_main  # guards the #2152 refusal main already has
class TestUnanalysedAuroraIsRefused:
    """A job the strategy drew before #2368: a statement that reads only a
    system table falls back to Aurora, and Aurora has no analysis. The handler
    must refuse it and write nothing."""

    @given(system_table=_system_table_id, data=handler_fixture_strategy())
    @example(system_table=_SYSTEM_TABLE_JOB[0], data=_SYSTEM_TABLE_JOB[1])
    @settings(deadline=None)
    def test_a_statement_on_unanalysed_aurora_is_refused_and_nothing_is_written(
        self,
        system_table: str,
        data: tuple[list[dict], dict, dict[str, dict], dict],
    ) -> None:
        queries, collector_output, analysis_outputs, triage = _without_aurora(data)
        catalog_read = {
            "query_id": "q-system",
            "query_text": "SELECT ... FROM ...",
            "query_type": "SELECT",
            "tables_accessed": [system_table],
            "join_count": 0,
            "has_joins": False,
            "has_aggregations": False,
            "filter_tables": [],
            "calls_per_second": 1.0,
            "rows_returned_avg": 10,
        }
        collector_output = {
            **collector_output,
            "queries": {"query_patterns": [*queries, catalog_read]},
        }
        db = "testdb"
        job_id = "test-job"

        with tempfile.TemporaryDirectory() as tmpdir:
            store = LocalArtifactStore(tmpdir)
            _seed_artifacts(store, db, job_id, collector_output, analysis_outputs, triage)
            seeded = sorted(store.list_prefix(f"{db}/{job_id}/"))

            with pytest.raises(InvalidAssignment, match="Aurora has no analysis"):
                run_assignment_resolver(job_id, db, store)

            assert sorted(store.list_prefix(f"{db}/{job_id}/")) == seeded
