"""#894: the reality check's Phase 1 scores a co-dependency group as a group.

Phase 1 decides whether an engine earns its place from the scores of the
queries it holds. A co-dependency group member used to be scored on its own
there, while every other pass uses the group's score: the mean of the
members' own scores over the engines that serve every member
(``co_dependency.group_engine_scores``, ADR-031 Decision 4, #727). So one
member could look unique to an engine the group as a whole does not need, or
the removal simulation could send the members to two different engines.
"""

from __future__ import annotations

from src.agents.referee.move_ledger import MoveLedger, ledger_for
from src.agents.referee.query_scoring import ScoringContext
from src.agents.referee.reality_check_floors import _apply_justification_floors
from src.agents.referee.reality_check_place import _place_settled_queries
from src.agents.referee.reality_check_rules import _moving_group_scores
from src.agents.referee.reality_check_settle import (
    _evaluate_engine_removal,
    _removal_tiebreak_key,
    _settle_committed_engines,
)
from src.agents.referee.routing_trace import RoutingTrace
from src.agents.referee.tie_break import break_tie

GROUP = {"key": "grp-g1-g2"}


def _qa(qid: str, engine: str) -> dict:
    return {"query_id": qid, "assigned_engine": engine, "assignment_reason": "test"}


def _member(engines: dict[str, object], group_scores: dict[str, float]) -> dict:
    return {"engines": engines, "group": {**GROUP, "scores": group_scores}}


class TestRemovalJudgesTheGroupScore:
    """The group {g1, g2} sits on DynamoDB. Alone, g1 scores DynamoDB 20
    points above DocumentDB (unique value); g2 scores it 15 below. The group's
    score is DynamoDB 80, DocumentDB 77.5: 2.5 points, not unique."""

    def _trace(self) -> dict[str, dict]:
        group_scores = {"dynamodb": 80.0, "documentdb": 77.5}
        return {
            "g1": _member({"dynamodb": 100, "documentdb": 80}, group_scores),
            "g2": _member({"dynamodb": 60, "documentdb": 75}, group_scores),
            "d1": {"engines": {"dynamodb": 50, "documentdb": 90}},
        }

    def _engine_queries(self) -> dict[str, list[dict]]:
        return {
            "dynamodb": [_qa("g1", "dynamodb"), _qa("g2", "dynamodb")],
            "documentdb": [_qa("d1", "documentdb")],
        }

    def _current(self) -> dict[str, str]:
        return {"g1": "dynamodb", "g2": "dynamodb", "d1": "documentdb"}

    def test_no_member_is_unique_on_its_own_score(self):
        removable, info, _ = _evaluate_engine_removal(
            "dynamodb",
            self._engine_queries()["dynamodb"],
            {"documentdb"},
            set(),
            self._trace(),
            self._current(),
            {},
            {},
            None,
            2,
        )

        assert info["unique_queries"] == []
        assert sorted(info["redundant_queries"]) == ["g1", "g2"]
        assert info["avg_delta"] == 2.5
        assert removable is True

    def test_the_engine_the_group_does_not_need_is_removed(self):
        committed, _ = _settle_committed_engines(
            engine_queries=self._engine_queries(),
            mandatory_query_ids=set(),
            mandatory_committed_engines=set(),
            trace_by_query=self._trace(),
            current_engine_by_qid=self._current(),
            query_signals={},
            query_map={},
            query_capabilities=None,
            max_rounds=5,
        )

        assert committed == {"documentdb"}

    def test_a_mandatory_member_stays_out_of_the_group_score(self):
        """A mandatory member never moves with its group (ADR #296), so the
        group's score is the mean over the members that do move -- the same
        member set the group-aware eligibility rule checks."""
        removable, info, _ = _evaluate_engine_removal(
            "dynamodb",
            self._engine_queries()["dynamodb"],
            {"documentdb"},
            {"g2"},
            self._trace(),
            self._current(),
            {},
            {},
            None,
            2,
        )

        # g2 is mandatory and keeps DynamoDB; g1 alone is unique by 20.
        assert info["unique_queries"] == ["g1"]
        assert removable is False


