"""#377: a key-scoped COUNT passes DynamoDB's aggregation gate when its table's writes
are on DynamoDB (ADR-031 Decision 2 Amendment 12, maintainer decision 2026-10-19).

The waiver is catalog data (``src/catalog/gates.toml`` ``[[waivers]]``): it lifts the
``aggregation`` gate for one engine on one class fact (``key_scoped_count``), only when
the query's one table has its source of truth on that engine. The shared scorer still
decides, a tie goes through the tie-break, and every engine change is a ledger Move.
MIN and MAX are not waived.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from src.agents.referee import assignment_resolver, query_scoring
from src.agents.referee.access_pattern import (
    KEY_SCOPED,
    KEY_SCOPED_COUNT_FACT,
    SchemaKeys,
    classify_query,
)
from src.agents.referee.assignment_resolver import AssignmentResolver, RoutingTrace
from src.agents.referee.query_scoring import ScoringContext, score_query_for_engine
from src.agents.referee.reality_check_rules import _query_eligible_engines
from src.agents.referee.write_groups import assignment_sources_of_truth, sources_of_truth
from src.contracts.move_ledger import CapabilityWaiver, MoveEvidence, MovePass, ReasonCode
from src.shared.catalog.engines import CatalogError
from src.shared.catalog.routing import GATES_FILE, load_gates_file, waivers
from src.shared.move_reasons import rule_label

TABLE = "forum.posts"
POSTS: dict[str, Any] = {
    "table_id": TABLE,
    "table_name": "posts",
    "primary_key": ["id"],
    "foreign_keys": [{"columns": ["topic_id"], "referenced_table": "topics"}],
    "columns": [
        {"column_name": "id"},
        {"column_name": "topic_id"},
        {"column_name": "post_type"},
        {"column_name": "body"},
    ],
}


def _q(qid: str, text: str, tables: tuple[str, ...] = (TABLE,)) -> dict[str, Any]:
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": text.split()[0].upper(),
        "tables_accessed": list(tables),
        "calls_per_second": 1.0,
    }


INS = _q("ins", "INSERT INTO posts (id, topic_id, body) VALUES ($1, $2, $3)")
UPD = _q("upd", "UPDATE posts SET body = $1 WHERE id = $2")
CNT = _q("cnt", "SELECT COUNT(*) FROM posts WHERE topic_id = $1 AND post_type = $2")
MAX = _q("mx", "SELECT coalesce(max(id), $1) FROM posts WHERE topic_id = $2")
AGGREGATES = ("cnt", "mx")


def _collector(queries: list[dict]) -> dict[str, Any]:
    return {
        "job_id": "t",
        "metadata": {"source_database": {"engine": "postgresql"}},
        "database_schema": {"tables": [POSTS]},
        "queries": {"query_patterns": queries},
    }


def _keys() -> SchemaKeys:
    return SchemaKeys.from_collector(_collector([]))


# ---------------------------------------------------------------------------
# The class fact (one definition, read from the SQL facade)
# ---------------------------------------------------------------------------

COUNTS = [
    "SELECT COUNT(*) FROM posts WHERE topic_id = $1",
    "SELECT COUNT(1) FROM posts WHERE topic_id = $1 AND post_type IN ($2, $3)",
    "SELECT COUNT(body) FROM posts WHERE topic_id = $1 AND (id > $2)",
]
NOT_COUNTS = [
    # MIN and MAX wait (#377): they need a designed sort key.
    "SELECT coalesce(max(id), $1) FROM posts WHERE topic_id = $2",
    "SELECT MIN(id) FROM posts WHERE topic_id = $1",
    "SELECT COUNT(*), MAX(id) FROM posts WHERE topic_id = $1",
    "SELECT SUM(id) FROM posts WHERE topic_id = $1",
    "SELECT COUNT(DISTINCT post_type) FROM posts WHERE topic_id = $1",
    # Several filtered counts are several Queries, not one Select = COUNT.
    "SELECT COUNT(*) FILTER (WHERE id > $1) AS a, COUNT(*) FILTER (WHERE id > $2) AS b "
    "FROM posts WHERE topic_id = $3",
    "SELECT topic_id, COUNT(*) FROM posts WHERE topic_id = $1 GROUP BY topic_id",
    # No key anchors the rows: the aggregation class.
    "SELECT COUNT(*) FROM posts WHERE post_type = $1",
    "SELECT COUNT(*) FROM posts WHERE topic_id IN (SELECT id FROM posts WHERE post_type = $1)",
    "SELECT * FROM posts WHERE topic_id = $1",
    # A write is not a read, and a count over a derived table is two scopes.
    "INSERT INTO posts (id, topic_id) SELECT COUNT(*), $1 FROM posts WHERE topic_id = $2",
    "SELECT COUNT(*) FROM (SELECT id FROM posts WHERE topic_id = $1) s",
]


@pytest.mark.parametrize("sql", COUNTS)
def test_a_count_over_one_keys_rows_of_one_table_shows_the_fact(sql: str) -> None:
    found = classify_query(_q("q", sql), _keys(), "postgresql")
    assert found.name == KEY_SCOPED
    assert KEY_SCOPED_COUNT_FACT in found.facts


@pytest.mark.parametrize("sql", NOT_COUNTS)
def test_any_other_aggregate_or_read_does_not(sql: str) -> None:
    assert KEY_SCOPED_COUNT_FACT not in classify_query(_q("q", sql), _keys(), "postgresql").facts


def test_a_count_over_a_key_anchored_join_does_not() -> None:
    """A join is not one table's item collection count, even when every table is reached
    by key: the waiver reads one table's source of truth."""
    keys = SchemaKeys.from_collector(
        {
            "database_schema": {
                "tables": [
                    POSTS,
                    {"table_id": "forum.topics", "table_name": "topics", "primary_key": ["id"]},
                ]
            }
        }
    )
    sql = "SELECT COUNT(*) FROM posts p JOIN topics t ON t.id = p.topic_id WHERE t.id = $1"
    found = classify_query(_q("q", sql, (TABLE, "forum.topics")), keys, "postgresql")
    assert found.name == KEY_SCOPED
    assert KEY_SCOPED_COUNT_FACT not in found.facts


