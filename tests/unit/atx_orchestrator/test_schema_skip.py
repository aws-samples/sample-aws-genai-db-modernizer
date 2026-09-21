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

import pytest

from src.atx_orchestrator import tools


@pytest.fixture(autouse=True)
def _assignment_review_approved():
    """These tests exercise the schema skip/dispatch logic, which is downstream of
    the ADR-028 assignment-review gate. Treat the gate as approved so every test
    reaches the behavior it targets rather than the gate's block."""
    with patch("src.atx_orchestrator.tools._assignment_review_approved", return_value=True):
        yield


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
            # No existing schema output -> the idempotent skip is bypassed and we
            # reach the routed pre-dispatch skip.
            patch("src.atx_orchestrator.tools._make_store", return_value=_FakeStore({})),
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
        # Suffix aurora-pg maps to engine aurora_postgresql. aurora_postgresql
        # now has an implemented designer, so it passes the designer gate and
        # this mapping is exercised via the routing skip instead: no in-scope
        # queries routed to it -> skipped, with the plan step and payload using
        # the engine name.
        with (
            patch("src.atx_orchestrator.tools._effective_assignment_version", return_value=2),
            patch(
                "src.atx_orchestrator.tools._engines_with_in_scope_queries",
                return_value={"dynamodb"},  # aurora_postgresql absent -> skip
            ),
            patch("src.atx_orchestrator.tools.invoke_and_wait") as mock_invoke,
            patch("src.atx_orchestrator.tools.mark_step_skipped") as mock_skip,
            patch("src.atx_orchestrator.tools.mark_step_running") as mock_running,
        ):
            out = tools._run_schema_design_via_a2a("aurora-pg", "job-1", "discourse")

        mock_invoke.assert_not_called()
        mock_running.assert_not_called()
        assert mock_skip.call_args.args[0] == "schema_aurora_postgresql"
        assert json.loads(out)["target_type"] == "aurora_postgresql"


# =============================================================================
# _run_schema_design_via_a2a — implemented-designer skip
#
# All six target engines now have real designers (dynamodb, documentdb,
# elasticache, opensearch, aurora_postgresql, aurora_mysql), so the
# `engine not in IMPLEMENTED_SCHEMA_DESIGNERS` branch in
# _run_schema_design_via_a2a is unreachable for any real engine — a
# non-registered engine string would fail the earlier
# `_SCHEMA_ENGINES[suffix]` lookup first. The two tests that used to exercise
# this gate's "no implemented designer" outcomes for aurora_mysql (the
# same-family "no redesign" note and the cross-family "not covered" warning)
# were removed: that classification is no longer reached through this
# pre-dispatch path for aurora_mysql. The equivalent classification is still
# tested post-dispatch, via schema_no_design_notes / run_schema_design_core's
# empty-design path — see test_schema_design_core.py's TestSameFamily and
# TestHeterogeneousSource. A test asserting the designer-gate-before-routing-
# skip ordering was also removed, since no real engine fails the designer gate
# anymore to demonstrate it.
#
# What remains reachable and tested below: the gate lets an implemented engine
# through (test_implemented_engine_not_skipped_by_designer_gate), and
# aurora_mysql — like aurora_postgresql — now falls through the designer gate
# to the routing skip / real dispatch instead of being caught by it
# (test_aurora_mysql_suffix_maps_to_engine_name).