class TestTheRemovalSimulationMovesTheGroupAsOne:
    """Removing DocumentDB: g1 alone would go to DynamoDB (80 vs 70), g2 alone
    to Aurora MySQL (70 vs 50). The group's score is DynamoDB 65, Aurora
    MySQL 70, so the whole group lands on Aurora MySQL: one destination."""

    def test_the_group_has_one_destination(self):
        trace = {
            "g1": _member(
                {"documentdb": 90, "dynamodb": 80, "aurora_mysql": 70},
                {"documentdb": 90.0, "dynamodb": 65.0, "aurora_mysql": 70.0},
            ),
            "g2": _member(
                {"documentdb": 90, "dynamodb": 50, "aurora_mysql": 70},
                {"documentdb": 90.0, "dynamodb": 65.0, "aurora_mysql": 70.0},
            ),
        }
        n_destinations, fit_lost = _removal_tiebreak_key(
            "documentdb",
            [_qa("g1", "documentdb"), _qa("g2", "documentdb")],
            {"dynamodb", "aurora_mysql"},
            set(),
            trace,
            {"g1": "documentdb", "g2": "documentdb"},
            {},
            {},
            None,
        )

        assert n_destinations == 1
        assert fit_lost == 40.0


class TestAnUngroupedQueryIsUnchanged:
    def test_its_own_score_decides(self):
        trace = {"q1": {"engines": {"dynamodb": 100, "documentdb": 80}}}
        removable, info, _ = _evaluate_engine_removal(
            "dynamodb",
            [_qa("q1", "dynamodb")],
            {"documentdb"},
            set(),
            trace,
            {"q1": "dynamodb"},
            {},
            {},
            None,
            2,
        )

        assert info["unique_queries"] == ["q1"]
        assert removable is False


class TestTheSimulationSeedsAGroupTieWithItsMembers:
    """Removing DocumentDB, the group {g1, g2} scores Aurora MySQL and
    DynamoDB the same, so the destination is a placeholder pick
    (``tie_break.break_tie``'s hash). Seeded with one member's id, g1 picks
    DynamoDB and g2 picks Aurora MySQL: two destinations for one group. The
    group's seed is its sorted member ids, so both land on one engine,
    whatever order the trace lists them in."""

    def _key(self, order: list[str]) -> tuple[int, float]:
        tied = {"documentdb": 90, "dynamodb": 70, "aurora_mysql": 70}
        means = {"documentdb": 90.0, "dynamodb": 70.0, "aurora_mysql": 70.0}
        trace = {qid: _member(dict(tied), dict(means)) for qid in order}
        return _removal_tiebreak_key(
            "documentdb",
            [_qa(qid, "documentdb") for qid in order],
            {"dynamodb", "aurora_mysql"},
            set(),
            trace,
            {qid: "documentdb" for qid in order},
            {},
            {},
            None,
        )

    def test_the_group_lands_on_one_engine_in_either_order(self):
        assert self._key(["g1", "g2"]) == (1, 40.0)
        assert self._key(["g2", "g1"]) == (1, 40.0)