def test_a_count_with_a_subquery_does_not() -> None:
    """A second scope (here a key-anchored semi-join) is a second read, not one Query."""
    keys = SchemaKeys.from_collector(
        {
            "database_schema": {
                "tables": [
                    POSTS,
                    {
                        "table_id": "forum.likes",
                        "table_name": "likes",
                        "primary_key": ["id"],
                        "foreign_keys": [{"columns": ["user_id"]}, {"columns": ["post_id"]}],
                    },
                ]
            }
        }
    )
    sql = (
        "SELECT COUNT(*) FROM posts WHERE topic_id = $1 "
        "AND id IN (SELECT post_id FROM likes WHERE user_id = $2)"
    )
    found = classify_query(_q("q", sql, (TABLE, "forum.likes")), keys, "postgresql")
    assert found.name == KEY_SCOPED
    assert KEY_SCOPED_COUNT_FACT not in found.facts


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def _ctx(queries: list[dict], engines=("aurora_postgresql", "dynamodb", "elasticache")):
    caps = {q["query_id"]: ["aggregation"] for q in queries if q["query_id"] in AGGREGATES}
    return ScoringContext.from_pipeline(
        {e: {"table_recommendations": [{"table_id": TABLE}]} for e in engines},
        {"query_capabilities": caps},
        _collector(queries),
    )


class TestTheGate:
    def test_the_waiver_lifts_the_gate_only_on_the_tables_source_of_truth(self) -> None:
        ctx = _ctx([INS, UPD, CNT])
        assert score_query_for_engine(CNT, "dynamodb", ctx).gate_kind == "capability"
        on_ddb = ctx.with_sources_of_truth({TABLE: "dynamodb"})
        assert score_query_for_engine(CNT, "dynamodb", on_ddb).score is not None
        on_aurora = ctx.with_sources_of_truth({TABLE: "aurora_postgresql"})
        assert score_query_for_engine(CNT, "dynamodb", on_aurora).gate_kind == "capability"
        # Another table's source of truth says nothing about this one.
        other = ctx.with_sources_of_truth({"forum.topics": "dynamodb"})
        assert score_query_for_engine(CNT, "dynamodb", other).gate_kind == "capability"

    def test_min_and_max_stay_gated(self) -> None:
        ctx = _ctx([INS, UPD, MAX]).with_sources_of_truth({TABLE: "dynamodb"})
        assert score_query_for_engine(MAX, "dynamodb", ctx).gate_kind == "capability"

    def test_no_other_engine_is_waived(self) -> None:
        """The waiver names one engine in the catalog; a cache is never a source of
        truth, and its gate stays even if a table's writes were said to be on it."""
        ctx = _ctx([INS, UPD, CNT]).with_sources_of_truth({TABLE: "elasticache"})
        assert score_query_for_engine(CNT, "elasticache", ctx).gate_kind == "capability"

    def test_a_query_reading_two_tables_is_not_waived(self) -> None:
        two = _q("cnt", CNT["query_text"], (TABLE, "forum.topics"))
        ctx = _ctx([INS, UPD, two]).with_sources_of_truth(
            {TABLE: "dynamodb", "forum.topics": "dynamodb"}
        )
        assert score_query_for_engine(two, "dynamodb", ctx).gate_kind == "capability"

    def test_the_waived_score_is_the_plain_scorers_score(self) -> None:
        """The waiver only lifts the gate: the score is the one the scorer gives any
        servable engine, the same pattern, cost and penalty arithmetic."""
        ctx = _ctx([INS, UPD, CNT])
        waived = score_query_for_engine(
            CNT, "dynamodb", ctx.with_sources_of_truth({TABLE: "dynamodb"})
        )
        no_gate = score_query_for_engine(
            CNT,
            "dynamodb",
            ScoringContext(
                analysis_outputs=ctx.analysis_outputs,
                anti_pattern_map=ctx.anti_pattern_map,
                pattern_model=ctx.pattern_model,
                source_engine=ctx.source_engine,
                cost_model=ctx.cost_model,
            ),
        )
        assert waived.score == no_gate.score

    def test_the_standing_cost_share_never_counts_a_waiver(self) -> None:
        """ADR-031 Decision 5: an engine's standing share is over the queries it could
        serve, a count that must not depend on the assignment the score decides."""
        # Two contexts: the cost model memoises the set per engine.
        waived = _ctx([INS, UPD, CNT]).with_sources_of_truth({TABLE: "dynamodb"})
        assert score_query_for_engine(CNT, "dynamodb", waived).score is not None
        assert "cnt" not in waived.servable_queries("dynamodb")
        assert waived.servable_queries("dynamodb") == _ctx([INS, UPD, CNT]).servable_queries(
            "dynamodb"
        )


# ---------------------------------------------------------------------------
# Sources of truth
# ---------------------------------------------------------------------------


