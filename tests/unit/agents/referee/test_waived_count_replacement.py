"""#1151: a key-scoped COUNT the waiver put on DynamoDB moves back when a later pass
moves its table's writes off DynamoDB (ADR-031 Decision 2 Amendment 12).

The waiver lets the count pass DynamoDB's ``aggregation`` gate only while its table's
source of truth is on DynamoDB. Three passes can move the writes after the count is
placed: a customer pick (with its write propagation), the source-of-truth reroute of
a saved split, and a post-schema reroute of a flagged write. Each re-places the count
with the shared scorer as a ledger Move on its own pass, and a pass that moves no
table's writes changes nothing.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from src.agents.referee import assignment_resolver, query_scoring
from src.agents.referee.assignment_overrides import (
    ExcludedEngineOverride,
    QueryOverrideInput,
    apply_assignment_overrides,
    reroute_split_writes,
)
from src.agents.referee.move_ledger import check_replay, state_of
from src.agents.referee.waived_counts import replace_lapsed_counts
from src.contracts.move_ledger import MovePass, ReasonCode
from src.shared.engine_names import display_engine
from tests.unit.agents.referee.test_key_scoped_count_waiver import (
    ANALYSES,
    CNT,
    INS,
    TABLE,
    TRIAGE,
    UPD,
    _assignment,
    _collector,
)
from tests.unit.test_assignment_overrides import DB, JOB, _MemStore

# The shared scorer's base confidence, patched: Aurora PostgreSQL ahead of DocumentDB
# for every query, and DynamoDB ahead for the count once its waiver holds.
BASE = {"aurora_postgresql": 70, "documentdb": 50, "dynamodb": 80}


@pytest.fixture(autouse=True)
def _scores(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake(query_id, query, engine, analysis, **_kwargs):
        return BASE.get(engine, 10)

    monkeypatch.setattr(query_scoring, "compute_query_confidence", fake)
    monkeypatch.setattr(assignment_resolver, "compute_query_confidence", fake)


def _store(
    write_engine: str,
    count_engine: str,
    *,
    pinned: bool = False,
    split: bool = False,
    pin_upd: bool = False,
):
    store = _MemStore()
    assignment = _assignment(write_engine, count_engine).model_dump(mode="json")
    for qa in assignment["query_assignments"]:
        if qa["query_id"] == "cnt":
            qa["customer_override"] = pinned
        if split and qa["query_id"] == "upd":
            qa["assigned_engine"] = "aurora_postgresql"
        if pin_upd and qa["query_id"] == "upd":
            qa["customer_override"] = True
    store.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", assignment)
    store.write_json(f"{DB}/{JOB}/collector/output.json", _collector([INS, UPD, CNT]))
    store.write_json(f"{DB}/{JOB}/referee-triage/triage.json", TRIAGE)
    for engine, analysis in ANALYSES.items():
        store.write_json(f"{DB}/{JOB}/analysis-{engine}/analysis.json", analysis)
    return store


def _engines(assignment) -> dict[str, str]:
    return {qa.query_id: qa.assigned_engine for qa in assignment.query_assignments}


def _count_moves(moves) -> list[tuple[str, str, str, str]]:
    return [
        (m.pass_.value, m.reason_code.value, m.from_engine, m.to_engine)
        for m in moves
        if m.query_id == "cnt"
    ]


class TestTheCustomerGate:
    def test_a_pick_that_moves_the_writes_re_places_the_count(self) -> None:
        store = _store("dynamodb", "dynamodb")
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("ins", assigned_engine="aurora_postgresql")]
        )
        engines = _engines(result.assignment)
        # The pick pulled the table's other write, and the count followed its table.
        assert engines == {
            "ins": "aurora_postgresql",
            "upd": "aurora_postgresql",
            "cnt": "aurora_postgresql",
        }
        assert _count_moves(result.assignment.moves) == [
            ("customer_group_propagation", "gate.key_scoped_count", "dynamodb", "aurora_postgresql")
        ]
        assert result.replaced_count_query_ids == ["cnt"]
        (count,) = [qa for qa in result.assignment.query_assignments if qa.query_id == "cnt"]
        assert "key-scoped count of forum.posts" in count.assignment_reason
        assert not count.customer_override
        (move,) = [m for m in result.assignment.moves if m.query_id == "cnt"]
        # The customer's own edit moved the writes: the Move is the customer's (ADR-029).
        assert move.actor.value == "customer"
        # The kept engines are Aurora PostgreSQL and DynamoDB, whose gate now rules
        # the count out: no runner-up (DocumentDB is not kept, so never a candidate).
        assert move.runner_up is None
        assert move.score_after is not None

    def test_the_count_goes_to_the_best_scoring_kept_engine(self, monkeypatch) -> None:
        """The scorer decides, not the writes: with DocumentDB scoring the count best,
        it lands there (rule ``highest_score``), not on the table's new writer."""
        monkeypatch.setitem(BASE, "documentdb", 75)
        store = _store("dynamodb", "dynamodb")
        assignment = _assignment("dynamodb", "dynamodb").model_dump(mode="json")
        # DocumentDB is kept: one other query of the version is on it.
        assignment["query_assignments"].append(
            {
                **assignment["query_assignments"][2],
                "query_id": "other",
                "assigned_engine": "documentdb",
            }
        )
        store.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", assignment)
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("ins", assigned_engine="aurora_postgresql")]
        )
        assert _engines(result.assignment)["cnt"] == "documentdb"
        assert _count_moves(result.assignment.moves) == [
            ("customer_group_propagation", "highest_score", "dynamodb", "documentdb")
        ]

    def test_picking_the_count_onto_the_old_writer_in_the_same_edit_is_rejected(self) -> None:
        store = _store("dynamodb", "dynamodb")
        with pytest.raises(ExcludedEngineOverride) as exc:
            apply_assignment_overrides(
                store,
                DB,
                JOB,
                [
                    QueryOverrideInput("ins", assigned_engine="aurora_postgresql"),
                    QueryOverrideInput("cnt", assigned_engine="dynamodb"),
                ],
            )
        assert [(e.query_id, e.gate_kind) for e in exc.value.excluded] == [("cnt", "capability")]
        assert "forum.posts's writes" in exc.value.excluded[0].message
        assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")

    def test_a_pinned_count_is_moved_and_its_pin_cleared_with_a_warning(self) -> None:
        store = _store("dynamodb", "dynamodb", pinned=True)
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("ins", assigned_engine="aurora_postgresql")]
        )
        (count,) = [qa for qa in result.assignment.query_assignments if qa.query_id == "cnt"]
        assert (count.assigned_engine, count.customer_override) == ("aurora_postgresql", False)
        assert any(
            w.startswith(f"Query cnt was pinned to {display_engine('dynamodb')}")
            for w in result.assignment.validation_warnings
        )