class TestPhaseOneAndTwoJudgeTheGroupOnTheSameNumbers:
    """ADR-031 Decision 7: the group {g1, g2} sits on DynamoDB, and g2 stays
    where it is (a mandatory signal override, or a customer pin the handler
    holds out of the check, #882). Only g1 moves, so the group's score is
    g1's own: DynamoDB 100, DocumentDB 80. The resolver's recorded mean
    counts g2 too (DynamoDB 70, DocumentDB 85). Phase 1 kept DynamoDB on
    g1's unique value, and Phase 2 then moved g1 to DocumentDB on the
    recorded mean, a 20-point loss decided by a member that does not move."""

    RECORDED = {"dynamodb": 70.0, "documentdb": 85.0}

    def _trace(self) -> dict[str, dict]:
        return {
            "g1": _member({"dynamodb": 100, "documentdb": 80}, self.RECORDED),
            "g2": _member({"dynamodb": 40, "documentdb": 90}, self.RECORDED),
            "d1": {"engines": {"dynamodb": 50, "documentdb": 90}},
        }

    def _run(self, *, pinned: bool) -> tuple[set[str], dict[str, str]]:
        held = [_qa("g1", "dynamodb"), _qa("d1", "documentdb")]
        mandatory: set[str] = set()
        if pinned:
            # #882: the handler holds a pinned query out of the check; the
            # resolver's trace still lists it in its group.
            assignments = held
        else:
            assignments = [*held, _qa("g2", "dynamodb")]
            mandatory = {"g2"}
        current = {qa["query_id"]: qa["assigned_engine"] for qa in assignments}
        engine_queries: dict[str, list[dict]] = {}
        for qa in assignments:
            engine_queries.setdefault(qa["assigned_engine"], []).append(qa)
        committed, _ = _settle_committed_engines(
            engine_queries=engine_queries,
            mandatory_query_ids=mandatory,
            mandatory_committed_engines=set(),
            trace_by_query=self._trace(),
            current_engine_by_qid=current,
            query_signals={},
            query_map={},
            query_capabilities=None,
            max_rounds=5,
        )
        revised, _ = _place_settled_queries(
            query_assignments=[dict(qa) for qa in assignments],
            committed_engines=committed,
            mandatory_query_ids=mandatory,
            current_engine_by_qid=current,
            query_signals={},
            query_map={},
            query_capabilities=None,
            trace_by_query=self._trace(),
            source_compatible_engine=None,
            workload_justified={},
        )
        return committed, {qa["query_id"]: qa["assigned_engine"] for qa in revised}

    def test_a_mandatory_member_weighs_in_on_neither_pass(self):
        committed, engine_of = self._run(pinned=False)

        assert committed == {"dynamodb", "documentdb"}
        assert engine_of["g1"] == "dynamodb"

    def test_a_customer_pinned_member_weighs_in_on_neither_pass(self):
        committed, engine_of = self._run(pinned=True)

        assert committed == {"dynamodb", "documentdb"}
        assert engine_of["g1"] == "dynamodb"

    def test_phase_one_leaves_a_pinned_member_out_of_the_score(self):
        trace = self._trace()
        _, info, _ = _evaluate_engine_removal(
            "dynamodb",
            [_qa("g1", "dynamodb")],
            {"documentdb"},
            set(),
            trace,
            {"g1": "dynamodb", "d1": "documentdb"},
            {},
            {},
            None,
            2,
        )

        assert info["unique_queries"] == ["g1"]
        assert info["avg_delta"] == 20.0


class TestASplitGroupAddsNoScoreTheScorerDidNotGive:
    """g1 sits on DynamoDB, but its group partner g2 sits on DocumentDB and
    DynamoDB cannot serve g2. The group is not held on DynamoDB as one unit,
    so DynamoDB has no group score: g1 is redundant there and adds no delta
    and no fit lost, never a 0 standing in for a score."""

    def _trace(self) -> dict[str, dict]:
        means = {"documentdb": 72.5}
        return {
            "g1": _member({"dynamodb": 100, "documentdb": 70}, means),
            "g2": _member({"dynamodb": {"gate": "capability"}, "documentdb": 75}, means),
        }

    def test_removal_counts_it_redundant_with_no_delta(self):
        removable, info, _ = _evaluate_engine_removal(
            "dynamodb",
            [_qa("g1", "dynamodb")],
            {"documentdb"},
            set(),
            self._trace(),
            {"g1": "dynamodb", "g2": "documentdb"},
            {},
            {},
            None,
            2,
        )

        assert info["redundant_queries"] == ["g1"]
        assert info["avg_delta"] == 0
        assert removable is True

    def test_the_simulation_adds_no_fit_lost(self):
        assert _removal_tiebreak_key(
            "dynamodb",
            [_qa("g1", "dynamodb")],
            {"documentdb"},
            set(),
            self._trace(),
            {"g1": "dynamodb", "g2": "documentdb"},
            {},
            {},
            None,
        ) == (1, 0.0)


