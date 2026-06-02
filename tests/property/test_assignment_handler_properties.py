"""
Property-based tests for the Assignment Handler.

Tests correctness properties from the design document:
- Property 1: Assignment Completeness — every query from collector appears in assignment
- Property 6: Assignment Version Monotonicity — version numbers increase monotonically
- Property 19: Versioned Artifact Immutability — versioned artifacts written to /v{N}/ paths

**Validates: Requirements 2.1, 4.2, 4.3**
"""

from __future__ import annotations

import tempfile

from hypothesis import given, settings
from hypothesis import strategies as st

from src.agents.referee.assignment_handler import run_assignment_resolver
from src.storage.local_store import LocalArtifactStore

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_engine = st.sampled_from(["dynamodb", "aurora", "opensearch", "documentdb"])
_table_id = st.from_regex(r"[a-z]{2,6}\.[a-z_]{2,8}", fullmatch=True)
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
        "has_aggregation": has_aggregation,
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
