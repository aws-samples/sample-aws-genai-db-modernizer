"""#833: one rule for a co-dependency group's tie when its members disagree.

ADR-031 Decisions 4 and 12: a group moves as one unit, so it has one tie.
``tie_break.group_tie`` is that rule: the intersection of the ties its tied
members carry (a member with no tie adds no constraint; an empty intersection
is no tie), logged as a warning when they differ. The reality check's
placement and sanity sweep, a model's flag and the post-schema reroute all use
it, so the answer never depends on which member comes first (#853 review).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

import pytest

from src.agents.referee.consolidation_validator import sanity_sweep
from src.agents.referee.model_flags import apply_consolidation_flags, route_flagged_unit
from src.agents.referee.move_ledger import ledger_for
from src.agents.referee.post_schema_router import route_unsupported_queries
from src.agents.referee.query_scoring import ScoringContext
from src.agents.referee.reality_check_place import _place_settled_queries
from src.agents.referee.tie_break import group_tie
from src.contracts.model_flag import ModelFlag

# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


class TestTheRule:
    def test_a_member_with_no_tie_adds_no_constraint(self) -> None:
        ties = {"m1": [], "m2": ["dynamodb", "documentdb"]}
        assert group_tie(["m1", "m2"], ties.get) == ["documentdb", "dynamodb"]
        assert group_tie(["m2", "m1"], ties.get) == ["documentdb", "dynamodb"]

    def test_disagreeing_ties_intersect(self, caplog: pytest.LogCaptureFixture) -> None:
        ties = {
            "m1": ["documentdb", "dynamodb", "aurora_mysql"],
            "m2": ["aurora_mysql", "dynamodb"],
        }
        with caplog.at_level(logging.WARNING, logger="src.agents.referee.tie_break"):
            assert group_tie(["m1", "m2"], ties.get) == ["aurora_mysql", "dynamodb"]
        assert "ties disagree" in caplog.text
        assert "m1" in caplog.text and "m2" in caplog.text

    def test_no_engine_in_common_is_no_tie(self, caplog: pytest.LogCaptureFixture) -> None:
        ties = {"m1": ["documentdb", "dynamodb"], "m2": ["aurora_mysql", "opensearch"]}
        with caplog.at_level(logging.WARNING, logger="src.agents.referee.tie_break"):
            assert group_tie(["m1", "m2"], ties.get) == []
        assert "no tie" in caplog.text

    def test_matching_ties_are_the_groups_tie_with_no_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        ties = {"m1": ["dynamodb", "documentdb"], "m2": ["documentdb", "dynamodb"]}
        with caplog.at_level(logging.WARNING, logger="src.agents.referee.tie_break"):
            assert group_tie(["m1", "m2"], ties.get) == ["documentdb", "dynamodb"]
        assert caplog.text == ""

    def test_one_engine_in_common_is_no_tie(self, caplog: pytest.LogCaptureFixture) -> None:
        """A tie has at least two engines: one engine common to the members'
        ties settles nothing, so the group is untied and its scores decide
        (#876 review, Important 1)."""
        ties = {"m1": ["documentdb", "dynamodb"], "m2": ["aurora_mysql", "documentdb"]}
        with caplog.at_level(logging.WARNING, logger="src.agents.referee.tie_break"):
            assert group_tie(["m1", "m2"], ties.get) == []
            assert group_tie(["m2", "m1"], ties.get) == []
        assert "ties disagree" in caplog.text

    def test_no_tied_member_is_no_tie(self) -> None:
        no_ties: dict[str, list[str]] = {}
        assert group_tie(["m1", "m2"], no_ties.get) == []
        assert group_tie([], no_ties.get) == []


# ---------------------------------------------------------------------------
# The reality check's placement (#853 review, Important 1)
# ---------------------------------------------------------------------------


def _place(
    qas: list[dict], committed: set[str], trace: dict, query_map: dict | None = None
) -> list[dict]:
    ledger = ledger_for(qas)
    query_map = query_map or {
        qa["query_id"]: {"query_id": qa["query_id"], "query_type": "SELECT"} for qa in qas
    }
    revised, _ = _place_settled_queries(
        qas,
        committed,
        set(),
        {qa["query_id"]: qa["assigned_engine"] for qa in qas},
        {},
        query_map,
        None,
        trace,
        None,
        {},
        {qa["query_id"] for qa in qas if qa.get("tied_engines")},
        {},
        {qa["query_id"]: list(qa["tied_engines"]) for qa in qas if qa.get("tied_engines")},
        ledger=ledger,
    )
    return revised


class TestPlacementTakesAnyMembersTie:
    """The #853 review's probe: group {m1 untied, m2 tied [DocumentDB,
    DynamoDB]}, DocumentDB removed, group scores DynamoDB 80, Aurora MySQL
    85. Decision 12 re-resolves m2's tie among its surviving tied engines
    first, so the group goes to DynamoDB. Before #833 the pool came from the
    trigger member only: with m1 first the group went to Aurora MySQL."""

    SCORES = {"documentdb": 60, "dynamodb": 80, "aurora_mysql": 85}

    def _run(self, order: list[str]) -> dict[str, dict[str, Any]]:
        qas_by_id: dict[str, dict[str, Any]] = {
            "m1": {"query_id": "m1", "assigned_engine": "documentdb"},
            "m2": {
                "query_id": "m2",
                "assigned_engine": "documentdb",
                "tied_engines": ["documentdb", "dynamodb"],
            },
        }
        group = {"key": "abc123abc123", "scores": dict(self.SCORES)}
        trace = {qid: {"engines": dict(self.SCORES), "group": group} for qid in order}
        qas = [qas_by_id[qid] for qid in order]
        revised = _place(qas, {"dynamodb", "aurora_mysql"}, trace)
        return {qa["query_id"]: qa for qa in revised}

    @pytest.mark.parametrize("order", [["m1", "m2"], ["m2", "m1"]])
    def test_the_group_goes_to_the_tied_survivor_in_either_order(self, order: list[str]) -> None:
        by_id = self._run(order)

        assert by_id["m1"]["assigned_engine"] == "dynamodb"
        assert by_id["m2"]["assigned_engine"] == "dynamodb"
        # The tie is settled: DynamoDB is the only surviving tied engine.
        assert by_id["m1"].get("tied_engines", []) == []
        assert by_id["m2"]["tied_engines"] == []

    def test_both_orders_agree_on_every_field(self) -> None:
        first = self._run(["m1", "m2"])
        second = self._run(["m2", "m1"])
        for qid in ("m1", "m2"):
            assert first[qid]["assigned_engine"] == second[qid]["assigned_engine"]
            assert first[qid].get("tied_engines", []) == second[qid].get("tied_engines", [])


class TestADisagreeingGroupIsDecidedByItsScores:
    """#876 review, Important 1: m1 tied [DocumentDB, DynamoDB], m2 tied
    [Aurora MySQL, DocumentDB], all three engines kept. Only DocumentDB is
    common, which is no tie: the group's scores decide over every kept engine,
    and no reason claims a tie was resolved."""

    QAS = [
        {
            "query_id": "m1",
            "assigned_engine": "documentdb",
            "tied_engines": ["documentdb", "dynamodb"],
        },
        {
            "query_id": "m2",
            "assigned_engine": "documentdb",
            "tied_engines": ["aurora_mysql", "documentdb"],
        },
    ]
    KEPT = {"documentdb", "dynamodb", "aurora_mysql"}

    def _run(self, scores: dict[str, float]) -> dict[str, dict[str, Any]]:
        group = {"key": "abc123abc123", "scores": dict(scores)}
        trace = {q["query_id"]: {"engines": dict(scores), "group": group} for q in self.QAS}
        revised = _place([dict(q) for q in self.QAS], self.KEPT, trace)
        return {qa["query_id"]: qa for qa in revised}

    def test_a_higher_scoring_engine_wins(self) -> None:
        by_id = self._run({"documentdb": 70, "dynamodb": 70, "aurora_mysql": 90})
        assert {qa["assigned_engine"] for qa in by_id.values()} == {"aurora_mysql"}

    def test_an_exact_three_way_tie_stays_pending_for_the_customer(self) -> None:
        by_id = self._run({"documentdb": 70, "dynamodb": 70, "aurora_mysql": 70})
        for qa in by_id.values():
            assert qa["tied_engines"] == ["aurora_mysql", "documentdb", "dynamodb"]
            assert "now resolved" not in qa["assignment_reason"], qa["assignment_reason"]


class TestATiedEngineTheGroupCannotTakeIsNotASurvivor:
    """#876 review, Important 2: m1 is an untied join on DocumentDB, m2 is
    tied [DocumentDB, DynamoDB]; DocumentDB is removed. DynamoDB is the only
    surviving tied engine, but the join-off-DocumentDB block refuses it for
    m1, so it is not a survivor: the group's scores decide among the engines
    that can take it (Aurora MySQL). Before, the pool held DynamoDB only, the
    group stayed on the removed engine, and placement raised."""

    SCORES = {"documentdb": 60, "dynamodb": 80, "aurora_mysql": 70}

    @pytest.mark.parametrize("order", [["m1", "m2"], ["m2", "m1"]])
    def test_the_group_goes_to_an_engine_that_can_take_it(self, order: list[str]) -> None:
        qas_by_id: dict[str, dict[str, Any]] = {
            "m1": {"query_id": "m1", "assigned_engine": "documentdb"},
            "m2": {
                "query_id": "m2",
                "assigned_engine": "documentdb",
                "tied_engines": ["documentdb", "dynamodb"],
            },
        }
        group = {"key": "abc123abc123", "scores": dict(self.SCORES)}
        trace = {qid: {"engines": dict(self.SCORES), "group": group} for qid in order}
        query_map = {
            qid: {"query_id": qid, "query_type": "SELECT", "has_joins": True, "join_count": 1}
            for qid in order
        }

        revised = _place(
            [qas_by_id[q] for q in order], {"dynamodb", "aurora_mysql"}, trace, query_map
        )

        assert {qa["assigned_engine"] for qa in revised} == {"aurora_mysql"}


# ---------------------------------------------------------------------------
# The sanity sweep
# ---------------------------------------------------------------------------


def _query_map(*query_ids: str) -> dict[str, dict]:
    return {
        qid: {
            "query_id": qid,
            "query_text": f"SELECT * FROM t_{qid} WHERE id = ?",
            "query_type": "SELECT",
            "tables_accessed": [f"t_{qid}"],
        }
        for qid in query_ids
    }


def _context(scores: Mapping[str, Mapping[str, float]]) -> ScoringContext:
    analysis: dict[str, dict] = {}
    for qid, per_engine in scores.items():
        for engine, score in per_engine.items():
            analysis.setdefault(engine, {"table_recommendations": []})[
                "table_recommendations"
            ].append({"table_id": f"t_{qid}", "confidence_score": score})
    return ScoringContext(analysis_outputs=analysis)


class TestTheSweepIntersectsTheTies:
    def test_a_disagreeing_group_tie_is_the_intersection_not_the_first_members(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """j1 is tied [DocumentDB, DynamoDB], j2 [DocumentDB, Aurora MySQL]: the
        only engine both are tied on is DocumentDB, the orphan being removed, so
        no tied engine survives and every safe destination competes. Aurora
        MySQL's mean (90) wins. The first member's tie alone (j1's) would have
        kept the group to DynamoDB at 10."""
        assignments: list[dict[str, Any]] = [
            {
                "query_id": "j1",
                "assigned_engine": "documentdb",
                "tied_engines": ["documentdb", "dynamodb"],
            },
            {
                "query_id": "j2",
                "assigned_engine": "documentdb",
                "tied_engines": ["aurora_mysql", "documentdb"],
            },
            {"query_id": "a0", "assigned_engine": "aurora_mysql"},
        ]
        assignments += [{"query_id": f"y{i}", "assigned_engine": "dynamodb"} for i in range(6)]
        query_map = _query_map("j1", "j2", "a0", *(f"y{i}" for i in range(6)))
        ctx = _context(
            {
                "j1": {"dynamodb": 10, "aurora_mysql": 90},
                "j2": {"dynamodb": 10, "aurora_mysql": 90},
            }
        )

        with caplog.at_level(logging.WARNING, logger="src.agents.referee.tie_break"):
            updated, _, _ = sanity_sweep(
                assignments, [], ctx, query_map, co_dependency_groups=[["j1", "j2"]]
            )

        by_id = {qa["query_id"]: qa["assigned_engine"] for qa in updated}
        assert by_id["j1"] == by_id["j2"] == "aurora_mysql"
        assert "ties disagree" in caplog.text


class TestTheSweepFallsBackWhenNoTiedEngineCanTakeTheGroup:
    def test_a_tied_engine_gated_for_a_member_is_not_a_survivor(self) -> None:
        """j1 and j2 are tied [DocumentDB, DynamoDB] on the orphan DocumentDB,
        and j2 is a utility statement DynamoDB cannot run. DynamoDB is no
        survivor, so every safe destination competes and the group goes to
        Aurora MySQL; before, the group was blocked and kept the orphan."""
        tie = ["documentdb", "dynamodb"]
        assignments: list[dict[str, Any]] = [
            {"query_id": "j1", "assigned_engine": "documentdb", "tied_engines": list(tie)},
            {"query_id": "j2", "assigned_engine": "documentdb", "tied_engines": list(tie)},
            {"query_id": "a0", "assigned_engine": "aurora_mysql"},
        ]
        assignments += [{"query_id": f"y{i}", "assigned_engine": "dynamodb"} for i in range(6)]
        query_map = _query_map("j1", "j2", "a0", *(f"y{i}" for i in range(6)))
        ctx = _context(
            {
                "j1": {"dynamodb": 90, "aurora_mysql": 50},
                "j2": {"dynamodb": 90, "aurora_mysql": 50},
            }
        )
        ctx = replace(ctx, query_capabilities={"j2": ["sql_admin"]})

        updated, _, _ = sanity_sweep(
            assignments, [], ctx, query_map, co_dependency_groups=[["j1", "j2"]]
        )

        by_id = {qa["query_id"]: qa["assigned_engine"] for qa in updated}
        assert by_id["j1"] == by_id["j2"] == "aurora_mysql"


# ---------------------------------------------------------------------------
# A model's flag and the post-schema reroute
# ---------------------------------------------------------------------------


class TestFlagsAndTheRerouteIntersectTheTies:
    """g1 is tied [DocumentDB, DynamoDB, Aurora MySQL], g2 [DocumentDB, Aurora
    MySQL]; DocumentDB is flagged. The group's tie minus DocumentDB is Aurora
    MySQL alone, so the group goes there although DynamoDB scores higher. The
    first member's tie (g1's) would have let DynamoDB win."""

    SCORES = {
        "g1": {"dynamodb": 90, "aurora_mysql": 50, "documentdb": 60},
        "g2": {"dynamodb": 90, "aurora_mysql": 50, "documentdb": 60},
    }
    TIES = {
        "g1": ["aurora_mysql", "documentdb", "dynamodb"],
        "g2": ["aurora_mysql", "documentdb"],
    }

    def test_the_reality_check_flag_path(self) -> None:
        revised = [
            {
                "query_id": qid,
                "assigned_engine": "documentdb",
                "assignment_reason": "moved",
                "tied_engines": list(tie),
            }
            for qid, tie in self.TIES.items()
        ]
        original = [{"query_id": qid, "assigned_engine": "dynamodb"} for qid in self.TIES]
        consolidation = {
            "from_engine": "dynamodb",
            "to_engine": "documentdb",
            "query_count": 2,
            "reason": "no unique value",
            "saved_cost_estimate": 250,
            "action": "full",
            "queries_retained": [],
            "retention_reason": None,
        }
        flag = ModelFlag.model_validate(
            {
                "query_ids": ["g1"],
                "flagged_engine": "documentdb",
                "reason_category": "client_side_join",
                "explanation": "needs a server-side join",
            }
        )

        updated, _, _, _ = apply_consolidation_flags(
            [flag],
            revised,
            [consolidation],
            original,
            _context(self.SCORES),
            _query_map("g1", "g2"),
            ["aurora_mysql", "documentdb", "dynamodb"],
            co_dependency_groups=[["g1", "g2"]],
        )

        by_id = {qa["query_id"]: qa["assigned_engine"] for qa in updated}
        assert by_id == {"g1": "aurora_mysql", "g2": "aurora_mysql"}

    def test_a_flagged_group_falls_back_when_no_tied_engine_can_take_it(self) -> None:
        """#876 review, Important 3: the group is tied [DocumentDB, DynamoDB]
        and flagged off DynamoDB; a second flag gates DocumentDB for g2. No
        tied engine can take the group, so every remaining engine competes and
        Aurora MySQL takes it, instead of the group staying on the flagged
        engine."""
        ctx = _context(
            {
                "g1": {"dynamodb": 50, "documentdb": 99, "aurora_mysql": 40},
                "g2": {"dynamodb": 50, "documentdb": 99, "aurora_mysql": 40},
            }
        )
        routing = route_flagged_unit(
            ["g1", "g2"],
            "dynamodb",
            ["aurora_mysql", "documentdb", "dynamodb"],
            ctx.with_flag_gaps({"g1": ["dynamodb"], "g2": ["dynamodb", "documentdb"]}),
            _query_map("g1", "g2"),
            tied_engines=["documentdb", "dynamodb"],
        )

        assert routing.destination == "aurora_mysql"

    def test_the_post_schema_reroute(self) -> None:
        output = route_unsupported_queries(
            schema_outputs={"documentdb": {}},
            active_engines=["aurora_mysql", "documentdb", "dynamodb"],
            routing_flags_by_engine={
                "documentdb": [
                    {
                        "query_ids": ["g1"],
                        "flagged_engine": "documentdb",
                        "reason_category": "client_side_join",
                        "explanation": "needs a server-side join",
                    }
                ]
            },
            scoring_context=_context(self.SCORES),
            query_map=_query_map("g1", "g2"),
            assigned_engines={"g1": "documentdb", "g2": "documentdb"},
            co_dependency_groups=[["g1", "g2"]],
            tied_engines=self.TIES,
        )

        destinations = {r.query_id: r.to_engine for r in output.routings}
        assert destinations == {"g1": "aurora_mysql", "g2": "aurora_mysql"}


# ---------------------------------------------------------------------------
# The invariant: the first assignment gives every member the same tie
# ---------------------------------------------------------------------------


def _resolve_groups(analyzed: dict[str, int], n_groups: int = 2):
    from src.agents.referee.assignment_resolver import AssignmentResolver

    queries: list[dict] = []
    tables: list[dict] = []
    for g in range(n_groups):
        table = f"db.t{g}"
        tables.append({"table_id": table, "table_name": f"t{g}"})
        for m in range(3):
            queries.append(
                {
                    "query_id": f"g{g}m{m}",
                    "query_text": f"SELECT * FROM t{g} a JOIN t{g} b ON a.id = b.id",
                    "query_type": "SELECT",
                    "tables_accessed": [table],
                    "join_count": 1,
                    "has_joins": True,
                    "has_aggregations": False,
                    "filter_tables": [table],
                    "calls_per_second": 1.0,
                    "rows_returned_avg": 10,
                }
            )
    return AssignmentResolver().resolve(
        {"selected_agents": [{"agent_type": e} for e in analyzed], "signals": []},
        {
            e: {
                "table_recommendations": [
                    {"table_id": t["table_id"], "confidence_score": c} for t in tables
                ],
                "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
            }
            for e, c in analyzed.items()
        },
        {
            "job_id": "t",
            "metadata": {"source_database": {"engine": "sqlserver"}},
            "database_schema": {"tables": tables},
            "queries": {"query_patterns": queries},
        },
    )


class TestEveryMemberCarriesTheSameTieAfterTheFirstAssignment:
    """The invariant ``group_tie``'s warning relies on: after the first
    assignment, every member of a co-dependency group carries the same
    ``tied_engines`` (and the same engine)."""

    @pytest.mark.parametrize(
        "analyzed",
        [
            {"documentdb": 70, "dynamodb": 70, "aurora_postgresql": 40},  # unresolved tie
            {"documentdb": 70, "dynamodb": 70, "aurora_postgresql": 70},  # three-way
            {"documentdb": 80, "dynamodb": 70, "aurora_postgresql": 40},  # no tie
        ],
    )
    def test_members_share_one_tie(self, analyzed: dict[str, int]) -> None:
        result = _resolve_groups(analyzed)
        by_id = {qa.query_id: qa for qa in result.query_assignments}

        assert result.co_dependency_groups, "the fixture must form groups"
        for group in result.co_dependency_groups:
            ties = {tuple(sorted(by_id[q].tied_engines)) for q in group}
            engines = {by_id[q].assigned_engine for q in group}
            assert len(ties) == 1, (group, ties)
            assert len(engines) == 1, (group, engines)

    def test_the_unresolved_case_really_ties(self) -> None:
        result = _resolve_groups({"documentdb": 70, "dynamodb": 70, "aurora_postgresql": 40})
        assert all(qa.tied_engines for qa in result.query_assignments)