class TestTheSimulationSeedIsTheSortedMemberIds:
    """Two groups tied between Aurora MySQL and DynamoDB, removing DocumentDB.
    Seeded with their sorted member ids ("a1,a2" and "a3,b6") they pick
    different engines: two destinations. A constant seed, either group's
    first or last member id, or any other single member would pick one engine
    for both, so this pins the seed Phase 2 and the floor use too."""

    def test_each_group_takes_its_own_seeds_pick(self):
        tied = {"documentdb": 90, "dynamodb": 70, "aurora_mysql": 70}
        means = {"documentdb": 90.0, "dynamodb": 70.0, "aurora_mysql": 70.0}
        # Listed last member first, so a "first member in trace order" seed differs too.
        order = ["a2", "a1", "b6", "a3"]
        groups = {"a1": "ga", "a2": "ga", "a3": "gb", "b6": "gb"}
        trace = {
            qid: {"engines": dict(tied), "group": {"key": groups[qid], "scores": dict(means)}}
            for qid in order
        }
        n_destinations, _ = _removal_tiebreak_key(
            "documentdb",
            [_qa(qid, "documentdb") for qid in order],
            {"dynamodb", "aurora_mysql"},
            set(),
            trace,
            {qid: "documentdb" for qid in order},
            {},
            {},
            None,
        )

        tied_engines = ["aurora_mysql", "dynamodb"]
        picks = {
            seed: break_tie(tied_engines, set(), None, None, seed)[0] for seed in ("a1,a2", "a3,b6")
        }
        assert picks == {"a1,a2": "aurora_mysql", "a3,b6": "dynamodb"}
        assert n_destinations == 2


class TestTheGroupScoreIsRoundedAsTheTraceRecordsIt:
    def test_a_three_member_mean_matches_record_group(self):
        trace = {
            "g1": {"engines": {"dynamodb": 70}},
            "g2": {"engines": {"dynamodb": 70}},
            "g3": {"engines": {"dynamodb": 71}},
        }
        recorder = RoutingTrace()
        recorder.record_group(["g1", "g2", "g3"], {"dynamodb": (70 + 70 + 71) / 3})

        scores = _moving_group_scores(["g1", "g2", "g3"], ["dynamodb"], trace)

        assert scores == {"dynamodb": 70.33}
        assert scores == recorder.group_by_query["g1"]["scores"]

    def test_one_query_keeps_its_own_score(self):
        trace = {"q1": {"engines": {"dynamodb": 70.333}}}
        assert _moving_group_scores(["q1"], ["dynamodb"], trace) == {"dynamodb": 70.333}


class TestASplitMemberLeavesDecision13Alone:
    """ADR-031 Decision 13: Aurora MySQL (source-compatible, $250) holds q1,
    which it scores the same as DocumentDB ($200, 20% and $50 cheaper: not
    enough). It also holds g1, whose group partner g2 sits on DocumentDB and
    cannot run on Aurora MySQL, so Aurora MySQL cannot keep that group as one
    unit. g1 is left out of the every-query check, as it is out of the unique
    value: Aurora MySQL keeps its protection on q1."""

    def _trace(self) -> dict[str, dict]:
        means = {"documentdb": 67.5}
        return {
            "q1": {"engines": {"aurora_mysql": 80, "documentdb": 80}},
            "g1": _member({"aurora_mysql": 70, "documentdb": 60}, means),
            "g2": _member({"aurora_mysql": {"gate": "capability"}, "documentdb": 75}, means),
        }

    def _current(self) -> dict[str, str]:
        return {"q1": "aurora_mysql", "g1": "aurora_mysql", "g2": "documentdb"}

    def test_the_source_compatible_engine_is_not_removable(self):
        removable, info, _ = _evaluate_engine_removal(
            "aurora_mysql",
            [_qa("q1", "aurora_mysql"), _qa("g1", "aurora_mysql")],
            {"documentdb"},
            set(),
            self._trace(),
            self._current(),
            {},
            {},
            None,
            2,
            source_compatible_engine="aurora_mysql",
        )

        assert info["redundant_queries"] == ["q1", "g1"]
        assert removable is False

    def test_it_stays_committed(self):
        committed, _ = _settle_committed_engines(
            engine_queries={
                "aurora_mysql": [_qa("q1", "aurora_mysql"), _qa("g1", "aurora_mysql")],
                "documentdb": [_qa("g2", "documentdb")],
            },
            mandatory_query_ids=set(),
            mandatory_committed_engines=set(),
            trace_by_query=self._trace(),
            current_engine_by_qid=self._current(),
            query_signals={},
            query_map={},
            query_capabilities=None,
            max_rounds=5,
            source_compatible_engine="aurora_mysql",
        )

        assert committed == {"aurora_mysql", "documentdb"}