class TestSourcesOfTruth:
    def test_a_table_whose_writes_share_one_engine_has_it(self) -> None:
        groups = {TABLE: ["ins", "upd"], "forum.topics": ["t1", "t2"], "forum.tags": ["g1"]}
        engine_of = {"ins": "dynamodb", "upd": "dynamodb", "t1": "dynamodb", "t2": "aurora_mysql"}
        # A split table has no source of truth; neither has one with no placed write.
        assert sources_of_truth(groups, engine_of) == {TABLE: "dynamodb"}

    def test_an_assignment_gives_its_in_scope_writes_sources(self) -> None:
        assignment = {
            "write_groups": {TABLE: ["ins", "upd"]},
            "query_assignments": [
                {"query_id": "ins", "assigned_engine": "dynamodb"},
                {"query_id": "upd", "assigned_engine": "dynamodb"},
            ],
        }
        assert assignment_sources_of_truth(assignment) == {TABLE: "dynamodb"}
        assert assignment_sources_of_truth({"query_assignments": []}) == {}
        assert assignment_sources_of_truth(None) == {}


# ---------------------------------------------------------------------------
# The first assignment (the real resolver and the real gate; base scores patched)
# ---------------------------------------------------------------------------

ENGINES = ("aurora_postgresql", "documentdb", "dynamodb")


def _resolve(
    monkeypatch: pytest.MonkeyPatch,
    queries: list[dict],
    base: dict[tuple[str, str], int],
):
    """The real resolver and gates over ``queries``; each base confidence patched to
    ``base`` (a missing pair is 10). ``cnt`` and ``mx`` need ``aggregation``."""

    def fake(query_id, query, engine, analysis, **_kwargs):
        return base.get((query_id, engine), 10)

    monkeypatch.setattr(query_scoring, "compute_query_confidence", fake)
    monkeypatch.setattr(assignment_resolver, "compute_query_confidence", fake)
    caps = {q["query_id"]: ["aggregation"] for q in queries if q["query_id"] in AGGREGATES}
    trace = RoutingTrace()
    result = AssignmentResolver().resolve(
        {
            "selected_agents": [{"agent_type": e} for e in ENGINES],
            "signals": [],
            "query_capabilities": caps,
        },
        {
            e: {
                "table_recommendations": [{"table_id": TABLE, "confidence_score": 50}],
                "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
            }
            for e in ENGINES
        },
        _collector(queries),
        routing_trace=trace,
    )
    return result, trace


WRITES_ON_DYNAMODB = {
    ("ins", "dynamodb"): 90,
    ("ins", "aurora_postgresql"): 60,
    ("upd", "dynamodb"): 90,
    ("upd", "aurora_postgresql"): 60,
}
WRITES_ON_AURORA = {
    ("ins", "dynamodb"): 60,
    ("ins", "aurora_postgresql"): 90,
    ("upd", "dynamodb"): 60,
    ("upd", "aurora_postgresql"): 90,
}


def _by_id(result) -> dict[str, Any]:
    return {qa.query_id: qa for qa in result.query_assignments}