class TestNothingMovesWhenTheWaiverStillHolds:
    @pytest.mark.parametrize(
        "override",
        [
            # A write picked onto the engine its table is already on.
            QueryOverrideInput("ins", assigned_engine="dynamodb"),
            # A scope edit of the count itself.
            QueryOverrideInput("cnt", in_scope=True),
        ],
        ids=["writes-stay", "scope-only"],
    )
    def test_an_edit_that_keeps_the_writes_keeps_the_count(self, override) -> None:
        store = _store("dynamodb", "dynamodb")
        result = apply_assignment_overrides(store, DB, JOB, [override])
        assert _engines(result.assignment)["cnt"] == "dynamodb"
        assert _count_moves(result.assignment.moves) == []
        assert result.replaced_count_query_ids == []

    def test_a_count_that_never_relied_on_the_waiver_stays(self) -> None:
        """A count already on Aurora PostgreSQL is not re-placed when the writes move."""
        store = _store("dynamodb", "aurora_postgresql")
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("ins", assigned_engine="documentdb")]
        )
        assert _engines(result.assignment)["cnt"] == "aurora_postgresql"
        assert _count_moves(result.assignment.moves) == []

    def test_writes_moving_onto_dynamodb_never_move_a_count_there(self) -> None:
        """The rule only re-places a count whose waiver lapsed; it never pulls one onto
        the new writer (the first assignment and the reality check place counts)."""
        store = _store("aurora_postgresql", "aurora_postgresql")
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("ins", assigned_engine="dynamodb")]
        )
        assert _engines(result.assignment)["cnt"] == "aurora_postgresql"
        assert _count_moves(result.assignment.moves) == []