class TestTheFloorMovesTheMembersItHolds:
    """DocumentDB is mandatory for g1 and g2 (a signal override) and fails its
    cost-share floor. Their group partner g3 is customer-pinned on Aurora
    MySQL: the handler holds it out of the check (#882), but the resolver's
    trace still lists it in the group. The floor moves the members it holds,
    as Phase 2 does, and scores them without g3: DynamoDB 90, not the
    recorded three-member mean of 80."""

    RECORDED = {"documentdb": 70.0, "dynamodb": 80.0, "aurora_mysql": 65.0}

    def _run(self) -> tuple[list[dict], MoveLedger]:
        revised = [
            {
                "query_id": qid,
                "assigned_engine": "documentdb",
                "assignment_reason": "signal override: flexible_schema",
                "signal_override": "flexible_schema",
            }
            for qid in ("g1", "g2")
        ] + [_qa(f"d{i}", "dynamodb") for i in range(40)]
        trace: dict[str, dict] = {
            "g1": _member({"documentdb": 70, "dynamodb": 90, "aurora_mysql": 50}, self.RECORDED),
            "g2": _member({"documentdb": 70, "dynamodb": 90, "aurora_mysql": 50}, self.RECORDED),
            "g3": _member({"documentdb": 70, "dynamodb": 60, "aurora_mysql": 95}, self.RECORDED),
        }
        trace.update({f"d{i}": {"engines": {"dynamodb": 90}} for i in range(40)})
        query_map = {
            qid: {"query_id": qid, "tables_accessed": ["db.t"], "query_type": "SELECT"}
            for qid in trace
        }
        engine_queries: dict[str, list[dict]] = {}
        for qa in revised:
            engine_queries.setdefault(qa["assigned_engine"], []).append(qa)
        ledger = ledger_for(revised)
        _apply_justification_floors(
            revised=revised,
            consolidations=[],
            unique_value_assessment={},
            committed_engines={"documentdb", "dynamodb"},
            engine_queries=engine_queries,
            mandatory_committed_engines={"documentdb"},
            fixed_query_ids={"g1", "g2"},
            total_queries=43,
            query_map=query_map,
            resolved_source_engine="mysql",
            trace_by_query=trace,
            current_engine_by_qid={qa["query_id"]: qa["assigned_engine"] for qa in revised},
            query_signals={},
            query_capabilities=None,
            source_compatible_engine=None,
            settled_counts={},
            scoring_ctx=ScoringContext.from_pipeline({}, {"signals": []}, {}),
            workload_justified={},
            ledger=ledger,
        )
        return revised, ledger

    def test_the_held_members_move_without_the_pinned_one(self):
        revised, _ = self._run()

        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in revised}
        assert engine_of["g1"] == engine_of["g2"] == "dynamodb"
        assert "g3" not in engine_of

    def test_their_scores_leave_the_pinned_member_out(self):
        _, ledger = self._run()

        moves = {m.query_id: m for m in ledger.moves if m.query_id in ("g1", "g2")}
        assert {qid: (m.to_engine, m.score_before, m.score_after) for qid, m in moves.items()} == {
            "g1": ("dynamodb", 70.0, 90.0),
            "g2": ("dynamodb", 70.0, 90.0),
        }