class TestTheFirstAssignment:
    def test_a_count_follows_its_tables_writes_onto_dynamodb_when_it_scores_best(
        self, monkeypatch
    ) -> None:
        base = {
            **WRITES_ON_DYNAMODB,
            ("cnt", "dynamodb"): 85,
            ("cnt", "aurora_postgresql"): 70,
            ("cnt", "documentdb"): 60,
        }
        result, trace = _resolve(monkeypatch, [INS, UPD, CNT], base)
        qas = _by_id(result)
        assert qas["ins"].assigned_engine == qas["upd"].assigned_engine == "dynamodb"
        assert qas["cnt"].assigned_engine == "dynamodb"
        assert qas["cnt"].tied_engines == []
        moves = [m for m in result.moves if m.query_id == "cnt"]
        # Placed once, after the writes: one first-assignment Move, never a placement
        # with DynamoDB gated that the waiver then undoes.
        assert [(m.pass_, m.reason_code) for m in moves] == [
            (MovePass.SCORE, ReasonCode.KEY_SCOPED_COUNT),
        ]
        last = moves[-1]
        assert (last.from_engine, last.to_engine) == (None, "dynamodb")
        assert (last.score_before, last.score_after) == (None, 85)
        assert last.runner_up is not None and last.runner_up.engine == "aurora_postgresql"
        assert last.evidence.capability_waivers == [
            CapabilityWaiver(
                rule_id="ddb-key-scoped-count",
                capability="aggregation",
                table=TABLE,
                source_of_truth="dynamodb",
            )
        ]
        assert {c.engine: c.score for c in last.evidence.candidates}["dynamodb"] == 85
        assert "key-scoped count of forum.posts, whose writes are on DynamoDB" in (
            qas["cnt"].assignment_reason
        )
        # The snapshot ties are judged by counts the count where Step 6 would have put
        # it, as before #377, so waiting for the writes changes no other tie.
        assert dict(trace.settled_counts) == {"dynamodb": 2, "aurora_postgresql": 1}
        entry = trace.by_query["cnt"]
        assert entry["engines"]["dynamodb"] == 85
        assert entry["capability_waivers"]["dynamodb"][0]["rule_id"] == "ddb-key-scoped-count"
        assert rule_label(ReasonCode.KEY_SCOPED_COUNT) != "routing rule"

    def test_a_count_stays_gated_when_its_tables_writes_are_elsewhere(self, monkeypatch) -> None:
        """No table is pulled off another engine for one count (the issue's rejected
        outcome): with the writes on Aurora PostgreSQL, DynamoDB stays gated."""
        base = {**WRITES_ON_AURORA, ("cnt", "dynamodb"): 99, ("cnt", "aurora_postgresql"): 70}
        result, trace = _resolve(monkeypatch, [INS, UPD, CNT], base)
        qas = _by_id(result)
        assert qas["ins"].assigned_engine == "aurora_postgresql"
        assert qas["cnt"].assigned_engine == "aurora_postgresql"
        assert trace.by_query["cnt"]["engines"]["dynamodb"] == {"gate": "capability"}
        assert "capability_waivers" not in trace.by_query["cnt"]
        assert ReasonCode.KEY_SCOPED_COUNT not in {m.reason_code for m in result.moves}

    def test_the_scorer_still_decides(self, monkeypatch) -> None:
        """Passing the gate is not winning: a count that scores higher on Aurora stays."""
        base = {**WRITES_ON_DYNAMODB, ("cnt", "dynamodb"): 70, ("cnt", "aurora_postgresql"): 80}
        result, trace = _resolve(monkeypatch, [INS, UPD, CNT], base)
        assert _by_id(result)["cnt"].assigned_engine == "aurora_postgresql"
        assert [m.reason_code for m in result.moves if m.query_id == "cnt"] == [
            ReasonCode.HIGHEST_SCORE
        ]
        # The trace still shows the score the waiver let DynamoDB have.
        assert trace.by_query["cnt"]["engines"]["dynamodb"] == 70

    def test_min_and_max_wait(self, monkeypatch) -> None:
        base = {**WRITES_ON_DYNAMODB, ("mx", "dynamodb"): 99, ("mx", "aurora_postgresql"): 70}
        result, trace = _resolve(monkeypatch, [INS, UPD, MAX], base)
        assert _by_id(result)["mx"].assigned_engine == "aurora_postgresql"
        assert trace.by_query["mx"]["engines"]["dynamodb"] == {"gate": "capability"}

    def test_an_exact_tie_goes_through_the_tie_break(self, monkeypatch) -> None:
        """DynamoDB and DocumentDB tie at 80: neither is source-compatible, so the tie
        stays open for the customer (Decision 12's placeholder), on one Move."""
        base = {
            **WRITES_ON_DYNAMODB,
            ("cnt", "dynamodb"): 80,
            ("cnt", "documentdb"): 80,
            ("cnt", "aurora_postgresql"): 70,
        }
        result, trace = _resolve(monkeypatch, [INS, UPD, CNT], base)
        cnt = _by_id(result)["cnt"]
        assert cnt.tied_engines == ["documentdb", "dynamodb"]
        last = [m for m in result.moves if m.query_id == "cnt"][-1]
        assert last.pass_ == MovePass.TIE
        assert last.reason_code in (
            ReasonCode.TIE_PLACEHOLDER_SETTLED_WORKLOAD,
            ReasonCode.TIE_PLACEHOLDER_HASH,
        )
        assert trace.tie_break_by_query["cnt"] == "tie_unresolved"

    def test_a_tie_with_the_source_compatible_engine_keeps_it(self, monkeypatch) -> None:
        """Decision 2: on an exact tie the source-compatible engine wins, waiver or not."""
        base = {**WRITES_ON_DYNAMODB, ("cnt", "dynamodb"): 80, ("cnt", "aurora_postgresql"): 80}
        result, _ = _resolve(monkeypatch, [INS, UPD, CNT], base)
        cnt = _by_id(result)["cnt"]
        assert cnt.assigned_engine == "aurora_postgresql"
        assert cnt.tied_engines == []
        assert ReasonCode.KEY_SCOPED_COUNT not in {m.reason_code for m in result.moves}

    def test_a_count_in_a_co_dependency_group_moves_with_its_group(self, monkeypatch) -> None:
        """Decision 4: a group places as one unit, so the waiver never re-picks a
        member alone (here the group's other member keeps it off DynamoDB)."""
        other = _q("other", "SELECT * FROM posts WHERE id = $1")
        monkeypatch.setattr(
            assignment_resolver, "build_co_dependency_groups", lambda q, t: [["cnt", "other"]]
        )
        base = {
            **WRITES_ON_DYNAMODB,
            ("cnt", "dynamodb"): 99,
            ("cnt", "aurora_postgresql"): 70,
            ("other", "dynamodb"): 50,
            ("other", "aurora_postgresql"): 70,
        }
        result, _ = _resolve(monkeypatch, [INS, UPD, CNT, other], base)
        qas = _by_id(result)
        assert qas["cnt"].assigned_engine == qas["other"].assigned_engine == "aurora_postgresql"
        assert ReasonCode.KEY_SCOPED_COUNT not in {m.reason_code for m in result.moves}

    def test_a_signal_override_stays_pinned(self, monkeypatch) -> None:
        monkeypatch.setattr(
            AssignmentResolver,
            "_build_signal_overrides",
            lambda self, *a: {"cnt": {"engine": "documentdb", "signal": "text_search"}},
        )
        base = {**WRITES_ON_DYNAMODB, ("cnt", "dynamodb"): 99, ("cnt", "documentdb"): 50}
        result, _ = _resolve(monkeypatch, [INS, UPD, CNT], base)
        assert _by_id(result)["cnt"].assigned_engine == "documentdb"
        assert ReasonCode.KEY_SCOPED_COUNT not in {m.reason_code for m in result.moves}

    def test_the_first_assignment_replays(self, monkeypatch) -> None:
        """Every engine change is a ledger Move: v1's Moves replay to its engines and
        ties (the resolver checks it before writing; checked here on the waiver's)."""
        from src.agents.referee.move_ledger import check_replay, state_of

        for cnt_scores in ({"dynamodb": 85, "documentdb": 60}, {"dynamodb": 80, "documentdb": 80}):
            base = {
                **WRITES_ON_DYNAMODB,
                ("cnt", "aurora_postgresql"): 70,
                **{("cnt", e): s for e, s in cnt_scores.items()},
            }
            result, _ = _resolve(monkeypatch, [INS, UPD, CNT], base)
            check_replay(result.moves, {}, state_of(result.query_assignments), "test")