class TestSchemaDesignImplementedDesignerSkip:
    def test_implemented_engine_not_skipped_by_designer_gate(self) -> None:
        # dynamodb has a real designer, so the designer gate lets it through; with
        # routed queries it dispatches normally.
        with (
            patch("src.atx_orchestrator.tools._effective_assignment_version", return_value=1),
            patch(
                "src.atx_orchestrator.tools._engines_with_in_scope_queries",
                return_value={"dynamodb"},
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

    def test_aurora_mysql_suffix_maps_to_engine_name(self) -> None:
        # Suffix aurora-mysql maps to engine aurora_mysql. aurora_mysql now has
        # an implemented designer, so it passes the designer gate and this
        # mapping is exercised via the routing skip instead: no in-scope
        # queries routed to it -> skipped, with the plan step and payload using
        # the engine name.
        with (
            patch("src.atx_orchestrator.tools._effective_assignment_version", return_value=2),
            patch(
                "src.atx_orchestrator.tools._engines_with_in_scope_queries",
                return_value={"dynamodb"},  # aurora_mysql absent -> skip
            ),
            patch("src.atx_orchestrator.tools.invoke_and_wait") as mock_invoke,
            patch("src.atx_orchestrator.tools.mark_step_skipped") as mock_skip,
            patch("src.atx_orchestrator.tools.mark_step_running") as mock_running,
        ):
            out = tools._run_schema_design_via_a2a("aurora-mysql", "job-1", "discourse")

        mock_invoke.assert_not_called()
        mock_running.assert_not_called()
        assert mock_skip.call_args.args[0] == "schema_aurora_mysql"
        assert json.loads(out)["target_type"] == "aurora_mysql"


# =============================================================================
# _run_schema_design_via_a2a — consolidated `schema` agent dispatch (ADR-027)
#
# All six engine tools resolve to the one `schema` agent id, and the target
# engine travels in the invocation payload as target_type rather than in the
# agent id.


class TestSchemaDesignConsolidatedDispatch:
    def test_dispatch_uses_one_agent_id_and_payload_target_type(self) -> None:
        with (
            patch("src.atx_orchestrator.tools._effective_assignment_version", return_value=3),
            patch(
                "src.atx_orchestrator.tools._engines_with_in_scope_queries",
                return_value={"opensearch"},
            ),
            patch(
                "src.atx_orchestrator.tools.invoke_and_wait", return_value={"status": "complete"}
            ) as mock_invoke,
            patch("src.atx_orchestrator.tools.mark_step_skipped"),
            patch("src.atx_orchestrator.tools.mark_step_running"),
            patch("src.atx_orchestrator.tools.mark_step_succeeded"),
        ):
            tools._run_schema_design_via_a2a("opensearch", "job-1", "discourse")

        agent_id = mock_invoke.call_args.args[0]
        message = json.loads(mock_invoke.call_args.args[1])
        assert agent_id == f"{tools._AGENT_PREFIX}-schema"  # not ...-schema-opensearch
        assert message["target_type"] == "opensearch"
        assert message["assignment_version"] == 3
        assert message["database_name"] == "discourse"

    def test_aurora_pg_suffix_becomes_engine_target_type_in_payload(self) -> None:
        # The hyphenated tool suffix (aurora-pg) maps to the engine identifier
        # (aurora_postgresql) the subagent validates against. aurora_postgresql
        # now has an implemented designer, so this is exercised via a real
        # dispatch, the same way opensearch is above.
        with (
            patch("src.atx_orchestrator.tools._effective_assignment_version", return_value=1),
            patch(
                "src.atx_orchestrator.tools._engines_with_in_scope_queries",
                return_value={"aurora_postgresql"},
            ),
            patch(
                "src.atx_orchestrator.tools.invoke_and_wait", return_value={"status": "complete"}
            ) as mock_invoke,
            patch("src.atx_orchestrator.tools.mark_step_skipped"),
            patch("src.atx_orchestrator.tools.mark_step_running"),
            patch("src.atx_orchestrator.tools.mark_step_succeeded"),
        ):
            out = tools._run_schema_design_via_a2a("aurora-pg", "job-1", "discourse")

        message = json.loads(mock_invoke.call_args.args[1])
        assert message["target_type"] == "aurora_postgresql"
        assert json.loads(out) == {"status": "complete"}


# =============================================================================
# _run_schema_design_via_a2a — idempotent skip (restore-safe reuse)
#
# If this engine's schema output already exists for the effective assignment
# version, reuse it instead of re-invoking. Makes an orchestrator recycle mid-
# design harmless (the subagent completes and writes output independently) and
# stops the re-dispatch loop a long design + a recycle would otherwise cause.


class TestSchemaDesignIdempotentSkip:
    def test_reuses_existing_output_without_reinvoking(self) -> None:
        key = "discourse/job-1/schema-dynamodb/v2/schema_output.json"
        store = _FakeStore({key: {"target_type": "dynamodb", "status": "completed", "tables": []}})
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.tools._effective_assignment_version", return_value=2),
            patch("src.atx_orchestrator.tools.invoke_and_wait") as mock_invoke,
            patch("src.atx_orchestrator.tools.mark_step_succeeded") as mock_succeeded,
        ):
            out = json.loads(tools._run_schema_design_via_a2a("dynamodb", "job-1", "discourse"))

        assert out["reused_existing"] is True
        assert out["status"] == "completed"  # preserved from the existing output
        mock_invoke.assert_not_called()  # no re-design
        mock_succeeded.assert_called_once()

    def test_dispatches_when_no_existing_output(self) -> None:
        store = _FakeStore(
            {"discourse/job-1/assignment/v2/assignment.json": _assignment(["dynamodb"])}
        )
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.tools._effective_assignment_version", return_value=2),
            patch(
                "src.atx_orchestrator.tools._engines_with_in_scope_queries",
                return_value={"dynamodb"},
            ),
            patch(
                "src.atx_orchestrator.tools.invoke_and_wait", return_value={"ok": True}
            ) as mock_invoke,
        ):
            out = json.loads(tools._run_schema_design_via_a2a("dynamodb", "job-1", "discourse"))

        mock_invoke.assert_called_once()  # no existing output -> designs normally
        assert out == {"ok": True}

    def test_dispatch_uses_multi_hour_timeout(self, monkeypatch) -> None:
        # Schema design is the longest phase; the heaviest engines (e.g.
        # ElastiCache) can run past the generic 30-min A2A default. The dispatch
        # must pass the multi-hour timeout so a slow but healthy run is not marked
        # FAILED at 1800s.
        monkeypatch.delenv("SCHEMA_DESIGN_A2A_TIMEOUT_SECONDS", raising=False)
        store = _FakeStore(
            {"discourse/job-1/assignment/v2/assignment.json": _assignment(["elasticache"])}
        )
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.tools._effective_assignment_version", return_value=2),
            patch(
                "src.atx_orchestrator.tools._engines_with_in_scope_queries",
                return_value={"elasticache"},
            ),
            patch(
                "src.atx_orchestrator.tools.invoke_and_wait", return_value={"ok": True}
            ) as mock_invoke,
        ):
            tools._run_schema_design_via_a2a("elasticache", "job-1", "discourse")

        mock_invoke.assert_called_once()
        assert mock_invoke.call_args.kwargs["timeout"] == 4 * 60 * 60
