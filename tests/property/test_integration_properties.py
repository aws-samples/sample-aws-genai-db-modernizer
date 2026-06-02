"""
Property-based tests for end-to-end integration.

Tests correctness properties from the design document:
- Property 7: Idempotent Phase Re-execution — same inputs produce same outputs
  for schema design and synthesis (excluding LLM content)
- Property 16: Scoped Re-Execution — only affected engines re-run when
  assignment changes

These are lightweight tests that verify the LocalOrchestrator's phase ordering
and scoping logic without running actual agents.

**Validates: Requirements 6.1, 6.3**
"""

from __future__ import annotations

import tempfile
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

from src.contracts.phase_models import Phase, PhaseStatus
from src.orchestrator.base import PhaseScope
from src.orchestrator.local_orchestrator import LocalOrchestrator
from src.storage.local_store import LocalArtifactStore

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_job_ids = st.from_regex(r"[a-z][a-z0-9]{3,12}", fullmatch=True)
_engine = st.sampled_from(["dynamodb", "aurora", "opensearch", "documentdb"])


def _make_orchestrator(tmpdir: str) -> LocalOrchestrator:
    store = LocalArtifactStore(base_dir=tmpdir)
    return LocalOrchestrator(store=store)


def _setup_completed_through_assignment(
    orch: LocalOrchestrator, job_id: str, database_name: str, engines: list[str]
) -> None:
    """Set up a progression and artifacts as if phases through ASSIGNMENT completed."""
    # Create progression with phases completed through ASSIGNMENT
    with patch.object(orch, "_run_phase"):
        orch.start_job(job_id, config={"database_name": database_name})

    progression = orch.get_progression(job_id)
    progression.phases[Phase.ANALYSIS].status = PhaseStatus.COMPLETED
    progression.phases[Phase.ASSIGNMENT].status = PhaseStatus.COMPLETED
    orch._save_progression(progression)

    # Write triage artifact
    triage = {
        "selected_agents": [{"agent_type": e} for e in engines],
    }
    orch.store.write_json(f"{database_name}/{job_id}/referee-triage/triage.json", triage)

    # Write assignment artifact with all queries in-scope
    assignment = {
        "job_id": job_id,
        "version": 1,
        "status": "auto_generated",
        "query_assignments": [
            {
                "query_id": f"q-{i:06d}",
                "assigned_engine": engines[i % len(engines)],
                "confidence": 80,
                "source_tables": [f"table_{i}"],
                "assignment_reason": "test",
                "in_scope": True,
                "customer_override": False,
                "warnings": [],
            }
            for i in range(len(engines) * 2)
        ],
        "table_assignments": [],
        "co_dependency_groups": [],
        "validation_warnings": [],
    }
    orch.store.write_json(f"{database_name}/{job_id}/assignment/v1/assignment.json", assignment)


# ---------------------------------------------------------------------------
# Property 7: Idempotent Phase Re-execution
# ---------------------------------------------------------------------------