# ---------------------------------------------------------------------------
# The reality check's eligibility reads the trace's waivers
# ---------------------------------------------------------------------------


def test_the_reality_check_eligibility_honours_a_traced_waiver() -> None:
    qa: dict[str, Any] = {"query_id": "cnt", "assigned_engine": "dynamodb"}
    entry: dict[str, Any] = {"engines": {"dynamodb": 85, "aurora_postgresql": 70}}
    trace: dict[str, dict] = {"cnt": entry}
    caps = {"cnt": ["aggregation"]}

    def eligible() -> list[str]:
        return _query_eligible_engines(
            "cnt", qa, "dynamodb", ["dynamodb", "aurora_postgresql"], trace, {}, {"cnt": CNT}, caps
        )

    assert eligible() == ["aurora_postgresql"]
    entry["capability_waivers"] = {
        "dynamodb": [{"rule_id": "ddb-key-scoped-count", "capability": "aggregation"}]
    }
    assert eligible() == ["dynamodb", "aurora_postgresql"]


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------


def test_the_catalog_holds_one_waiver() -> None:
    (waiver,) = waivers()
    assert (waiver.engine, waiver.capability, waiver.fact, waiver.when) == (
        "dynamodb",
        "aggregation",
        KEY_SCOPED_COUNT_FACT,
        "source_of_truth",
    )


BAD_WAIVERS = [
    ('capability = "nope"', "unknown"),
    ('capability = "multi_doc_acid"', "already has"),
    ('fact = "nope"', "unknown fact"),
    ('engine = "elasticache"', "owns no writes"),
]


@pytest.mark.parametrize(("change", "message"), BAD_WAIVERS)
def test_the_loader_rejects_a_bad_waiver(tmp_path: Path, change: str, message: str) -> None:
    key = change.split(" = ")[0]
    text = GATES_FILE.read_text()
    start = text.index("[[waivers]]")
    line_start = text.index(f"\n{key} = ", start) + 1
    line_end = text.index("\n", line_start)
    path = tmp_path / "gates.toml"
    path.write_text(text[:line_start] + change + text[line_end:])
    with pytest.raises(CatalogError, match=message):
        load_gates_file(path)


def test_a_move_written_before_the_waiver_still_loads() -> None:
    """The move ledger change is additive: evidence written before it has no ``capability_waivers``."""
    assert MoveEvidence.model_validate({"candidates": []}).capability_waivers == []


# ---------------------------------------------------------------------------
# Every later pass reads the sources of truth of the version it routes
# ---------------------------------------------------------------------------


def _assignment(write_engine: str, count_engine: str, count_tied: list[str] | None = None):
    from datetime import UTC, datetime

    from src.contracts.assignment_models import (
        Assignment,
        AssignmentSource,
        AssignmentStatus,
        QueryAssignment,
    )

    def qa(qid: str, engine: str, tied: list[str] | None = None) -> QueryAssignment:
        return QueryAssignment(
            query_id=qid,
            assigned_engine=engine,
            confidence=80,
            source_tables=[TABLE],
            assignment_reason="test",
            tied_engines=tied or [],
        )

    return Assignment(
        job_id="job-1",
        version=1,
        status=AssignmentStatus.AUTO_GENERATED,
        source=AssignmentSource.ASSIGNMENT_RESOLUTION,
        timestamp=datetime.now(UTC),
        query_assignments=[
            qa("ins", write_engine),
            qa("upd", write_engine),
            qa("cnt", count_engine, count_tied),
        ],
        table_assignments=[],
        co_dependency_groups=[],
        validation_warnings=[],
        write_groups={TABLE: ["ins", "upd"]},
    )


ANALYSES = {
    e: {
        "table_recommendations": [{"table_id": TABLE, "confidence_score": 70}],
        "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
    }
    for e in ENGINES
}
TRIAGE = {"signals": [], "query_capabilities": {"cnt": ["aggregation"]}}


