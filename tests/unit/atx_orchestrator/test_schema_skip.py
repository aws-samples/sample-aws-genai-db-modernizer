"""Tests for marking unselected schema-design engines as skipped (STOPPED).

Pins the WebApp progress-panel fix: engines that had no in-scope queries routed
to them (never selected, or consolidated away by Reality Check) get their
``schema_<engine>`` sub-step marked STOPPED, instead of sitting at NOT_STARTED
(a perpetual pending clock). Selected engines are read from the effective-version
assignment artifact.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from src.atx_orchestrator import tools


class _FakeStore:
    """Minimal store: holds assignment JSON keyed by path."""

    def __init__(self, objects: dict[str, dict]) -> None:
        self._objects = objects

    def exists(self, path: str) -> bool:
        return path in self._objects

    def read_json(self, path: str) -> dict:
        return self._objects[path]


def _assignment(engines_in_scope: list[str], out_of_scope: list[str] | None = None) -> dict:
    qas = [
        {"query_id": f"q{i}", "assigned_engine": e, "in_scope": True}
        for i, e in enumerate(engines_in_scope)
    ]
    for i, e in enumerate(out_of_scope or []):
        qas.append({"query_id": f"o{i}", "assigned_engine": e, "in_scope": False})
    return {"query_assignments": qas}


# =============================================================================
# _engines_with_in_scope_queries


class TestEnginesWithInScopeQueries:
    def test_unions_in_scope_assigned_engines(self) -> None:
        store = _FakeStore(
            {
                "discourse/job-1/assignment/v1/assignment.json": _assignment(
                    ["dynamodb", "opensearch", "dynamodb"], out_of_scope=["aurora_mysql"]
                )
            }
        )
        with patch("src.atx_orchestrator.tools._make_store", return_value=store):
            got = tools._engines_with_in_scope_queries("job-1", "discourse", 1)
        assert got == {"dynamodb", "opensearch"}  # out-of-scope aurora_mysql excluded

    def test_missing_artifact_returns_empty(self) -> None:
        with patch("src.atx_orchestrator.tools._make_store", return_value=_FakeStore({})):
            assert tools._engines_with_in_scope_queries("job-1", "discourse", 1) == set()

    def test_read_error_fails_open_to_empty(self) -> None:
        class _Boom:
            def exists(self, path):
                return True

            def read_json(self, path):
                raise RuntimeError("s3 down")

        with patch("src.atx_orchestrator.tools._make_store", return_value=_Boom()):
            assert tools._engines_with_in_scope_queries("job-1", "discourse", 1) == set()


# =============================================================================
# _mark_unselected_schema_steps_skipped


class TestMarkUnselectedSchemaStepsSkipped:
    def test_marks_only_unselected_engines(self) -> None:
        # Selected: dynamodb + aurora_postgresql. The other four must be skipped.
        store = _FakeStore(
            {
                "discourse/job-1/assignment/v1/assignment.json": _assignment(
                    ["dynamodb", "aurora_postgresql"]
                )
            }
        )
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.tools._effective_assignment_version", return_value=1),
            patch("src.atx_orchestrator.tools.mark_step_skipped") as mock_skip,
        ):
            tools._mark_unselected_schema_steps_skipped("job-1", "discourse")

        skipped_labels = {c.args[0] for c in mock_skip.call_args_list}
        assert skipped_labels == {
            "schema_documentdb",
            "schema_elasticache",
            "schema_opensearch",
            "schema_aurora_mysql",
        }
        # Selected engines are NOT marked skipped.
        assert "schema_dynamodb" not in skipped_labels
        assert "schema_aurora_postgresql" not in skipped_labels

    def test_no_selected_engines_marks_nothing(self) -> None:
        # Cannot resolve the selected set -> leave the plan untouched (do not
        # wrongly mark all six skipped).
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=_FakeStore({})),
            patch("src.atx_orchestrator.tools._effective_assignment_version", return_value=1),
            patch("src.atx_orchestrator.tools.mark_step_skipped") as mock_skip,
        ):
            tools._mark_unselected_schema_steps_skipped("job-1", "discourse")

        mock_skip.assert_not_called()

    def test_all_engines_selected_marks_nothing(self) -> None:
        store = _FakeStore(
            {
                "discourse/job-1/assignment/v1/assignment.json": _assignment(
                    [
                        "dynamodb",
                        "documentdb",
                        "elasticache",
                        "opensearch",
                        "aurora_postgresql",
                        "aurora_mysql",
                    ]
                )
            }
        )
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.tools._effective_assignment_version", return_value=1),
            patch("src.atx_orchestrator.tools.mark_step_skipped") as mock_skip,
        ):
            tools._mark_unselected_schema_steps_skipped("job-1", "discourse")

        mock_skip.assert_not_called()


# =============================================================================
# run_synthesis_via_a2a invokes the skip-marking before closing the schema box


class TestSynthesisMarksSkips:
    def test_synthesis_marks_unselected_schema_steps(self) -> None:
        with (
            patch("src.atx_orchestrator.tools.invoke_and_wait", return_value={"ok": 1}),
            patch("src.atx_orchestrator.tools._publish_synthesis_deliverables"),
            patch("src.atx_orchestrator.tools._effective_assignment_version", return_value=1),
            patch("src.atx_orchestrator.tools._complete_job_success"),
            patch("src.atx_orchestrator.tools._mark_unselected_schema_steps_skipped") as mock_mark,
            patch("src.atx_orchestrator.tools.mark_step_succeeded"),
            patch("src.atx_orchestrator.tools.mark_step_running"),
        ):
            tools.run_synthesis_via_a2a(job_id="job-1", database_name="discourse")

        mock_mark.assert_called_once_with("job-1", "discourse")


# =============================================================================
# _run_schema_design_via_a2a — pre-dispatch skip
#
# Skip engines with zero in-scope routed queries BEFORE the A2A call, so we don't
# pay an AgentCore runtime cold-start just for the subagent to write a "skipped"
# placeholder. Fail-open: an unresolved (empty) routed set means "unknown" and
# must still dispatch.


class TestSchemaDesignPreDispatchSkip:
    def test_skips_engine_with_no_routed_queries_without_a2a(self) -> None:
        with (
            patch("src.atx_orchestrator.tools._effective_assignment_version", return_value=2),
            patch(
                "src.atx_orchestrator.tools._engines_with_in_scope_queries",
                return_value={"aurora_postgresql"},  # dynamodb absent -> skip
            ),
            patch("src.atx_orchestrator.tools.invoke_and_wait") as mock_invoke,
            patch("src.atx_orchestrator.tools.mark_step_skipped") as mock_skip,
            patch("src.atx_orchestrator.tools.mark_step_running") as mock_running,
        ):
            out = tools._run_schema_design_via_a2a("dynamodb", "job-1", "discourse")

        mock_invoke.assert_not_called()  # no A2A round-trip / runtime cold-start
        mock_running.assert_not_called()  # engine sub-step never flipped to running
        mock_skip.assert_called_once()
        assert mock_skip.call_args.args[0] == "schema_dynamodb"
        payload = json.loads(out)
        assert payload["status"] == "skipped"
        assert payload["skipped_pre_dispatch"] is True
        assert payload["target_type"] == "dynamodb"
        assert payload["assignment_version"] == 2

    def test_dispatches_engine_with_routed_queries(self) -> None:
        with (
            patch("src.atx_orchestrator.tools._effective_assignment_version", return_value=1),
            patch(
                "src.atx_orchestrator.tools._engines_with_in_scope_queries",
                return_value={"dynamodb", "opensearch"},
            ),
            patch(
                "src.atx_orchestrator.tools.invoke_and_wait", return_value={"status": "complete"}
            ) as mock_invoke,
            patch("src.atx_orchestrator.tools.mark_step_skipped") as mock_skip,
            patch("src.atx_orchestrator.tools.mark_step_running"),
            patch("src.atx_orchestrator.tools.mark_step_succeeded"),
        ):
            out = tools._run_schema_design_via_a2a("dynamodb", "job-1", "discourse")

        mock_invoke.assert_called_once()
        mock_skip.assert_not_called()
        assert json.loads(out) == {"status": "complete"}

    def test_fail_open_dispatches_when_routed_set_unresolved(self) -> None:
        # Empty set = "unknown" (assignment unreadable). Must NOT skip; dispatch.
        with (
            patch("src.atx_orchestrator.tools._effective_assignment_version", return_value=1),
            patch("src.atx_orchestrator.tools._engines_with_in_scope_queries", return_value=set()),
            patch(
                "src.atx_orchestrator.tools.invoke_and_wait", return_value={"status": "complete"}
            ) as mock_invoke,
            patch("src.atx_orchestrator.tools.mark_step_skipped") as mock_skip,
            patch("src.atx_orchestrator.tools.mark_step_running"),
            patch("src.atx_orchestrator.tools.mark_step_succeeded"),
        ):
            tools._run_schema_design_via_a2a("dynamodb", "job-1", "discourse")

        mock_invoke.assert_called_once()
        mock_skip.assert_not_called()

    def test_aurora_pg_suffix_maps_to_engine_name(self) -> None:
        # Suffix aurora-pg maps to engine aurora_postgresql; the skip must key off
        # the engine name, and both the plan step and payload use it.
        with (
            patch("src.atx_orchestrator.tools._effective_assignment_version", return_value=2),
            patch(
                "src.atx_orchestrator.tools._engines_with_in_scope_queries",
                return_value={"dynamodb"},  # aurora_postgresql absent -> skip
            ),
            patch("src.atx_orchestrator.tools.invoke_and_wait") as mock_invoke,
            patch("src.atx_orchestrator.tools.mark_step_skipped") as mock_skip,
            patch("src.atx_orchestrator.tools.mark_step_running"),
        ):
            out = tools._run_schema_design_via_a2a("aurora-pg", "job-1", "discourse")

        mock_invoke.assert_not_called()
        assert mock_skip.call_args.args[0] == "schema_aurora_postgresql"
        assert json.loads(out)["target_type"] == "aurora_postgresql"