class TestTheRule:
    def _version(self, write_engine: str, count_engine: str) -> list[dict[str, Any]]:
        qas: list[dict[str, Any]] = _assignment(write_engine, count_engine).model_dump(mode="json")[
            "query_assignments"
        ]
        return qas

    def _run(self, qas, before, pinned="hold"):
        from src.agents.referee.move_ledger import ledger_for
        from src.agents.referee.query_scoring import ScoringContext

        ledger = ledger_for(qas)
        start = state_of(qas)
        self.replaced = replace_lapsed_counts(
            qas,
            ledger,
            query_by_id={q["query_id"]: q for q in (INS, UPD, CNT)},
            ctx=ScoringContext.from_pipeline(ANALYSES, TRIAGE, _collector([INS, UPD, CNT])),
            write_groups={TABLE: ["ins", "upd"]},
            before=before,
            pass_=MovePass.POST_SCHEMA_FLAG,
            source_compatible="aurora_postgresql",
            pinned=pinned,
        )
        check_replay(ledger.moves, start, state_of(qas), "test")
        return self.replaced.moved, ledger

    def test_no_changed_source_of_truth_is_a_no_op(self) -> None:
        qas = self._version("dynamodb", "dynamodb")
        moves, ledger = self._run(qas, {TABLE: "dynamodb"})
        assert moves == [] and ledger.moves == []

    def test_an_exact_tie_goes_through_the_tie_break(self, monkeypatch) -> None:
        """DocumentDB and Aurora PostgreSQL tie for the count: the source-compatible
        engine takes it (``tie.source_compatible``), the shared tie-break's first rule."""
        from src.agents.referee import waived_counts
        from src.agents.referee.query_scoring import score_query_for_engine

        def level(query, engine, ctx):
            result = score_query_for_engine(query, engine, ctx)
            if engine in ("aurora_postgresql", "documentdb") and result.score is not None:
                return dataclasses.replace(result, score=70)
            return result

        monkeypatch.setattr(waived_counts, "score_query_for_engine", level)
        qas = self._version("dynamodb", "dynamodb")
        # The writes moved to DocumentDB; Aurora PostgreSQL is kept by another query.
        qas.append({**qas[2], "query_id": "other", "assigned_engine": "aurora_postgresql"})
        qas[0]["assigned_engine"] = qas[1]["assigned_engine"] = "documentdb"
        moves, ledger = self._run(qas, {TABLE: "dynamodb"})
        (move,) = moves
        assert (move.to_engine, move.source_of_truth) == ("aurora_postgresql", "documentdb")
        assert [m.reason_code for m in ledger.moves] == [ReasonCode.TIE_SOURCE_COMPATIBLE]

    def test_a_split_table_moves_its_count_too(self) -> None:
        """Writes on two engines are no source of truth: the waiver holds on neither."""
        qas = self._version("dynamodb", "dynamodb")
        qas[1]["assigned_engine"] = "aurora_postgresql"
        moves, _ = self._run(qas, {TABLE: "dynamodb"})
        assert [(m.to_engine, m.source_of_truth) for m in moves] == [("aurora_postgresql", None)]


class TestPinsAndUnservedCounts:
    def _rule(self):
        return TestTheRule()

    def test_a_rule_pass_holds_a_pinned_count(self) -> None:
        rule = self._rule()
        qas = rule._version("dynamodb", "dynamodb")
        qas[2]["customer_override"] = True
        qas[0]["assigned_engine"] = qas[1]["assigned_engine"] = "aurora_postgresql"
        moves, ledger = rule._run(qas, {TABLE: "dynamodb"}, pinned="hold")
        assert moves == [] and ledger.moves == []
        assert rule.replaced.held == ["cnt"]
        assert (qas[2]["assigned_engine"], qas[2]["customer_override"]) == ("dynamodb", True)
        assert any("the pin is kept" in w for w in rule.replaced.warnings)

    def test_no_kept_engine_serving_it_is_a_version_warning(self, monkeypatch) -> None:
        from src.agents.referee import waived_counts

        monkeypatch.setattr(waived_counts, "can_own", lambda *_a, **_k: False)
        rule = self._rule()
        qas = rule._version("dynamodb", "dynamodb")
        qas[0]["assigned_engine"] = qas[1]["assigned_engine"] = "aurora_postgresql"
        moves, _ = rule._run(qas, {TABLE: "dynamodb"})
        assert moves == [] and rule.replaced.unserved == ["cnt"]
        assert any(
            w.startswith("Query cnt is a key-scoped count") and "no engine" in w
            for w in rule.replaced.warnings
        )


