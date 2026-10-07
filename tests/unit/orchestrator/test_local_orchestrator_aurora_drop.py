"""#381 review round 2: drop the losing Aurora engine from post-schema routing's
active engine set.

Triage selects both ``aurora_mysql`` and ``aurora_postgresql`` for a heterogeneous
source (SQL Server, Oracle, DB2) with no Aurora dialect of its own, so a caller
working off the triage-selected engine list (``_run_post_schema_routing``) would
otherwise still treat the losing engine as an active target for an orphaned/
unsupported query even after the assignment resolver picked one of the two
(``Assignment.aurora_engine_choice``).
"""

from __future__ import annotations

from unittest.mock import patch

from src.contracts.post_schema_router_output import RouterOutput
from src.orchestrator.local_orchestrator import LocalOrchestrator
from src.storage.local_store import LocalArtifactStore

DB = "adventureworks"
JOB = "job-381"


def _store(tmp_path) -> LocalArtifactStore:
    return LocalArtifactStore(base_dir=str(tmp_path))


class TestDropLosingAuroraEngine:
    """Direct unit tests of the helper method."""

    def test_drops_the_loser_when_a_choice_is_recorded(self, tmp_path) -> None:
        store = _store(tmp_path)
        store.write_json(
            f"{DB}/{JOB}/assignment/v1/assignment.json",
            {"aurora_engine_choice": {"engine": "aurora_postgresql"}},
        )
        orch = LocalOrchestrator(store)
        result = orch._drop_losing_aurora_engine(
            JOB, DB, ["aurora_mysql", "aurora_postgresql", "dynamodb"], 1
        )
        assert result == ["aurora_postgresql", "dynamodb"]

    def test_no_op_without_an_assignment(self, tmp_path) -> None:
        store = _store(tmp_path)
        orch = LocalOrchestrator(store)
        result = orch._drop_losing_aurora_engine(JOB, DB, ["aurora_mysql", "aurora_postgresql"], 0)
        assert result == ["aurora_mysql", "aurora_postgresql"]

    def test_no_op_without_a_recorded_choice(self, tmp_path) -> None:
        store = _store(tmp_path)
        store.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", {})
        orch = LocalOrchestrator(store)
        result = orch._drop_losing_aurora_engine(JOB, DB, ["aurora_mysql", "aurora_postgresql"], 1)
        assert result == ["aurora_mysql", "aurora_postgresql"]

    def test_no_op_for_a_homogeneous_winner(self, tmp_path) -> None:
        # A homogeneous source's retained engine is not one of the two competing
        # Aurora engines at all -- nothing to drop.
        store = _store(tmp_path)
        store.write_json(
            f"{DB}/{JOB}/assignment/v1/assignment.json",
            {"aurora_engine_choice": {"engine": "dynamodb"}},
        )
        orch = LocalOrchestrator(store)
        result = orch._drop_losing_aurora_engine(JOB, DB, ["dynamodb", "opensearch"], 1)
        assert result == ["dynamodb", "opensearch"]


class TestPostSchemaRoutingExcludesTheLoser:
    """Integration: _run_post_schema_routing passes the trimmed engine set through
    to route_unsupported_queries as active_engines."""

    def test_active_engines_excludes_the_losing_aurora_engine(self, tmp_path) -> None:
        store = _store(tmp_path)
        store.write_json(
            f"{DB}/{JOB}/referee-triage/triage.json",
            {
                "selected_agents": [
                    {"agent_type": "aurora_mysql"},
                    {"agent_type": "aurora_postgresql"},
                ]
            },
        )
        store.write_json(
            f"{DB}/{JOB}/assignment/v1/assignment.json",
            {"aurora_engine_choice": {"engine": "aurora_postgresql"}},
        )
        store.write_json(f"{DB}/{JOB}/collector/output.json", {"queries": {"query_patterns": []}})
        for engine in ("aurora_mysql", "aurora_postgresql"):
            store.write_json(
                f"{DB}/{JOB}/schema-{engine}/v1/schema_output.json",
                {"unsupported_patterns": []},
            )

        orch = LocalOrchestrator(store)
        captured: dict = {}

        def _fake_route(**kwargs):
            captured.update(kwargs)
            return RouterOutput(job_id=JOB, routings=[], terminal_queries=[], cascade_depth=0)

        with patch(
            "src.agents.referee.post_schema_router.route_unsupported_queries",
            side_effect=_fake_route,
        ):
            orch._run_post_schema_routing(JOB, DB)

        assert captured["active_engines"] == ["aurora_postgresql"]