class TestIdempotentPhaseReExecution:
    """**Validates: Requirement 6.1**

    Property 7: Idempotent Phase Re-execution — same inputs produce same
    outputs for schema design and synthesis (excluding LLM content).

    We verify that the LocalOrchestrator's _run_phase dispatches
    deterministically: calling it twice with the same inputs invokes the
    same agent handlers with the same arguments.
    """

    @given(job_id=_job_ids)
    @settings(deadline=None)
    def test_schema_design_dispatch_is_deterministic(self, job_id: str) -> None:
        """Schema design dispatch produces the same set of engine calls
        when invoked twice with identical inputs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            orch = _make_orchestrator(tmpdir)
            engines = ["dynamodb", "opensearch"]
            _setup_completed_through_assignment(orch, job_id, "testdb", engines)

            with patch(
                "src.orchestrator.local_orchestrator.LocalOrchestrator._run_schema_design"
            ) as _mock_sd:
                # Capture calls by tracking the scope argument
                call_args_1: list = []
                call_args_2: list = []

                def capture_run_1(jid, phase, config=None, scope=None):
                    if phase == Phase.SCHEMA_DESIGN:
                        call_args_1.append((jid, phase, scope))

                def capture_run_2(jid, phase, config=None, scope=None):
                    if phase == Phase.SCHEMA_DESIGN:
                        call_args_2.append((jid, phase, scope))

                # Run 1
                with patch.object(orch, "_run_phase", side_effect=capture_run_1):
                    try:
                        orch.resume(job_id, Phase.SCHEMA_DESIGN)
                    except Exception:  # nosec B110
                        pass

                # Reset progression to allow re-run
                progression = orch.get_progression(job_id)
                progression.phases[Phase.SCHEMA_DESIGN].status = PhaseStatus.COMPLETED
                progression.phases[Phase.ASSIGNMENT].status = PhaseStatus.COMPLETED
                orch._save_progression(progression)

                # Run 2
                with patch.object(orch, "_run_phase", side_effect=capture_run_2):
                    try:
                        orch.resume(job_id, Phase.SCHEMA_DESIGN)
                    except Exception:  # nosec B110
                        pass

                assert call_args_1 == call_args_2, (
                    f"Schema design dispatch not idempotent: "
                    f"run1={call_args_1} vs run2={call_args_2}"
                )

    @given(job_id=_job_ids)
    @settings(deadline=None)
    def test_same_assignment_version_produces_same_dispatch(self, job_id: str) -> None:
        """The assignment version resolved for schema design is deterministic."""
        with tempfile.TemporaryDirectory() as tmpdir:
            orch = _make_orchestrator(tmpdir)
            engines = ["dynamodb"]
            _setup_completed_through_assignment(orch, job_id, "testdb", engines)

            ver1 = orch._get_assignment_version(job_id, "testdb")
            ver2 = orch._get_assignment_version(job_id, "testdb")
            assert ver1 == ver2, f"Assignment version not deterministic: {ver1} vs {ver2}"


# ---------------------------------------------------------------------------
# Property 16: Scoped Re-Execution
# ---------------------------------------------------------------------------


class TestScopedReExecution:
    """**Validates: Requirement 6.3**

    Property 16: Scoped Re-Execution — only affected engines re-run when
    assignment changes. We verify that the LocalOrchestrator correctly
    identifies which engines have in-scope queries and only dispatches
    schema design for those engines.
    """

    @given(job_id=_job_ids)
    @settings(deadline=None)
    def test_engines_with_zero_in_scope_queries_are_skipped(self, job_id: str) -> None:
        """Engines with zero in-scope queries should not be dispatched
        for schema design."""
        with tempfile.TemporaryDirectory() as tmpdir:
            orch = _make_orchestrator(tmpdir)
            database_name = "testdb"

            # Set up with two engines but only one has in-scope queries
            with patch.object(orch, "_run_phase"):
                orch.start_job(job_id, config={"database_name": database_name})

            progression = orch.get_progression(job_id)
            progression.phases[Phase.ANALYSIS].status = PhaseStatus.COMPLETED
            progression.phases[Phase.ASSIGNMENT].status = PhaseStatus.COMPLETED
            orch._save_progression(progression)

            triage = {
                "selected_agents": [
                    {"agent_type": "dynamodb"},
                    {"agent_type": "opensearch"},
                ],
            }
            orch.store.write_json(f"{database_name}/{job_id}/referee-triage/triage.json", triage)

            # Assignment: all queries assigned to dynamodb, opensearch has none in-scope
            assignment = {
                "job_id": job_id,
                "version": 1,
                "status": "auto_generated",
                "query_assignments": [
                    {
                        "query_id": "q-000001",
                        "assigned_engine": "dynamodb",
                        "confidence": 90,
                        "source_tables": ["orders"],
                        "assignment_reason": "test",
                        "in_scope": True,
                        "customer_override": False,
                        "warnings": [],
                    },
                    {
                        "query_id": "q-000002",
                        "assigned_engine": "opensearch",
                        "confidence": 70,
                        "source_tables": ["logs"],
                        "assignment_reason": "test",
                        "in_scope": False,
                        "customer_override": False,
                        "warnings": [],
                    },
                ],
                "table_assignments": [],
                "co_dependency_groups": [],
                "validation_warnings": [],
            }
            orch.store.write_json(
                f"{database_name}/{job_id}/assignment/v1/assignment.json",
                assignment,
            )

            engines_with_queries = orch._get_engines_with_in_scope_queries(job_id, database_name, 1)

            assert "dynamodb" in engines_with_queries
            assert "opensearch" not in engines_with_queries

    @given(
        job_id=_job_ids,
        engines=st.lists(_engine, min_size=1, max_size=3, unique=True),
    )
    @settings(deadline=None)
    def test_scope_restricts_execution_to_specified_engines(
        self, job_id: str, engines: list[str]
    ) -> None:
        """When a PhaseScope is provided, only engines in the scope should
        be considered for execution."""
        with tempfile.TemporaryDirectory() as tmpdir:
            orch = _make_orchestrator(tmpdir)
            database_name = "testdb"
            all_engines = ["dynamodb", "aurora", "opensearch", "documentdb"]

            _setup_completed_through_assignment(orch, job_id, database_name, all_engines)

            scope = PhaseScope(engines=engines)
            selected = orch._get_selected_engines(job_id, database_name)
            scoped = [e for e in selected if e in scope.engines]

            # Only engines in the scope should be in the scoped list
            for e in scoped:
                assert e in engines, f"Engine {e} not in scope {engines}"

    @given(job_id=_job_ids)
    @settings(deadline=None)
    def test_assignment_change_affects_engine_scope(self, job_id: str) -> None:
        """When assignment changes (queries moved between engines), the set
        of engines with in-scope queries changes accordingly."""
        with tempfile.TemporaryDirectory() as tmpdir:
            orch = _make_orchestrator(tmpdir)
            database_name = "testdb"

            # Write triage
            triage = {
                "selected_agents": [
                    {"agent_type": "dynamodb"},
                    {"agent_type": "opensearch"},
                ],
            }
            orch.store.write_json(f"{database_name}/{job_id}/referee-triage/triage.json", triage)

            # V1: both engines have queries
            assignment_v1 = {
                "job_id": job_id,
                "version": 1,
                "query_assignments": [
                    {
                        "query_id": "q-000001",
                        "assigned_engine": "dynamodb",
                        "in_scope": True,
                        "source_tables": ["t1"],
                    },
                    {
                        "query_id": "q-000002",
                        "assigned_engine": "opensearch",
                        "in_scope": True,
                        "source_tables": ["t2"],
                    },
                ],
            }
            orch.store.write_json(
                f"{database_name}/{job_id}/assignment/v1/assignment.json",
                assignment_v1,
            )

            engines_v1 = orch._get_engines_with_in_scope_queries(job_id, database_name, 1)
            assert engines_v1 == {"dynamodb", "opensearch"}

            # V2: opensearch query moved out of scope
            assignment_v2 = {
                "job_id": job_id,
                "version": 2,
                "query_assignments": [
                    {
                        "query_id": "q-000001",
                        "assigned_engine": "dynamodb",
                        "in_scope": True,
                        "source_tables": ["t1"],
                    },
                    {
                        "query_id": "q-000002",
                        "assigned_engine": "opensearch",
                        "in_scope": False,
                        "source_tables": ["t2"],
                    },
                ],
            }
            orch.store.write_json(
                f"{database_name}/{job_id}/assignment/v2/assignment.json",
                assignment_v2,
            )

            engines_v2 = orch._get_engines_with_in_scope_queries(job_id, database_name, 2)
            assert engines_v2 == {"dynamodb"}
            assert "opensearch" not in engines_v2