class TestTheFeasibilityReview:
    def test_a_held_pinned_count_is_a_blocking_finding(self) -> None:
        from src.agents.referee.feasibility_review import review_assignment_feasibility
        from src.contracts.feasibility_models import FindingKind, FindingSeverity

        assignment = _assignment("aurora_postgresql", "dynamodb")
        assignment.query_assignments[2].customer_override = True
        findings = [
            f
            for f in review_assignment_feasibility(assignment, _collector([INS, UPD, CNT]))
            if f.kind is FindingKind.STRANDED_COUNT
        ]
        assert [(f.severity, f.query_ids, f.table) for f in findings] == [
            (FindingSeverity.BLOCKING, ["cnt"], TABLE)
        ]

    def test_a_count_on_its_tables_writer_is_no_finding(self) -> None:
        from src.agents.referee.feasibility_review import review_assignment_feasibility
        from src.contracts.feasibility_models import FindingKind

        for writes, count in (("dynamodb", "dynamodb"), ("dynamodb", "aurora_postgresql")):
            findings = review_assignment_feasibility(
                _assignment(writes, count), _collector([INS, UPD, CNT])
            )
            assert not [f for f in findings if f.kind is FindingKind.STRANDED_COUNT]


class TestTheSourceOfTruthReroute:
    def test_a_split_rerouted_off_dynamodb_takes_the_count_with_it(self) -> None:
        """A saved split (ins on DynamoDB, upd on Aurora PostgreSQL) goes to one engine;
        with Aurora PostgreSQL scoring the writes best, the count on DynamoDB follows."""
        store = _store("dynamodb", "dynamodb", split=True)
        BASE_WRITES = {"aurora_postgresql": 90, "dynamodb": 60}

        def fake(query_id, query, engine, analysis, **_kwargs):
            return (BASE_WRITES if query_id in ("ins", "upd") else BASE).get(engine, 10)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(query_scoring, "compute_query_confidence", fake)
            result = reroute_split_writes(store, DB, JOB, after_approval=True)
        assert result is not None
        engines = _engines(result.assignment)
        assert engines == {
            "ins": "aurora_postgresql",
            "upd": "aurora_postgresql",
            "cnt": "aurora_postgresql",
        }
        assert _count_moves(result.assignment.moves) == [
            ("co_dependency", "gate.key_scoped_count", "dynamodb", "aurora_postgresql")
        ]
        assert "cnt" in result.moved_query_ids
        rows = {r.query_id: r for r in result.assignment.post_schema_reroutes}
        assert rows["cnt"].reason_category == "source_of_truth"

    def test_a_split_rerouted_onto_dynamodb_keeps_the_count(self) -> None:
        store = _store("dynamodb", "dynamodb", split=True)
        BASE_WRITES = {"aurora_postgresql": 60, "dynamodb": 90}

        def fake(query_id, query, engine, analysis, **_kwargs):
            return (BASE_WRITES if query_id in ("ins", "upd") else BASE).get(engine, 10)

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(query_scoring, "compute_query_confidence", fake)
            result = reroute_split_writes(store, DB, JOB)
        assert result is not None
        assert _engines(result.assignment)["cnt"] == "dynamodb"
        assert _count_moves(result.assignment.moves) == []