class TestLaterPasses:
    def test_the_routed_fit_scores_a_count_on_its_tables_source_of_truth(self, monkeypatch) -> None:
        """Synthesis's routed fit is the scorer's score on the engine the query is on;
        a count the waiver placed on DynamoDB is scored, never shown as gated (0)."""
        from src.agents.referee.routed_confidence import routed_fits

        monkeypatch.setattr(query_scoring, "compute_query_confidence", lambda *a, **k: 80)

        assignment = _assignment("dynamodb", "dynamodb").model_dump(mode="json")

        def fit(version: dict) -> float | None:
            return routed_fits(
                version,
                TRIAGE,
                [INS, UPD, CNT],
                ANALYSES,
                source_engine="postgresql",
                schema_tables=[POSTS],
            )["dynamodb"].confidence

        with_sources, without = fit(assignment), fit({**assignment, "write_groups": {}})
        assert with_sources is not None and without is not None
        assert with_sources > without

    def test_a_pending_choice_scores_the_waived_engine(self) -> None:
        """A tied count's pending choice (Amendment 10) shows DynamoDB's score."""
        from src.agents.referee.assignment_overrides import pending_choices_from

        assignment = _assignment("dynamodb", "dynamodb", ["documentdb", "dynamodb"])
        (choice,) = pending_choices_from(
            assignment.model_dump(mode="json"), _collector([INS, UPD, CNT]), ANALYSES, TRIAGE
        )
        scores = {e.engine: e.score for e in choice.options}
        assert scores["dynamodb"] is not None and scores["documentdb"] is not None

    @pytest.mark.parametrize(
        ("write_engine", "accepted"), [("dynamodb", True), ("aurora_postgresql", False)]
    )
    def test_a_customer_pick_onto_the_source_of_truth_passes_the_gate(
        self, write_engine: str, accepted: bool
    ) -> None:
        from src.agents.referee.assignment_overrides import (
            ExcludedEngineOverride,
            QueryOverrideInput,
            apply_assignment_overrides,
        )
        from tests.unit.test_assignment_overrides import DB, JOB, _MemStore

        store = _MemStore()
        store.write_json(
            f"{DB}/{JOB}/assignment/v1/assignment.json",
            _assignment(write_engine, "aurora_postgresql").model_dump(mode="json"),
        )
        store.write_json(f"{DB}/{JOB}/collector/output.json", _collector([INS, UPD, CNT]))
        store.write_json(f"{DB}/{JOB}/referee-triage/triage.json", TRIAGE)
        for engine, analysis in ANALYSES.items():
            store.write_json(f"{DB}/{JOB}/analysis-{engine}/analysis.json", analysis)
        pick = [QueryOverrideInput("cnt", assigned_engine="dynamodb")]
        if accepted:
            result = apply_assignment_overrides(store, DB, JOB, pick)
            engines = {
                qa.query_id: qa.assigned_engine for qa in result.assignment.query_assignments
            }
            assert engines["cnt"] == "dynamodb"
        else:
            with pytest.raises(ExcludedEngineOverride) as exc:
                apply_assignment_overrides(store, DB, JOB, pick)
            assert [(e.query_id, e.gate_kind) for e in exc.value.excluded] == [
                ("cnt", "capability")
            ]

    def test_the_reality_check_handler_reads_the_revised_sources(self) -> None:
        """The sweep and a model's flag score with the sources of truth as the check
        has placed them so far, not as the first assignment had them."""
        from src.agents.referee.reality_check_handler import _scoring_inputs

        det = {
            "assignment": {"write_groups": {TABLE: ["ins", "upd"]}},
            "revised_assignments": [
                {"query_id": "ins", "assigned_engine": "dynamodb"},
                {"query_id": "upd", "assigned_engine": "dynamodb"},
                {"query_id": "cnt", "assigned_engine": "dynamodb"},
            ],
            "triage": TRIAGE,
            "collector_output": _collector([INS, UPD, CNT]),
            "analysis_outputs": ANALYSES,
        }
        _, ctx = _scoring_inputs(det)
        assert ctx.sources_of_truth == {TABLE: "dynamodb"}
        assert score_query_for_engine(CNT, "dynamodb", ctx).score is not None

    @pytest.mark.parametrize(
        ("write_engine", "pinned", "count_engine"),
        [
            # A customer moved the table's writes off DynamoDB: the count loses the waiver.
            ("aurora_postgresql", True, "aurora_postgresql"),
            # A customer pinned the writes on DynamoDB: they still count, held out or not.
            ("dynamodb", True, "dynamodb"),
            # The writes left where routing put them: the count keeps the waiver.
            ("dynamodb", False, "dynamodb"),
        ],
    )
    def test_the_reality_check_reads_its_input_versions_sources(
        self, monkeypatch, write_engine: str, pinned: bool, count_engine: str
    ) -> None:
        """Phase 1/2 eligibility, the trace's scores and the floors read the input
        version's sources of truth, customer-pinned writes included, not the first
        assignment's (the resolver re-run would put these writes on DynamoDB)."""
        from src.agents.referee.reality_check_handler import run_reality_check_deterministic
        from tests.unit.test_assignment_overrides import _MemStore

        base = {
            **WRITES_ON_DYNAMODB,
            ("cnt", "dynamodb"): 85,
            ("cnt", "aurora_postgresql"): 70,
            ("rd", "dynamodb"): 95,
            ("rd", "aurora_postgresql"): 50,
            ("grp", "aurora_postgresql"): 90,
        }

        def fake(query_id, query, engine, analysis, **_kwargs):
            return base.get((query_id, engine), 10)

        monkeypatch.setattr(query_scoring, "compute_query_confidence", fake)
        monkeypatch.setattr(assignment_resolver, "compute_query_confidence", fake)
        read = _q("rd", "SELECT * FROM posts WHERE id = $1")
        # Keeps Aurora PostgreSQL in the plan, so the count has somewhere to go.
        grouped = _q("grp", "SELECT post_type, COUNT(*) FROM posts GROUP BY post_type")
        version = _assignment(write_engine, "dynamodb").model_dump(mode="json")
        for qa in version["query_assignments"]:
            if qa["query_id"] in ("ins", "upd"):
                qa["customer_override"] = pinned
        for qid, engine in (("rd", "dynamodb"), ("grp", "aurora_postgresql")):
            version["query_assignments"].append(
                {**version["query_assignments"][-1], "query_id": qid, "assigned_engine": engine}
            )
        store = _MemStore()
        prefix = "forum/job-1"
        store.write_json(f"{prefix}/assignment/v1/assignment.json", version)
        store.write_json(
            f"{prefix}/collector/output.json", _collector([INS, UPD, CNT, read, grouped])
        )
        store.write_json(
            f"{prefix}/referee-triage/triage.json",
            {
                "signals": [],
                "query_capabilities": {"cnt": ["aggregation"], "grp": ["aggregation"]},
                "selected_agents": [{"agent_type": e} for e in ENGINES],
            },
        )
        for engine, analysis in ANALYSES.items():
            store.write_json(f"{prefix}/analysis-{engine}/analysis.json", analysis)
        det = run_reality_check_deterministic(
            "job-1", "forum", store, assignment_version=1  # type: ignore[arg-type]
        )
        engines = {qa["query_id"]: qa["assigned_engine"] for qa in det["revised_assignments"]}
        assert engines["ins"] == engines["upd"] == write_engine
        assert engines["rd"] == "dynamodb"
        assert engines["grp"] == "aurora_postgresql"
        assert engines["cnt"] == count_engine

    def test_the_post_schema_router_scores_with_the_approved_versions_sources(self) -> None:
        from src.agents.referee.post_schema_routing_handler import PostSchemaRouting
        from tests.unit.test_assignment_overrides import _MemStore

        store = _MemStore()
        prefix = "forum/job-1"
        store.write_json(
            f"{prefix}/assignment/v1/assignment.json",
            _assignment("dynamodb", "dynamodb").model_dump(mode="json"),
        )
        store.write_json(f"{prefix}/collector/output.json", _collector([INS, UPD, CNT]))
        store.write_json(f"{prefix}/referee-triage/triage.json", TRIAGE)
        for engine, analysis in ANALYSES.items():
            store.write_json(f"{prefix}/analysis-{engine}/analysis.json", analysis)
        inputs = PostSchemaRouting(store, _no_redesign)._router_scoring_inputs(  # type: ignore[arg-type]
            "job-1", "forum", list(ENGINES), 1
        )
        ctx = inputs["scoring_context"]
        assert ctx.sources_of_truth == {TABLE: "dynamodb"}
        assert score_query_for_engine(CNT, "dynamodb", ctx).score is not None

    def test_the_legacy_cache_owner_normalisation_reads_the_versions_sources(
        self, monkeypatch
    ) -> None:
        """A pre-#296 version with ElastiCache owning the count: the owner it gets is
        the shared scorer's, with the waiver read from that version's writes."""
        from src.agents.referee.cache_overlay import normalize_cache_owners

        monkeypatch.setattr(
            query_scoring,
            "compute_query_confidence",
            lambda qid, q, engine, *a, **k: {"dynamodb": 85, "aurora_postgresql": 70}.get(
                engine, 10
            ),
        )
        version = _assignment("dynamodb", "elasticache").model_dump(mode="json")
        engines = {**ANALYSES, "elasticache": ANALYSES["dynamodb"]}
        normalize_cache_owners(
            version,
            [INS, UPD, CNT],
            engines,
            "postgresql",
            triage=TRIAGE,
            collector=_collector([INS, UPD, CNT]),
        )
        owner = {qa["query_id"]: qa["assigned_engine"] for qa in version["query_assignments"]}
        assert owner["cnt"] == "dynamodb"

    @pytest.mark.parametrize(
        ("write_engine", "absorber"),
        [("dynamodb", "dynamodb"), ("aurora_postgresql", "aurora_postgresql")],
    )
    def test_the_floors_absorber_reads_the_input_versions_sources(
        self, monkeypatch, write_engine: str, absorber: str
    ) -> None:
        """OpenSearch's justification floor moves a count with no search depth off it;
        its absorber (``_find_best_absorber_for_query``) scores DynamoDB with the
        check's own context, so DynamoDB takes the count only when the input
        version's writes are there (an empty sources map would gate it)."""
        from src.agents.referee.reality_check import run_reality_check

        base = {
            ("ins", "dynamodb"): 90 if write_engine == "dynamodb" else 50,
            ("upd", "dynamodb"): 90 if write_engine == "dynamodb" else 50,
            ("ins", "aurora_postgresql"): 60 if write_engine == "dynamodb" else 95,
            ("upd", "aurora_postgresql"): 60 if write_engine == "dynamodb" else 95,
            ("cnt", "dynamodb"): 85,
            ("cnt", "aurora_postgresql"): 70,
            ("cnt", "opensearch"): 60,
            ("rd", "dynamodb"): 95,
            ("grp", "aurora_postgresql"): 90,
        }

        def fake(query_id, query, engine, analysis, **_kwargs):
            return base.get((query_id, engine), 10)

        monkeypatch.setattr(query_scoring, "compute_query_confidence", fake)
        monkeypatch.setattr(assignment_resolver, "compute_query_confidence", fake)
        engines = (*ENGINES, "opensearch")
        analyses = {e: ANALYSES["dynamodb"] for e in engines}
        read = _q("rd", "SELECT * FROM posts WHERE id = $1")
        grouped = _q("grp", "SELECT post_type, COUNT(*) FROM posts GROUP BY post_type")
        version = _assignment(write_engine, "opensearch").model_dump(mode="json")
        qas = version["query_assignments"]
        for qa in qas:
            if qa["query_id"] == "cnt":
                qa["signal_override"] = "text_search"
        for qid, engine in (("rd", "dynamodb"), ("grp", "aurora_postgresql")):
            qas.append({**qas[0], "query_id": qid, "assigned_engine": engine})
        result = run_reality_check(
            version,
            {
                "signals": [],
                "query_capabilities": {"cnt": ["aggregation"], "grp": ["aggregation"]},
                "selected_agents": [{"agent_type": e} for e in engines],
            },
            analyses,
            _collector([INS, UPD, CNT, read, grouped]),
            # Both owners stay in the plan (as a customer pin keeps an engine,
            # #860), so the floor's absorber chooses between them on score alone.
            customer_kept_engines=["dynamodb", "aurora_postgresql"],
        )
        placed = {qa["query_id"]: qa for qa in result["revised_assignments"]}
        assert placed["cnt"]["assigned_engine"] == absorber
        assert any(
            m.reason_code == ReasonCode.FLOOR_OPENSEARCH
            for m in result["move_ledger"].moves
            if m.query_id == "cnt"
        )