class TestTheSourceOfTruthRerouteHoldsPins:
    WRITES_ON_AURORA = {"aurora_postgresql": 90, "dynamodb": 60}

    def _fake(self, query_id, query, engine, analysis, **_kwargs):
        return (self.WRITES_ON_AURORA if query_id in ("ins", "upd") else BASE).get(engine, 10)

    def test_a_pinned_count_holds_its_tables_writes_on_its_engine(self) -> None:
        """Aurora PostgreSQL scores the writes best, but the count pinned on DynamoDB is
        a pin on its table's writes: the split goes to DynamoDB, the count stays."""
        store = _store("dynamodb", "dynamodb", split=True, pinned=True)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(query_scoring, "compute_query_confidence", self._fake)
            result = reroute_split_writes(store, DB, JOB)
        assert result is not None
        assert _engines(result.assignment) == {
            "ins": "dynamodb",
            "upd": "dynamodb",
            "cnt": "dynamodb",
        }
        (count,) = [qa for qa in result.assignment.query_assignments if qa.query_id == "cnt"]
        assert count.customer_override
        assert _count_moves(result.assignment.moves) == []

    def test_a_count_pin_against_a_write_pin_is_held_and_blocks(self) -> None:
        """The customer pinned a write to Aurora PostgreSQL and the count to DynamoDB:
        the writes re-route by score (their pins disagree), the count keeps its pin
        and engine, the version says so, and the feasibility review blocks on it."""
        from src.agents.referee.feasibility_review import review_assignment_feasibility
        from src.contracts.feasibility_models import FindingKind

        store = _store("dynamodb", "dynamodb", split=True, pinned=True, pin_upd=True)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(query_scoring, "compute_query_confidence", self._fake)
            result = reroute_split_writes(store, DB, JOB)
        assert result is not None
        engines = _engines(result.assignment)
        assert (engines["ins"], engines["upd"], engines["cnt"]) == (
            "aurora_postgresql",
            "aurora_postgresql",
            "dynamodb",
        )
        (count,) = [qa for qa in result.assignment.query_assignments if qa.query_id == "cnt"]
        assert count.customer_override
        assert _count_moves(result.assignment.moves) == []
        assert any("the pin is kept" in w for w in result.assignment.validation_warnings)
        kinds = [
            f.kind
            for f in review_assignment_feasibility(result.assignment, _collector([INS, UPD, CNT]))
        ]
        assert FindingKind.STRANDED_COUNT in kinds

    def test_a_concurrent_reroute_lists_the_moved_count_from_the_written_version(
        self, monkeypatch
    ) -> None:
        """Two resumed runs reroute the same saved version: the second finds v2 written
        and returns it, its moved queries read from v2's Moves, the count included."""
        from src.agents.referee import assignment_overrides

        store = _store("dynamodb", "dynamodb", split=True)
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(query_scoring, "compute_query_confidence", self._fake)
            first = reroute_split_writes(store, DB, JOB)
            assert first is not None and "cnt" in first.moved_query_ids
            mp.setattr(assignment_overrides, "resolve_effective_assignment_version", lambda *_a: 1)
            second = reroute_split_writes(store, DB, JOB)
        assert second is not None
        assert second.moved_query_ids == sorted(first.moved_query_ids)
        assert "cnt" in second.moved_query_ids


class TestThePostSchemaReroute:
    def _reroute(self, store, to_engine: str):
        from src.agents.referee.post_schema_routing_handler import PostSchemaRouting
        from src.contracts.post_schema_router_output import QueryRouting

        assignment = store.read_json(f"{DB}/{JOB}/assignment/v1/assignment.json")
        routings = [
            QueryRouting(
                query_id=qid,
                from_engine="dynamodb",
                to_engine=to_engine,
                reason="flagged on DynamoDB",
                cascade_depth=0,
                reason_category="capability_gap",
            )
            for qid in ("ins", "upd")
        ]
        return PostSchemaRouting(store, _no_redesign)._rerouted_assignment(
            JOB, DB, assignment, 1, 2, routings
        )

    def test_a_flagged_write_group_moved_off_dynamodb_takes_its_count(self) -> None:
        store = _store("dynamodb", "dynamodb")
        revised = self._reroute(store, "aurora_postgresql")
        engines = {qa["query_id"]: qa["assigned_engine"] for qa in revised["query_assignments"]}
        assert engines["cnt"] == "aurora_postgresql"
        count = [m for m in revised["moves"] if m["query_id"] == "cnt"]
        assert [(m["pass"], m["reason_code"], m["actor"]) for m in count] == [
            ("post_schema_flag", "gate.key_scoped_count", "rule")
        ]
        rows = {r["query_id"]: r for r in revised["post_schema_reroutes"]}
        assert (rows["cnt"]["reason_category"], rows["cnt"]["approved_version"]) == (
            "source_of_truth",
            1,
        )
        assert set(rows) == {"ins", "upd", "cnt"}

    def test_a_reroute_that_moves_no_write_leaves_the_count(self) -> None:
        store = _store("dynamodb", "dynamodb")
        from src.agents.referee.post_schema_routing_handler import PostSchemaRouting

        assignment = store.read_json(f"{DB}/{JOB}/assignment/v1/assignment.json")
        revised = PostSchemaRouting(store, _no_redesign)._rerouted_assignment(
            JOB, DB, assignment, 1, 2, []
        )
        assert {qa["query_id"]: qa["assigned_engine"] for qa in revised["query_assignments"]}[
            "cnt"
        ] == "dynamodb"
        assert revised["moves"] == []


class TestThePostSchemaRerouteHoldsPins:
    def test_the_router_holds_a_flagged_write_whose_table_has_a_pinned_count(self) -> None:
        """A flag on a write of a table whose count the customer pinned on DynamoDB
        holds the write group in place (``held_queries``), as a pinned write does."""
        from src.agents.referee.post_schema_router import route_unsupported_queries
        from src.agents.referee.query_scoring import ScoringContext
        from src.agents.referee.waived_counts import pinned_counts_by_table

        assignment = _assignment("dynamodb", "dynamodb")
        assignment.query_assignments[2].customer_override = True
        ctx = ScoringContext.from_pipeline(ANALYSES, TRIAGE, _collector([INS, UPD, CNT]))
        query_map = {q["query_id"]: q for q in (INS, UPD, CNT)}
        pinned_counts = pinned_counts_by_table(assignment.query_assignments, query_map, ctx)
        assert pinned_counts == {TABLE: ["cnt"]}
        flags = {
            "dynamodb": [
                {
                    "flagged_engine": "dynamodb",
                    "query_ids": ["ins"],
                    "reason_category": "missing_feature",
                    "explanation": "needs a transaction",
                }
            ]
        }

        def route(counts):
            return route_unsupported_queries(
                {"dynamodb": {}},
                ["aurora_postgresql", "dynamodb"],
                routing_flags_by_engine=flags,
                scoring_context=ctx,
                query_map=query_map,
                assigned_engines={
                    qa.query_id: qa.assigned_engine for qa in assignment.query_assignments
                },
                customer_pinned={"cnt"},
                co_dependency_groups=[["ins", "upd"]],
                write_groups={TABLE: ["ins", "upd"]},
                pinned_counts=counts,
            )

        held = route(pinned_counts)
        assert held.routings == [] and sorted(held.held_queries) == ["ins", "upd"]
        # Without the hold the flag would move the writes (the case #1151's review found).
        assert {r.query_id for r in route(None).routings} == {"ins", "upd"}

    def test_a_reroute_never_clears_a_count_pin(self) -> None:
        """Even given moves that take the writes (the router would have held them), the
        rerouted version keeps the pinned count, its pin and engine, and says so."""
        store = _store("dynamodb", "dynamodb", pinned=True)
        revised = TestThePostSchemaReroute()._reroute(store, "aurora_postgresql")
        (count,) = [qa for qa in revised["query_assignments"] if qa["query_id"] == "cnt"]
        assert (count["assigned_engine"], count["customer_override"]) == ("dynamodb", True)
        assert not [m for m in revised["moves"] if m["query_id"] == "cnt"]
        assert "cnt" not in {r["query_id"] for r in revised["post_schema_reroutes"]}
        assert any("Query cnt" in w and "pin is kept" in w for w in revised["validation_warnings"])


def _no_redesign(*_args: object) -> None:
    raise AssertionError("the post-schema routing redesigns nothing in this test")