# ---------------------------------------------------------------------------
# The DynamoDB designer is told each key-scoped count's key and its cardinality
# (no cardinality gate in routing; the maintainer's call on #1104, applied on #1150)
# ---------------------------------------------------------------------------


class TestTheDesignerInput:
    def test_the_analysis_lists_each_count_its_key_and_cardinality(self) -> None:
        from src.tools.analysis.dynamodb_analysis_tools import key_scoped_counts

        collector = _collector([INS, UPD, CNT, MAX])
        (count,) = key_scoped_counts(collector)
        assert (count.query_id, count.table_id, count.columns) == ("cnt", TABLE, ["topic_id"])
        assert [(k.column, k.cardinality) for k in count.keys] == [("topic_id", "unknown")]
        assert "Select = COUNT" in count.schema_note
        assert "every matching item" in count.schema_note
        columns: list[dict[str, Any]] = [dict(c) for c in POSTS["columns"]]
        columns[1]["cardinality"] = 4
        posts = {**POSTS, "columns": columns}
        collector["database_schema"]["tables"] = [posts]
        (count,) = key_scoped_counts(collector)
        assert [(k.column, k.cardinality) for k in count.keys] == [("topic_id", 4)]

    def test_the_dynamodb_analysis_carries_them(self) -> None:
        from src.agents.analysis.dynamodb_analysis_agent import analyze_for_dynamodb_deterministic
        from src.contracts.analysis_input import AnalysisInput, TargetDatabase

        contract, _, _ = analyze_for_dynamodb_deterministic(
            AnalysisInput(
                job_id="job-1",
                collector_output=_collector([INS, UPD, CNT]),
                target_database=TargetDatabase.DYNAMODB,
            )
        )
        assert [c.query_id for c in contract.key_scoped_counts or []] == ["cnt"]

    def test_a_design_call_sees_only_its_own_counts(self) -> None:
        from src.contracts.analysis_output import key_scoped_counts_in_scope
        from src.tools.analysis.dynamodb_analysis_tools import key_scoped_counts

        raw = [c.model_dump(mode="json") for c in key_scoped_counts(_collector([CNT]))]
        assert [c["query_id"] for c in key_scoped_counts_in_scope(raw, {"cnt"})] == ["cnt"]
        assert key_scoped_counts_in_scope(raw, {"ins"}) == []
        assert key_scoped_counts_in_scope(None, {"cnt"}) == []

    def test_each_design_group_carries_its_own_counts(self) -> None:
        from src.agents.schema_design.group_input import build_group_input
        from src.tools.analysis.dynamodb_analysis_tools import key_scoped_counts

        collector = _collector([INS, UPD, CNT])
        analysis = {
            "contract_version": "2.2",
            "table_recommendations": [],
            "workload_analysis": {"patterns_detected": []},
            "key_scoped_counts": [c.model_dump(mode="json") for c in key_scoped_counts(collector)],
        }

        def group(queries: list[dict]) -> dict[str, Any]:
            built: dict[str, Any] = build_group_input(
                job_id="job-1",
                database_name="forum",
                engine="dynamodb",
                group_index=1,
                group_name="posts",
                primary_tables=[TABLE],
                group_queries=queries,
                group_tables=[POSTS],
                collector_output=collector,
                analysis_output=analysis,
                total_queries=3,
            )["analysis_output"]
            return built

        assert [c["query_id"] for c in group([INS, CNT])["key_scoped_counts"]] == ["cnt"]
        assert "key_scoped_counts" not in group([INS, UPD])

    def test_the_external_request_carries_only_the_engines_counts(self) -> None:
        from src.agents.schema_design.handler import prepare_schema_design_input
        from src.tools.analysis.dynamodb_analysis_tools import key_scoped_counts
        from tests.unit.test_assignment_overrides import _MemStore

        store = _MemStore()
        version = _assignment("dynamodb", "aurora_postgresql").model_dump(mode="json")
        store.write_json("forum/job-1/assignment/v1/assignment.json", version)
        collector = _collector([INS, UPD, CNT])
        store.write_json("forum/job-1/collector/output.json", collector)
        counts = [c.model_dump(mode="json") for c in key_scoped_counts(collector)]
        store.write_json(
            "forum/job-1/analysis-dynamodb/analysis.json", {"key_scoped_counts": counts}
        )
        payload = prepare_schema_design_input(
            "job-1", "forum", "dynamodb", store, assignment_version=1  # type: ignore[arg-type]
        )
        # The count is on Aurora PostgreSQL in this version, so DynamoDB's designer
        # is not given it.
        assert payload["analysis_output"]["key_scoped_counts"] == []
        version = _assignment("dynamodb", "dynamodb").model_dump(mode="json")
        store.write_json("forum/job-1/assignment/v1/assignment.json", version)
        payload = prepare_schema_design_input(
            "job-1", "forum", "dynamodb", store, assignment_version=1  # type: ignore[arg-type]
        )
        assert [c["query_id"] for c in payload["analysis_output"]["key_scoped_counts"]] == ["cnt"]


def _no_redesign(*_args: object) -> None:
    raise AssertionError("the post-schema routing redesigns nothing in this test")
