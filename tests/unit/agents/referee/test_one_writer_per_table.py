"""#992: a table has one writer; its reads stay per query (ADR-031 Amendment 9).

Maintainer decision (2026-10-17): all of a table's write paths (INSERT, UPDATE,
DELETE, MERGE, and the locking reads that precede a write) land on one engine,
the table's source of truth. Other engines may serve its reads as eventually
consistent replicas fed by CDC from the source of truth (CQRS).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from src.agents.referee import assignment_resolver
from src.agents.referee.assignment_overrides import (
    ExcludedEngineOverride,
    QueryOverrideInput,
    apply_assignment_overrides,
)
from src.agents.referee.assignment_resolver import AssignmentResolver, RoutingTrace
from src.agents.referee.feasibility_review import review_assignment_feasibility
from src.agents.referee.migration_wave_tables import _durable_owner, _table_owners_for_served
from src.agents.referee.query_scoring import EngineScore
from src.agents.referee.table_assignments import derive_table_assignments
from src.agents.referee.write_groups import (
    assignment_placement_groups,
    build_write_groups,
    placement_groups,
    table_sources,
    unit_write_tables,
    written_tables,
)
from src.contracts.assignment_models import (
    Assignment,
    AssignmentSource,
    AssignmentStatus,
    QueryAssignment,
)
from src.contracts.feasibility_models import FindingKind, FindingSeverity
from src.contracts.move_ledger import MovePass, ReasonCode
from src.shared.move_reasons import render_move_reason
from src.shared.report_facts import table_source_facts
from src.tools.sql.facade import sql_facts
from src.tools.sql.query_facts import query_facts
from src.tools.sql.regex_facts import regex_write_targets

# ---------------------------------------------------------------------------
# The write-target fact (one definition, ADR-031 Decision 8)
# ---------------------------------------------------------------------------

WRITE_TARGETS = [
    ("postgresql", "INSERT INTO orders (id, a) VALUES ($1, $2)", ("orders",)),
    ("postgresql", "INSERT INTO archive SELECT * FROM orders WHERE id = $1", ("archive",)),
    ("postgresql", "UPDATE posts p SET x = 1 FROM topics t WHERE p.topic_id = t.id", ("posts",)),
    ("postgresql", "DELETE FROM posts USING topics WHERE posts.topic_id = topics.id", ("posts",)),
    (
        "postgresql",
        "WITH w AS (INSERT INTO user_badges (badge_id) SELECT id FROM badges RETURNING id) "
        "SELECT * FROM w",
        ("user_badges",),
    ),
    ("postgresql", "SELECT * FROM orders WHERE id = $1 FOR UPDATE", ()),
    ("postgresql", "SELECT * FROM orders WHERE id = $1", ()),
    ("mysql", "INSERT INTO t (a) VALUES (?) ON DUPLICATE KEY UPDATE a = ?", ("t",)),
    ("mysql", "DELETE a FROM wp_posts a JOIN wp_x b ON a.id = b.id WHERE b.c = ?", ("wp_posts",)),
    ("sqlserver", "UPDATE t SET x = 1 FROM [Ticket] t WHERE t.id = @id", ("ticket",)),
    ("mysql", "UPDATE a JOIN b ON a.id = b.aid SET a.x = ?, b.y = ?", ("a", "b")),
    ("mysql", "UPDATE a, b SET a.x = ?, b.y = ? WHERE a.id = b.aid", ("a", "b")),
    ("mysql", "UPDATE a JOIN b ON a.id = b.aid SET b.y = ?", ("b",)),
    ("mysql", "UPDATE wp_posts p JOIN wp_x x ON p.id = x.pid SET p.a = ?", ("wp_posts",)),
    ("postgresql", "UPDATE posts SET x = 1 WHERE id IN (SELECT id FROM topics)", ("posts",)),
    (
        "sqlserver",
        "MERGE INTO dbo.StockLevel AS tgt USING (SELECT 1 AS a) src ON tgt.a = src.a "
        "WHEN MATCHED THEN UPDATE SET qty = 1;",
        ("stocklevel",),
    ),
]


@pytest.mark.parametrize(("engine", "sql", "targets"), WRITE_TARGETS)
def test_the_write_targets_are_the_tables_a_statement_writes(engine, sql, targets) -> None:
    """A table the write only reads (an INSERT ... SELECT source, an UPDATE ... FROM
    table, a MERGE source, a locking read's table) is never a write target."""
    assert sql_facts(sql, engine).write_targets == targets
    assert query_facts(sql, engine).write_targets == targets


@pytest.mark.parametrize(("engine", "sql", "targets"), WRITE_TARGETS)
def test_the_regex_fallback_reads_the_same_targets(engine, sql, targets) -> None:
    assert regex_write_targets(sql) == targets


def test_a_replace_names_its_target_parsed_and_in_the_regex_fallback() -> None:
    """MySQL ``REPLACE INTO`` writes its table. #986 taught the parser to read it;
    the regex fallback, used when a statement does not parse, names the same table."""
    sql = "REPLACE INTO `wp_wc_product_meta_lookup` (`product_id`) VALUES (?)"
    assert sql_facts(sql, "mysql").parsed_by == "sqlglot"
    assert sql_facts(sql, "mysql").write_targets == ("wp_wc_product_meta_lookup",)
    assert query_facts(sql, "mysql").write_targets == ("wp_wc_product_meta_lookup",)
    assert regex_write_targets(sql) == ("wp_wc_product_meta_lookup",)


# ---------------------------------------------------------------------------
# Write groups and placement units
# ---------------------------------------------------------------------------


def _q(qid: str, text: str, tables: list[str], qtype: str | None = None) -> dict[str, Any]:
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": qtype or text.split()[0].upper(),
        "tables_accessed": tables,
        "calls_per_second": 1.0,
    }


class TestWrittenTables:
    def test_a_write_writes_its_target_only(self) -> None:
        q = _q("q1", "INSERT INTO a SELECT * FROM b WHERE b.id = $1", ["db.a", "db.b"])
        assert written_tables(q, "postgresql") == ["db.a"]

    def test_a_locking_read_locks_every_table_it_reads(self) -> None:
        q = _q(
            "q1",
            "SELECT * FROM a JOIN b ON a.id = b.a_id WHERE a.id = $1 FOR UPDATE",
            ["db.a", "db.b"],
        )
        assert written_tables(q, "postgresql") == ["db.a", "db.b"]

    def test_a_plain_read_writes_nothing(self) -> None:
        assert written_tables(_q("q1", "SELECT * FROM a WHERE id = $1", ["db.a"])) == []

    def test_an_administration_statement_joins_no_write_group(self) -> None:
        assert written_tables(_q("q1", "TRUNCATE TABLE a", ["db.a"], "OTHER"), "postgresql") == []

    def test_a_write_with_no_readable_target_keeps_all_its_tables(self) -> None:
        q = {"query_id": "q1", "query_type": "UPDATE", "tables_accessed": ["db.a", "db.b"]}
        assert written_tables(q) == ["db.a", "db.b"]


class TestPlacementGroups:
    def test_write_groups_are_listed_per_table_in_query_order(self) -> None:
        queries = [
            _q("w2", "UPDATE a SET x = $1 WHERE id = $2", ["db.a"]),
            _q("r1", "SELECT * FROM a WHERE id = $1", ["db.a"]),
            _q("w1", "INSERT INTO a (id) VALUES ($1)", ["db.a"]),
            _q("w3", "DELETE FROM b WHERE id = $1", ["db.b"]),
        ]
        assert build_write_groups(queries, "postgresql") == {
            "db.a": ["w2", "w1"],
            "db.b": ["w3"],
        }

    def test_an_untouched_co_dependency_group_comes_back_as_given(self) -> None:
        co = [["c2", "c1", "c3"]]
        assert placement_groups(co, {"db.a": ["w1", "w2"]}) == [["c2", "c1", "c3"], ["w1", "w2"]]

    def test_groups_that_share_a_query_merge(self) -> None:
        co = [["c1", "w1"]]
        units = placement_groups(co, {"db.a": ["w1", "w2"], "db.b": ["w2", "w3"]})
        assert units == [["c1", "w1", "w2", "w3"]]
        assert unit_write_tables(units[0], {"db.a": ["w1", "w2"], "db.b": ["w2", "w3"]}) == [
            "db.a",
            "db.b",
        ]

    def test_a_single_write_constrains_nothing(self) -> None:
        assert placement_groups([], {"db.a": ["w1"]}) == []
        assert unit_write_tables(["w1", "x"], {"db.a": ["w1"]}) == []

    def test_an_assignment_written_before_write_groups_gives_its_co_dependency_groups(self) -> None:
        assert assignment_placement_groups({"co_dependency_groups": [["a", "b"]]}) == [["a", "b"]]
        assert assignment_placement_groups(None) == []


# ---------------------------------------------------------------------------
# The first assignment
# ---------------------------------------------------------------------------

ENGINES = ("aurora_postgresql", "dynamodb", "opensearch")


def _resolve(
    monkeypatch: pytest.MonkeyPatch,
    queries: list[dict],
    scores: dict[tuple[str, str], float | None],
    engines: tuple[str, ...] = ENGINES,
):
    """The real resolver over ``queries``, the shared scorer patched to ``scores``
    (a missing pair scores 10; ``None`` is a gate)."""

    def fake(query, engine, ctx):
        score = scores.get((query["query_id"], engine), 10.0)
        if score is None:
            return EngineScore(None, "gated in this test", "capability")
        return EngineScore(score)

    monkeypatch.setattr(assignment_resolver, "score_query_for_engine", fake)
    trace = RoutingTrace()
    tables = sorted({t for q in queries for t in q["tables_accessed"]})
    result = AssignmentResolver().resolve(
        {"selected_agents": [{"agent_type": e} for e in engines], "signals": []},
        {
            e: {
                "table_recommendations": [{"table_id": t, "confidence_score": 50} for t in tables],
                "workload_analysis": {"patterns_detected": [], "anti_patterns_detected": []},
            }
            for e in engines
        },
        {
            "job_id": "t",
            # A heterogeneous source: no source-compatible engine, no Aurora guarantee.
            "metadata": {"source_database": {"engine": "sqlserver"}},
            "database_schema": {"tables": [{"table_id": t, "table_name": t} for t in tables]},
            "queries": {"query_patterns": queries},
        },
        routing_trace=trace,
    )
    return result, trace


SHIPMENT = [
    _q("ins", "INSERT INTO [Shipment] ([OrderId]) VALUES (@o)", ["trailgear.Shipment"]),
    _q(
        "upd",
        "UPDATE [Shipment] SET [DeliveredDate] = @d WHERE [TrackingNo] = @t",
        ["trailgear.Shipment"],
    ),
    _q("read", "SELECT * FROM [Shipment] WHERE [ShipmentId] = @id", ["trailgear.Shipment"]),
]


class TestTheFirstAssignment:
    def test_a_tables_insert_and_update_land_on_one_engine(self, monkeypatch) -> None:
        """trailgear's Shipment shape: the INSERT alone scores best on DynamoDB, the
        UPDATE alone on Aurora PostgreSQL. The write group goes to the engine with
        the best mean over both writes (Aurora PostgreSQL: 70 against 65)."""
        scores = {
            ("ins", "dynamodb"): 90.0,
            ("ins", "aurora_postgresql"): 60.0,
            ("upd", "dynamodb"): 40.0,
            ("upd", "aurora_postgresql"): 80.0,
            ("read", "dynamodb"): 95.0,
            ("read", "aurora_postgresql"): 50.0,
        }
        result, trace = _resolve(monkeypatch, SHIPMENT, scores)
        engines = {qa.query_id: qa.assigned_engine for qa in result.query_assignments}
        assert engines["ins"] == engines["upd"] == "aurora_postgresql"
        # Reads stay per query: the read keeps its own best engine, a replica.
        assert engines["read"] == "dynamodb"
        assert result.write_groups == {"trailgear.Shipment": ["ins", "upd"]}
        moves = {m.query_id: m for m in result.moves}
        for qid in ("ins", "upd"):
            assert moves[qid].pass_ == MovePass.CO_DEPENDENCY
            assert moves[qid].reason_code == ReasonCode.WRITE_GROUP
            # OpenSearch owns no write, so it is no candidate for the group.
            assert moves[qid].evidence.group_scores == {
                "aurora_postgresql": 70.0,
                "dynamodb": 65.0,
            }
            assert render_move_reason(moves[qid]).startswith(
                "Placed on Aurora PostgreSQL with its write group: all of a table's writes "
                "land on one engine, its source of truth"
            )
        assert moves["read"].reason_code == ReasonCode.HIGHEST_SCORE
        assert result.query_assignments[0].assignment_reason.startswith(
            "write group: one source of truth → Aurora PostgreSQL"
        )
        assert trace.by_query["ins"]["group"]["scores"] == {
            "aurora_postgresql": 70.0,
            "dynamodb": 65.0,
        }
        table = next(t for t in result.table_assignments if t.table_id == "trailgear.Shipment")
        assert table.source_of_truth == "aurora_postgresql"
        assert table.write_engines == ["aurora_postgresql"]
        assert table.replica_engines == ["dynamodb"]

    def test_an_engine_that_cannot_own_a_write_never_takes_the_group(self, monkeypatch) -> None:
        """OpenSearch is not a system of record: it owns no write (the ownership
        gate, Decision 2 step 3), however well it scores."""
        scores = {(q, "opensearch"): 100.0 for q in ("ins", "upd", "read")}
        result, _ = _resolve(monkeypatch, SHIPMENT[:2], scores)
        assert {qa.assigned_engine for qa in result.query_assignments} != {"opensearch"}
        assert len({qa.assigned_engine for qa in result.query_assignments}) == 1

    def test_a_gate_on_one_write_rules_the_engine_out_for_the_group(self, monkeypatch) -> None:
        scores = {
            ("ins", "dynamodb"): 99.0,
            ("upd", "dynamodb"): None,
            ("ins", "aurora_postgresql"): 20.0,
            ("upd", "aurora_postgresql"): 20.0,
        }
        result, _ = _resolve(monkeypatch, SHIPMENT[:2], scores)
        assert {qa.assigned_engine for qa in result.query_assignments} == {"aurora_postgresql"}

    def test_an_exact_tie_goes_through_the_tie_break(self, monkeypatch) -> None:
        scores = {
            ("ins", "dynamodb"): 90.0,
            ("ins", "aurora_postgresql"): 70.0,
            ("upd", "dynamodb"): 70.0,
            ("upd", "aurora_postgresql"): 90.0,
        }
        result, trace = _resolve(monkeypatch, SHIPMENT[:2], scores)
        qas = {qa.query_id: qa for qa in result.query_assignments}
        assert qas["ins"].assigned_engine == qas["upd"].assigned_engine
        # No source-compatible engine (SQL Server): an unresolved tie, for the customer.
        assert (
            qas["ins"].tied_engines
            == qas["upd"].tied_engines
            == [
                "aurora_postgresql",
                "dynamodb",
            ]
        )
        assert {m.pass_ for m in result.moves if m.query_id in qas} == {MovePass.TIE}
        assert trace.tie_break_by_query["ins"] == "tie_unresolved"

    def test_a_join_groups_read_moves_with_the_write_group_it_shares_a_write_with(
        self, monkeypatch
    ) -> None:
        """A JOIN co-dependency group (``j1`` reads orders and items, ``ins`` is in
        it) shares the write ``ins`` with orders' write group, so the two place as
        one unit (Amendment 9): the read ``j1`` goes with them, on the unit's mean,
        though it scores best on DynamoDB alone. Its Move and reason say it moved
        with its co-dependency group, not as a write."""
        join = {
            "has_joins": True,
            "join_count": 2,
            "tables_accessed": ["trailgear.Shipment", "trailgear.Item"],
            "filter_tables": ["trailgear.Shipment"],
        }
        queries = [
            {**SHIPMENT[0], **join, "query_text": SHIPMENT[0]["query_text"]},
            SHIPMENT[1],
            {
                **_q(
                    "j1",
                    "SELECT * FROM [Shipment] s JOIN [Item] i ON i.ShipmentId = s.ShipmentId "
                    "JOIN [Item] i2 ON i2.Id = i.Id WHERE s.ShipmentId = @id",
                    [],
                ),
                **join,
            },
        ]
        scores = {
            ("ins", "dynamodb"): 50.0,
            ("ins", "aurora_postgresql"): 80.0,
            ("upd", "dynamodb"): 50.0,
            ("upd", "aurora_postgresql"): 80.0,
            ("j1", "dynamodb"): 90.0,
            ("j1", "aurora_postgresql"): 70.0,
        }
        result, _ = _resolve(monkeypatch, queries, scores)
        engines = {qa.query_id: qa.assigned_engine for qa in result.query_assignments}
        assert engines == {
            "ins": "aurora_postgresql",
            "upd": "aurora_postgresql",
            "j1": "aurora_postgresql",
        }
        moves = {m.query_id: m for m in result.moves}
        assert moves["ins"].reason_code == moves["upd"].reason_code == ReasonCode.WRITE_GROUP
        assert moves["j1"].reason_code == ReasonCode.GROUP_MEAN
        reasons = {qa.query_id: qa.assignment_reason for qa in result.query_assignments}
        assert reasons["j1"].startswith("co-dependency group → Aurora PostgreSQL")
        assert reasons["ins"].startswith("write group: one source of truth → Aurora PostgreSQL")

    def test_a_table_with_one_write_is_still_scored_per_query(self, monkeypatch) -> None:
        scores = {("ins", "dynamodb"): 90.0, ("ins", "aurora_postgresql"): 60.0}
        result, _ = _resolve(monkeypatch, SHIPMENT[:1], scores)
        assert result.query_assignments[0].assigned_engine == "dynamodb"
        assert result.moves[0].reason_code == ReasonCode.HIGHEST_SCORE


# ---------------------------------------------------------------------------
# The customer gate: a pin on one write pulls the table's whole write group
# ---------------------------------------------------------------------------


class _MemStore:
    def __init__(self) -> None:
        self.objects: dict[str, dict] = {}

    def read_json(self, path: str) -> dict:
        return self.objects[path]

    def write_json(self, path: str, data: dict) -> None:
        self.objects[path] = data

    def exists(self, path: str) -> bool:
        return path in self.objects

    def list_prefix(self, prefix: str) -> list[str]:
        return [k for k in self.objects if k.startswith(prefix)]


DB, JOB = "db", "job-1"
GATE_QUERIES = [
    _q("w1", "INSERT INTO orders (id) VALUES ($1)", ["db.orders"]),
    _q("w2", "UPDATE orders SET status = $1 WHERE id = $2", ["db.orders"]),
    _q("r1", "SELECT * FROM orders WHERE id = $1", ["db.orders"]),
    _q("x1", "UPDATE other SET a = $1 WHERE id = $2", ["db.other"]),
]


def _seed_gate(store: _MemStore, engines: list[str], extra: list[dict] | None = None) -> None:
    queries = GATE_QUERIES + (extra or [])
    assignment = Assignment(
        job_id=JOB,
        version=1,
        status=AssignmentStatus.AUTO_GENERATED,
        source=AssignmentSource.ASSIGNMENT_RESOLUTION,
        timestamp=datetime.now(UTC),
        query_assignments=[
            QueryAssignment(
                query_id=q["query_id"],
                assigned_engine="dynamodb",
                confidence=80,
                source_tables=q["tables_accessed"],
                assignment_reason="test",
            )
            for q in queries
        ],
        table_assignments=[],
        co_dependency_groups=[],
        validation_warnings=[],
    )
    store.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json", assignment.model_dump(mode="json")
    )
    store.write_json(
        f"{DB}/{JOB}/collector/output.json",
        {
            "metadata": {"source_database": {"engine": "postgresql"}},
            "queries": {"query_patterns": queries},
            "database_schema": {"tables": []},
        },
    )
    for engine in engines:
        store.write_json(f"{DB}/{JOB}/analysis-{engine}/analysis.json", {"workload_analysis": {}})


class TestTheCustomerGate:
    def test_a_pin_on_one_write_pulls_the_tables_other_writes(self) -> None:
        store = _MemStore()
        _seed_gate(store, ["dynamodb", "aurora_postgresql"])
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("w1", assigned_engine="aurora_postgresql")]
        )
        by_id = {qa.query_id: qa for qa in result.assignment.query_assignments}
        assert by_id["w1"].assigned_engine == by_id["w2"].assigned_engine == "aurora_postgresql"
        assert by_id["w2"].co_dependency_propagated is True
        assert "a table's writes stay on one engine" in by_id["w2"].assignment_reason
        # The table's read and another table's write stay where they are.
        assert by_id["r1"].assigned_engine == by_id["x1"].assigned_engine == "dynamodb"
        assert result.propagated_query_ids == ["w2"]
        move = next(m for m in result.assignment.moves if m.query_id == "w2")
        assert move.reason_code == ReasonCode.WRITE_GROUP_CUSTOMER_PROPAGATION
        assert move.pass_ == MovePass.CUSTOMER_GROUP_PROPAGATION
        assert "a customer re-route moved its table's writes" in render_move_reason(move)
        table = next(t for t in result.assignment.table_assignments if t.table_id == "db.orders")
        assert table.source_of_truth == "aurora_postgresql"
        assert table.replica_engines == ["dynamodb"]

    def test_a_pin_on_a_read_in_a_join_group_moves_its_merged_unit(self) -> None:
        """j1 (a read) and j2 (a write of orders) share a significant JOIN, so their
        JOIN group and orders' write group are one unit (Amendment 9). A pick of j1
        moves the whole unit: j2, and with it orders' other writes w1 and w2. Each
        says why: the writes moved with orders' writes, not a JOIN they do not share."""
        join = {"has_joins": True, "join_count": 2, "filter_tables": ["db.orders", "db.items"]}
        extra = [
            {
                **_q(
                    "j1",
                    "SELECT * FROM orders a JOIN items b ON b.order_id = a.id WHERE a.id = $1",
                    ["db.orders", "db.items"],
                ),
                **join,
            },
            {
                **_q(
                    "j2",
                    "UPDATE orders SET status = $1 FROM items b WHERE b.order_id = orders.id "
                    "AND b.id = $2",
                    ["db.orders", "db.items"],
                ),
                **join,
            },
        ]
        store = _MemStore()
        _seed_gate(store, ["dynamodb", "aurora_postgresql"], extra)
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("j1", assigned_engine="aurora_postgresql")]
        )
        by_id = {qa.query_id: qa for qa in result.assignment.query_assignments}
        # The edit response names every query the pick pulled.
        assert result.propagated_query_ids == ["j2", "w1", "w2"]
        for qid in ("j1", "j2", "w1", "w2"):
            assert by_id[qid].assigned_engine == "aurora_postgresql", qid
        # A read in no JOIN group, and another table's write, stay.
        assert by_id["r1"].assigned_engine == by_id["x1"].assigned_engine == "dynamodb"
        moves = {m.query_id: m for m in result.assignment.moves}
        for qid in ("j2", "w1", "w2"):
            assert moves[qid].reason_code == ReasonCode.WRITE_GROUP_CUSTOMER_PROPAGATION, qid
            assert by_id[qid].assignment_reason == (
                "Moved with the other writes of table db.orders when the customer re-routed j1: "
                "a table's writes stay on one engine, its source of truth."
            )

    def test_a_pin_on_a_read_in_no_join_group_moves_only_the_read(self) -> None:
        store = _MemStore()
        _seed_gate(store, ["dynamodb", "aurora_postgresql"])
        result = apply_assignment_overrides(
            store, DB, JOB, [QueryOverrideInput("r1", assigned_engine="aurora_postgresql")]
        )
        by_id = {qa.query_id: qa.assigned_engine for qa in result.assignment.query_assignments}
        assert by_id == {
            "w1": "dynamodb",
            "w2": "dynamodb",
            "r1": "aurora_postgresql",
            "x1": "dynamodb",
        }
        assert result.propagated_query_ids == []

    def test_two_writes_of_one_table_on_two_engines_are_rejected(self) -> None:
        store = _MemStore()
        _seed_gate(store, ["dynamodb", "aurora_postgresql", "documentdb"])
        with pytest.raises(ExcludedEngineOverride) as err:
            apply_assignment_overrides(
                store,
                DB,
                JOB,
                [
                    QueryOverrideInput("w1", assigned_engine="aurora_postgresql"),
                    QueryOverrideInput("w2", assigned_engine="documentdb"),
                ],
            )
        message = str(err.value)
        assert "both write table db.orders" in message
        assert "Pick one engine for both" in message
        assert [e.gate_kind for e in err.value.excluded] == ["write_group_split"]
        # Nothing was written.
        assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")

    def test_a_pin_another_write_cannot_follow_is_rejected_with_the_reason(self) -> None:
        """OpenSearch may own no write: a pin of w1 onto it is rejected for w1
        itself, and for w2, which would have to follow it."""
        store = _MemStore()
        _seed_gate(store, ["dynamodb", "opensearch"])
        with pytest.raises(ExcludedEngineOverride) as err:
            apply_assignment_overrides(
                store, DB, JOB, [QueryOverrideInput("w1", assigned_engine="opensearch")]
            )
        blocked = {e.query_id: e.message for e in err.value.excluded}
        assert set(blocked) == {"w1", "w2"}
        assert "also writes table db.orders" in blocked["w2"]
        assert "its source of truth" in blocked["w2"]


# ---------------------------------------------------------------------------
# The post-schema reroute and a model's flag move the write group as one
# ---------------------------------------------------------------------------


def test_the_post_schema_router_moves_a_flagged_write_with_its_write_group() -> None:
    """A design review flags one write of orders on DynamoDB: the table's other
    write moves with it, the read and the other table's write stay."""
    from src.agents.referee.post_schema_router import route_unsupported_queries

    assignment = {"co_dependency_groups": [], "write_groups": {"db.orders": ["w1", "w2"]}}
    output = route_unsupported_queries(
        schema_outputs={"dynamodb": {"unsupported_patterns": []}},
        active_engines=["dynamodb", "aurora_postgresql"],
        routing_flags_by_engine={
            "dynamodb": [
                {
                    "query_ids": ["w1"],
                    "flagged_engine": "dynamodb",
                    "reason_category": "missing_feature",
                    "explanation": "needs a server-side join",
                }
            ]
        },
        query_map={q["query_id"]: q for q in GATE_QUERIES},
        assigned_engines={q["query_id"]: "dynamodb" for q in GATE_QUERIES},
        co_dependency_groups=assignment_placement_groups(assignment),
    )
    assert sorted((r.query_id, r.to_engine) for r in output.routings) == [
        ("w1", "aurora_postgresql"),
        ("w2", "aurora_postgresql"),
    ]


def test_the_sanity_sweep_moves_an_orphan_engines_write_group_as_one() -> None:
    """DocumentDB holds only orders' two writes, an orphan the sweep removes. Alone,
    w1 would go to Aurora MySQL and w2 to DynamoDB; as one write group they go to
    the engine with the best mean over both."""
    from src.agents.referee.consolidation_validator import sanity_sweep
    from tests.unit.test_consolidation_validator import _query_map, _scoring_context

    assignments = [
        {"query_id": "w1", "assigned_engine": "documentdb"},
        {"query_id": "w2", "assigned_engine": "documentdb"},
        {"query_id": "a1", "assigned_engine": "aurora_mysql"},
    ] + [{"query_id": f"y{i}", "assigned_engine": "dynamodb"} for i in range(6)]
    query_map = _query_map("w1", "w2", "a1", *(f"y{i}" for i in range(6)))
    scores = _scoring_context(
        {"w1": {"aurora_mysql": 95, "dynamodb": 50}, "w2": {"aurora_mysql": 40, "dynamodb": 60}}
    )
    units = assignment_placement_groups(
        {"co_dependency_groups": [], "write_groups": {"db.orders": ["w1", "w2"]}}
    )
    updated, _, _ = sanity_sweep(
        assignments, [], scores, query_map, source_engine="mysql", co_dependency_groups=units
    )
    engine = {qa["query_id"]: qa["assigned_engine"] for qa in updated}
    assert engine["w1"] == engine["w2"] == "aurora_mysql"


def test_a_flag_on_a_sibling_of_a_pinned_write_holds_the_whole_write_group() -> None:
    """The customer pinned w1 (orders) to DynamoDB; a design review flags w2, another
    write of orders. Moving w2 alone would split orders' writes, so both stay on the
    pinned engine and the flag is a note on the group (``held_queries``)."""
    from src.agents.referee.post_schema_router import route_unsupported_queries

    write_groups = {"db.orders": ["w1", "w2"]}
    assignment = {"co_dependency_groups": [], "write_groups": write_groups}
    output = route_unsupported_queries(
        schema_outputs={"dynamodb": {"unsupported_patterns": []}},
        active_engines=["dynamodb", "aurora_postgresql"],
        routing_flags_by_engine={
            "dynamodb": [
                {
                    "query_ids": ["w2"],
                    "flagged_engine": "dynamodb",
                    "reason_category": "missing_feature",
                    "explanation": "needs a server-side join",
                }
            ]
        },
        query_map={q["query_id"]: q for q in GATE_QUERIES},
        assigned_engines={q["query_id"]: "dynamodb" for q in GATE_QUERIES},
        customer_pinned={"w1"},
        co_dependency_groups=assignment_placement_groups(assignment),
        write_groups=write_groups,
    )
    assert output.routings == []
    assert output.held_queries == ["w2"]
    assert output.terminal_queries == []


ROUTING_PASS_MODULES = (
    "src/agents/referee/reality_check_handler.py",
    "src/orchestrator/local_orchestrator.py",
)


@pytest.mark.parametrize("module", ROUTING_PASS_MODULES)
def test_every_later_routing_pass_places_the_write_groups_too(module: str) -> None:
    """The reality check's flags and sanity sweep and the post-schema reroute take
    their units from ``assignment_placement_groups`` (co-dependency and write
    groups), never the bare co-dependency groups, which would split a table's
    writes. The first assignment and the reality check's Phases 1-2 place the
    resolver's own units (``placement_groups``); the customer gate builds them
    the same way."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[4] / module).read_text(encoding="utf-8")
    assert "co_dependency_groups=assignment_placement_groups(" in text
    if module.endswith("local_orchestrator.py"):
        # A pinned write holds its table's other writes in the reroute.
        assert 'write_groups=assignment.get("write_groups")' in text
    for line in text.splitlines():
        if line.strip().startswith("co_dependency_groups="):
            assert "assignment_placement_groups(" in line, (module, line)


# ---------------------------------------------------------------------------
# The feasibility review flags replicas and split writes
# ---------------------------------------------------------------------------


def _assignment(engines: dict[str, str], extra: list[dict] | None = None) -> Assignment:
    known = GATE_QUERIES + FEAS + (extra or [])
    return Assignment(
        job_id="j",
        version=2,
        status=AssignmentStatus.CUSTOMER_MODIFIED,
        source=AssignmentSource.CUSTOMER_GATE,
        timestamp=datetime.now(UTC),
        query_assignments=[
            QueryAssignment(
                query_id=qid,
                assigned_engine=engine,
                confidence=80,
                source_tables=next(q["tables_accessed"] for q in known if q["query_id"] == qid),
                assignment_reason="t",
            )
            for qid, engine in engines.items()
        ],
        table_assignments=[],
        co_dependency_groups=[],
        validation_warnings=[],
    )


FEAS = [
    _q(
        "copy",
        "INSERT INTO archive SELECT * FROM orders WHERE id = $1",
        ["db.archive", "db.orders"],
    ),
]


def _collector(queries: list[dict]) -> dict:
    return {
        "metadata": {"source_database": {"engine": "postgresql"}},
        "queries": {"query_patterns": queries},
    }


class TestTheFeasibilityReview:
    def test_writes_on_two_engines_block(self) -> None:
        findings = review_assignment_feasibility(
            _assignment({"w1": "dynamodb", "w2": "aurora_postgresql"}), _collector(GATE_QUERIES)
        )
        split = [f for f in findings if f.kind is FindingKind.WRITE_SPLIT]
        assert len(split) == 1
        assert split[0].severity is FindingSeverity.BLOCKING
        assert split[0].table == "db.orders"
        assert split[0].query_ids == ["w1", "w2"]
        assert split[0].message.startswith(
            "Table db.orders has writes on Aurora PostgreSQL and DynamoDB: a table's writes "
            "must land on one engine"
        )
        assert "(w1, w2)" in split[0].message
        assert findings[0] is split[0]  # blocking first

    def test_a_read_on_another_engine_is_a_replica_with_its_cdc_direction(self) -> None:
        findings = review_assignment_feasibility(
            _assignment({"w1": "dynamodb", "w2": "dynamodb", "r1": "aurora_postgresql"}),
            _collector(GATE_QUERIES),
        )
        assert len(findings) == 1
        f = findings[0]
        assert (f.kind, f.severity, f.table) == (
            FindingKind.READ_WRITE_SPLIT,
            FindingSeverity.ADVISORY,
            "db.orders",
        )
        assert f.message.startswith(
            "Table db.orders has its source of truth on DynamoDB and a replica on Aurora "
            "PostgreSQL, kept in sync by CDC from DynamoDB to Aurora PostgreSQL. 1 read is "
            "served by a replica (r1) and eventually consistent: a read can miss a write "
            "until it replicates. Writes own the data on DynamoDB."
        )
        # Customer prose: display names, no raw ids or list reprs.
        assert "aurora_postgresql" not in f.message and "[" not in f.message

    def test_a_split_join_group_is_named_in_plain_prose(self) -> None:
        """A JOIN unit the customer split reaches the co-dependency findings: they name
        engines by display name and list query ids, never Python reprs."""
        assignment = _assignment({"w1": "dynamodb", "w2": "aurora_postgresql"})
        assignment.co_dependency_groups = [["w1", "w2"]]
        findings = review_assignment_feasibility(assignment, _collector([]))
        (split,) = [f for f in findings if f.kind is FindingKind.CO_DEPENDENCY_SPLIT]
        assert split.message.startswith(
            "Co-dependent queries (w1, w2) share a significant JOIN but are split across "
            "Aurora PostgreSQL and DynamoDB; DynamoDB cannot serve the JOIN"
        )
        assert "[" not in split.message and "dynamodb" not in split.message

    def test_a_table_a_write_only_reads_is_read_not_written(self) -> None:
        """The INSERT ... SELECT reads orders: on its own engine it is a replica
        read of orders, never a second writer of it."""
        findings = review_assignment_feasibility(
            _assignment({"w1": "dynamodb", "w2": "dynamodb", "copy": "aurora_postgresql"}),
            _collector(GATE_QUERIES + FEAS),
        )
        assert [f.kind for f in findings] == [FindingKind.READ_WRITE_SPLIT]
        assert findings[0].table == "db.orders"
        assert "served by a replica (copy)" in findings[0].message

    def test_the_replica_reads_are_named_up_to_five(self) -> None:
        reads = [_q(f"r{i}", "SELECT * FROM orders WHERE id = $1", ["db.orders"]) for i in range(7)]
        engines = {"w1": "dynamodb", "w2": "dynamodb", **{f"r{i}": "documentdb" for i in range(7)}}
        (f,) = review_assignment_feasibility(
            _assignment(engines, reads), _collector(GATE_QUERIES + reads)
        )
        assert "7 reads are served by a replica (r0, r1, r2, r3, r4 and 2 more)" in f.message

    def test_a_mysql_multi_table_update_writes_every_table_its_set_names(self) -> None:
        """``UPDATE a JOIN b ... SET b.y = ?`` writes b: on another engine than b's
        own write, it splits b's writes, and the review blocks it."""
        queries = [
            _q(
                "u1",
                "UPDATE wp_a a JOIN wp_b b ON a.id = b.a_id SET b.y = ? WHERE a.id = ?",
                ["db.wp_a", "db.wp_b"],
            ),
            _q("u2", "UPDATE wp_b SET y = ? WHERE id = ?", ["db.wp_b"]),
        ]
        collector = {
            "metadata": {"source_database": {"engine": "mysql"}},
            "queries": {"query_patterns": queries},
        }
        assert build_write_groups(queries, "mysql") == {"db.wp_b": ["u1", "u2"]}
        findings = review_assignment_feasibility(
            _assignment({"u1": "dynamodb", "u2": "aurora_mysql"}, queries), collector
        )
        split = [f for f in findings if f.kind is FindingKind.WRITE_SPLIT]
        assert [f.table for f in split] == ["db.wp_b"]


# ---------------------------------------------------------------------------
# Table facts: source of truth and replicas, and the waves' owner
# ---------------------------------------------------------------------------


def _qas(engines: dict[str, str]) -> list[QueryAssignment]:
    return _assignment(engines).query_assignments


class TestTheSourceOfTruth:
    def test_the_owner_is_the_writer_not_the_plurality(self) -> None:
        """Three reads on Aurora and two writes on DynamoDB: Aurora is the table's
        plurality engine (a summary, Decision 1), DynamoDB its source of truth."""
        qas = _qas({"w1": "dynamodb", "w2": "dynamodb", "r1": "aurora_postgresql"})
        qas += [
            QueryAssignment(
                query_id=f"r{i}",
                assigned_engine="aurora_postgresql",
                confidence=80,
                source_tables=["db.orders"],
                assignment_reason="t",
            )
            for i in (2, 3)
        ]
        (table,), _ = derive_table_assignments(qas, write_groups={"db.orders": ["w1", "w2"]})
        assert table.primary_engine == "aurora_postgresql"
        assert table.source_of_truth == "dynamodb"
        assert table.replica_engines == ["aurora_postgresql"]
        assert _durable_owner(table.model_dump(), "aurora_postgresql") == "dynamodb"
        # The Aurora wave keeps a copy of it in sync from DynamoDB, one way.
        assert _table_owners_for_served([table.model_dump()], "aurora_postgresql", None) == [
            {"table": "db.orders", "owner": "dynamodb", "sync": "CDC"}
        ]
        assert _table_owners_for_served([table.model_dump()], "dynamodb", None) is None

    def test_a_table_written_on_two_engines_claims_no_one_way_sync(self) -> None:
        """An assignment written before the rule can still split a table's writes:
        no engine is its source of truth, and no writer reads as a copy of the
        other."""
        qas = _qas({"w1": "dynamodb", "w2": "aurora_postgresql"})
        (table,), _ = derive_table_assignments(qas, write_groups={"db.orders": ["w1", "w2"]})
        assert table.source_of_truth is None
        assert table.write_engines == ["aurora_postgresql", "dynamodb"]
        for engine in table.write_engines:
            assert _table_owners_for_served([table.model_dump()], engine, None) is None

    def test_an_old_artifact_has_no_source_of_truth(self) -> None:
        (table,), _ = derive_table_assignments(_qas({"w1": "dynamodb"}))
        assert (table.source_of_truth, table.write_engines, table.replica_engines) == (
            None,
            [],
            [],
        )

    def test_the_report_facts_name_the_replicas_cdc_and_risk(self) -> None:
        assignment = {
            "write_groups": {"db.orders": ["w1", "w2"], "db.other": ["x1"]},
            "query_assignments": [
                qa.model_dump()
                for qa in _qas(
                    {
                        "w1": "dynamodb",
                        "w2": "dynamodb",
                        "r1": "aurora_postgresql",
                        "x1": "dynamodb",
                    }
                )
            ],
        }
        rows = table_sources(assignment)
        assert rows == [
            {
                "table": "db.orders",
                "source_of_truth": "dynamodb",
                "write_engines": ["dynamodb"],
                "write_query_ids": ["w1", "w2"],
                "replicas": [{"engine": "aurora_postgresql", "read_query_ids": ["r1"]}],
            },
            {
                "table": "db.other",
                "source_of_truth": "dynamodb",
                "write_engines": ["dynamodb"],
                "write_query_ids": ["x1"],
                "replicas": [],
            },
        ]
        facts = table_source_facts({"table_sources": rows})
        assert facts == {
            "written_table_count": 2,
            "replicated_table_count": 1,
            "split_write_table_count": 0,
            "replicated_tables": [
                {
                    "table": "db.orders",
                    "source_of_truth": "dynamodb",
                    "replica_engines": ["aurora_postgresql"],
                    "write_query_count": 2,
                    "replica_read_query_ids": ["r1"],
                    "replica_read_count": 1,
                    "risk": (
                        "Eventually consistent: 1 read on Aurora PostgreSQL read a copy kept "
                        "in sync from DynamoDB via CDC, so each sees a write only after CDC "
                        "applies it, behind by the replication lag; a read that must see its "
                        "own write should read from DynamoDB."
                    ),
                }
            ],
            "more_replicated_count": 0,
            "single_engine_tables": ["db.other"],
            "single_engine_table_count": 1,
        }
        assert table_source_facts({}) is None


# ---------------------------------------------------------------------------
# Call-rate weighting (maintainer decision on #992, 2026-10-17)
# ---------------------------------------------------------------------------

USER_SESSION = [
    {
        **_q("ins", "INSERT INTO [UserSession] ([Token]) VALUES (@t)", ["db.UserSession"]),
        "calls_per_second": 50.0,
    },
    {
        **_q("upd", "UPDATE [UserSession] SET [Cart] = @c WHERE [Token] = @t", ["db.UserSession"]),
        "calls_per_second": 50.0,
    },
    {
        **_q(
            "del", "DELETE TOP (5000) FROM [UserSession] WHERE [ExpiresAt] < @n", ["db.UserSession"]
        ),
        "calls_per_second": 0.01,
    },
]
USER_SESSION_SCORES = {
    ("ins", "dynamodb"): 95.0,
    ("upd", "dynamodb"): 96.0,
    ("del", "dynamodb"): 26.0,
    ("ins", "aurora_postgresql"): 80.0,
    ("upd", "aurora_postgresql"): 80.0,
    ("del", "aurora_postgresql"): 80.0,
}


class TestCallRateWeighting:
    def test_a_rare_low_scoring_delete_no_longer_drags_frequent_writes_off(
        self, monkeypatch
    ) -> None:
        """UserSession's shape: the plain mean (DynamoDB 72.3, Aurora PostgreSQL 80)
        would pull the INSERT and UPDATE, 95 and 96 on DynamoDB and 5,000 times as
        frequent, onto Aurora PostgreSQL for a rare DELETE's sake. Weighted by call
        rate, the group goes to DynamoDB."""
        result, trace = _resolve(monkeypatch, USER_SESSION, USER_SESSION_SCORES)
        engines = {qa.query_id: qa.assigned_engine for qa in result.query_assignments}
        assert set(engines.values()) == {"dynamodb"}
        group = trace.by_query["ins"]["group"]
        assert group["scores"]["dynamodb"] > group["scores"]["aurora_postgresql"] == 80.0
        # Relative to the busiest member; the DELETE, below 1% of it, weighs the floor.
        assert group["weights"] == {"del": 0.01, "ins": 1.0, "upd": 1.0}

    def test_equal_calls_reproduce_the_plain_mean(self, monkeypatch) -> None:
        queries = [{**q, "calls_per_second": 3.0} for q in USER_SESSION]
        result, trace = _resolve(monkeypatch, queries, USER_SESSION_SCORES)
        assert trace.by_query["ins"]["group"]["scores"] == {
            "aurora_postgresql": 80.0,
            "dynamodb": round((95 + 96 + 26) / 3, 2),
        }
        assert {qa.assigned_engine for qa in result.query_assignments} == {"aurora_postgresql"}

    def test_equal_real_world_rates_keep_an_exact_tie_a_tie(self, monkeypatch) -> None:
        """The round-4 repro: four writes at discourse's 0.0002199 calls per second.
        Their plain means tie at 75.75; the weighted mean must tie too (weights are
        exactly 1.0, and group scores are compared at ``GROUP_SCORE_PLACES``), so the
        tie goes through the tie-break instead of float noise and member order."""
        rate = 0.0002199074074074074
        queries = [
            {
                **_q(f"w{i}", f"UPDATE t SET a = @a WHERE id = @id{i}", ["db.t"]),
                "calls_per_second": rate,
            }
            for i in range(4)
        ]
        pg, ddb = [68, 91, 94, 50], [94, 50, 91, 68]
        scores = {(f"w{i}", "aurora_postgresql"): float(pg[i]) for i in range(4)}
        scores.update({(f"w{i}", "dynamodb"): float(ddb[i]) for i in range(4)})
        result, trace = _resolve(monkeypatch, queries, scores)
        group = trace.by_query["w0"]["group"]
        assert group["scores"] == {"aurora_postgresql": 75.75, "dynamodb": 75.75}
        # SQL Server has no source-compatible engine: an unresolved tie, the customer's.
        assert trace.tie_break_by_query["w0"] == "tie_unresolved"
        assert {m.pass_ for m in result.moves} == {MovePass.TIE}
        assert result.query_assignments[0].tied_engines == ["aurora_postgresql", "dynamodb"]

    def test_a_group_with_no_call_statistics_takes_the_plain_mean(self, monkeypatch) -> None:
        queries = [{**q, "calls_per_second": 0} for q in USER_SESSION]
        result, trace = _resolve(monkeypatch, queries, USER_SESSION_SCORES)
        assert "weights" not in trace.by_query["ins"]["group"]
        assert {qa.assigned_engine for qa in result.query_assignments} == {"aurora_postgresql"}

    def test_a_write_with_no_calls_keeps_a_floor_weight(self) -> None:
        from src.agents.referee.write_groups import CALL_WEIGHT_FLOOR_SHARE, unit_call_weights

        by_id: dict[str, dict] = {
            "a": {"calls_per_second": 200.0},
            "b": {"calls_per_second": 0},
            "c": {},
        }
        weights = unit_call_weights([["a", "b", "c"]], {"db.t": ["a", "b", "c"]}, by_id)
        assert weights == {"a": 1.0, "b": CALL_WEIGHT_FLOOR_SHARE, "c": CALL_WEIGHT_FLOOR_SHARE}
        # Any member below the floor share of the busiest, not only one with no calls.
        by_id["b"] = {"calls_per_second": 1.0}
        assert unit_call_weights([["a", "b"]], {"db.t": ["a", "b"]}, by_id)["b"] == (
            CALL_WEIGHT_FLOOR_SHARE
        )
        # A unit with no write group is never weighted: Amendment 4's plain mean.
        assert unit_call_weights([["a", "b"]], {}, by_id) == {}

    def test_the_reality_check_judges_the_unit_on_the_same_weights(self) -> None:
        from src.agents.referee.reality_check_rules import _moving_group_scores

        group = {"key": "k", "scores": {}, "weights": {"a": 9.0, "b": 1.0}}
        trace = {
            "a": {"engines": {"dynamodb": 90, "aurora_postgresql": 50}, "group": group},
            "b": {"engines": {"dynamodb": 10, "aurora_postgresql": 50}, "group": group},
        }
        scores = _moving_group_scores(["a", "b"], ["dynamodb", "aurora_postgresql"], trace)
        assert scores == {"dynamodb": 82.0, "aurora_postgresql": 50.0}


# ---------------------------------------------------------------------------
# A saved job that split a table's writes is re-routed when loaded (#992)
# ---------------------------------------------------------------------------


def _seed_legacy_split(store: _MemStore) -> None:
    """A version-1.15 assignment file (no write_groups) with orders' two writes on
    two engines, as jobs saved before the rule could hold, and stale designs."""
    _seed_gate(store, ["dynamodb", "aurora_postgresql"])
    path = f"{DB}/{JOB}/assignment/v1/assignment.json"
    legacy = store.read_json(path)
    for key in ("write_groups", "contract_version"):
        legacy.pop(key, None)
    for qa in legacy["query_assignments"]:
        if qa["query_id"] == "w2":
            qa["assigned_engine"] = "aurora_postgresql"
    store.write_json(path, legacy)
    for engine in ("dynamodb", "aurora_postgresql"):
        store.write_json(f"{DB}/{JOB}/schema-{engine}/v1/schema_output.json", {})


class TestASavedSplitIsReRouted:
    def test_it_loads_and_gets_a_new_version_with_one_writer(self) -> None:
        from src.agents.referee.assignment_overrides import reroute_split_writes
        from src.storage.assignment_versioning import (
            resolve_effective_assignment_version,
            stale_schema_versions,
        )

        store = _MemStore()
        _seed_legacy_split(store)
        rerouted = reroute_split_writes(store, DB, JOB)
        assert rerouted is not None
        assert rerouted.split_tables == ["db.orders"]
        new = rerouted.assignment
        assert (new.version, new.previous_version) == (2, 1)
        assert new.source == AssignmentSource.SOURCE_OF_TRUTH_REROUTE
        assert store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")
        # v1 is never overwritten.
        v1 = store.read_json(f"{DB}/{JOB}/assignment/v1/assignment.json")
        assert {qa["query_id"]: qa["assigned_engine"] for qa in v1["query_assignments"]}[
            "w2"
        ] == "aurora_postgresql"
        by_id = {qa.query_id: qa for qa in new.query_assignments}
        assert by_id["w1"].assigned_engine == by_id["w2"].assigned_engine
        (moved,) = rerouted.moved_query_ids
        assert by_id[moved].assignment_reason.startswith("Re-routed to one source of truth on ")
        (move,) = [m for m in new.moves if m.query_id == moved]
        assert move.reason_code == ReasonCode.WRITE_GROUP_REROUTE
        assert "re-routed to one source of truth" in render_move_reason(move)
        table = next(t for t in new.table_assignments if t.table_id == "db.orders")
        assert table.source_of_truth == by_id["w1"].assigned_engine
        # The changed engines are designed again: their designs are stale now.
        assert rerouted.affected_engines == ["aurora_postgresql", "dynamodb"]
        assert set(stale_schema_versions(store, DB, JOB)) >= set(rerouted.affected_engines)
        assert resolve_effective_assignment_version(store, DB, JOB) == 2
        # The approval gate no longer finds a split: nothing to block on.
        collector = store.read_json(f"{DB}/{JOB}/collector/output.json")
        assert not [
            f
            for f in review_assignment_feasibility(new, collector)
            if f.kind is FindingKind.WRITE_SPLIT
        ]

    def test_an_assignment_with_one_writer_per_table_is_left_alone(self) -> None:
        from src.agents.referee.assignment_overrides import reroute_split_writes

        store = _MemStore()
        _seed_gate(store, ["dynamodb", "aurora_postgresql"])
        assert reroute_split_writes(store, DB, JOB) is None
        assert not store.exists(f"{DB}/{JOB}/assignment/v2/assignment.json")

    def test_a_customer_pin_decides_the_engine(self) -> None:
        from src.agents.referee.assignment_overrides import reroute_split_writes

        store = _MemStore()
        _seed_legacy_split(store)
        path = f"{DB}/{JOB}/assignment/v1/assignment.json"
        legacy = store.read_json(path)
        for qa in legacy["query_assignments"]:
            if qa["query_id"] == "w2":
                qa["customer_override"] = True
        store.write_json(path, legacy)
        rerouted = reroute_split_writes(store, DB, JOB)
        assert rerouted is not None
        engines = {qa.query_id: qa.assigned_engine for qa in rerouted.assignment.query_assignments}
        assert engines["w1"] == engines["w2"] == "aurora_postgresql"


@pytest.mark.parametrize(
    "module",
    [
        "src/atx_orchestrator/tools.py",
        "src/api/routes/assignments.py",
        "src/orchestrator/local_orchestrator.py",
        "scripts/run_schema_design.py",
    ],
)
def test_every_approval_and_resume_path_reroutes_a_saved_split(module: str) -> None:
    """The ATX approval tool, the API's edit/approval route, the orchestrator's
    schema-design resume and the schema-design script all re-route a saved split
    first (``reroute_split_writes``), so no path approves or designs one."""
    from pathlib import Path

    text = (Path(__file__).resolve().parents[4] / module).read_text(encoding="utf-8")
    assert "reroute_split_writes(" in text


# ---------------------------------------------------------------------------
# Round 4: the review shows the re-routed version; pins; after approval; races
# ---------------------------------------------------------------------------


def _local_legacy_split(tmp_path):
    """``_seed_legacy_split`` on a real local store (the API and the race tests)."""
    from src.storage.local_store import LocalArtifactStore

    mem = _MemStore()
    _seed_legacy_split(mem)
    store = LocalArtifactStore(base_dir=str(tmp_path))
    for key, value in mem.objects.items():
        store.write_json(key, value)
    return store


class TestTheReviewShowsTheReRoutedVersion:
    def test_an_unchanged_submission_of_the_shown_table_changes_nothing(self, tmp_path) -> None:
        """The round-4 repro: the customer was shown the split v1 and submits it
        unchanged. The gate re-routes (v2) and approves; no customer override, no
        v3, nothing moved back onto the old engine."""
        from unittest.mock import patch

        from src.agents.referee.assignment_review import render_assignment_review
        from src.atx_orchestrator import tools
        from src.atx_orchestrator.runtime.store import upgrade_store

        store = upgrade_store(_local_legacy_split(tmp_path))
        v1 = Assignment.model_validate(store.read_json(f"{DB}/{JOB}/assignment/v1/assignment.json"))
        shown = render_assignment_review(v1)
        with patch("src.atx_orchestrator.tools._make_store", return_value=store):
            out = __import__("json").loads(tools.finalize_assignment_review(JOB, DB, shown))
        assert out["status"] == "approved", out
        assert out["changed"] is False and out["applied_overrides"] == 0
        assert out["assignment_version"] == 2
        assert not store.exists(f"{DB}/{JOB}/assignment/v3/assignment.json")
        v2 = store.read_json(f"{DB}/{JOB}/assignment/v2/assignment.json")
        assert not any(qa.get("customer_override") for qa in v2["query_assignments"])
        engines = {qa["query_id"]: qa["assigned_engine"] for qa in v2["query_assignments"]}
        assert engines["w1"] == engines["w2"]

    def test_presenting_the_review_reroutes_first(self, tmp_path) -> None:
        from unittest.mock import patch

        from src.atx_orchestrator import tools
        from src.atx_orchestrator.runtime.store import upgrade_store

        store = upgrade_store(_local_legacy_split(tmp_path))
        with (
            patch("src.atx_orchestrator.tools._make_store", return_value=store),
            patch("src.atx_orchestrator.tools.mark_step_pending_human_input"),
        ):
            out = __import__("json").loads(tools.present_assignment_review(JOB, DB))
        assert out["assignment_version"] == 2

    def test_the_api_shows_the_rerouted_version_and_refuses_a_stale_edit(self, tmp_path) -> None:
        from fastapi.testclient import TestClient

        from src.api.main import app
        from src.api.routes import assignments

        assignments.artifact_store = _local_legacy_split(tmp_path)
        try:
            client = TestClient(app)
            url = f"/api/v1/assessments/{JOB}/assignments?database_name={DB}"
            shown = client.get(url).json()["assignment"]
            assert shown["version"] == 2
            assert shown["source"] == "source_of_truth_reroute"
            stale = client.put(
                url,
                json={
                    "overrides": [{"query_id": "r1", "assigned_engine": "aurora_postgresql"}],
                    "base_version": 1,
                },
            )
            assert stale.status_code == 409
            assert "Reload the assignment review" in stale.json()["detail"]
            fresh = client.put(
                url,
                json={
                    "overrides": [{"query_id": "r1", "assigned_engine": "aurora_postgresql"}],
                    "base_version": 2,
                },
            )
            assert fresh.status_code == 200, fresh.json()
            assert fresh.json()["assignment"]["version"] == 3
        finally:
            assignments.artifact_store = None


class TestReRouteEdges:
    def test_conflicting_legacy_pins_clear_the_overridden_pin_and_say_so(self) -> None:
        from src.agents.referee.assignment_overrides import reroute_split_writes

        store = _MemStore()
        _seed_legacy_split(store)
        path = f"{DB}/{JOB}/assignment/v1/assignment.json"
        legacy = store.read_json(path)
        for qa in legacy["query_assignments"]:
            if qa["query_id"] in ("w1", "w2"):
                qa["customer_override"] = True
        store.write_json(path, legacy)
        rerouted = reroute_split_writes(store, DB, JOB)
        assert rerouted is not None
        (moved,) = rerouted.moved_query_ids
        by_id = {qa.query_id: qa for qa in rerouted.assignment.query_assignments}
        assert by_id[moved].customer_override is False
        kept = "w1" if moved == "w2" else "w2"
        assert by_id[kept].customer_override is True
        (conflict,) = rerouted.pin_conflicts
        assert f"Query {moved} was pinned to" in conflict and "no longer pinned" in conflict
        assert conflict in rerouted.assignment.validation_warnings

    def test_after_approval_the_moves_are_disclosed_like_a_post_schema_reroute(self) -> None:
        from src.agents.referee.assignment_overrides import reroute_split_writes
        from src.contracts.assignment_models import AssignmentStatus

        store = _MemStore()
        _seed_legacy_split(store)
        rerouted = reroute_split_writes(store, DB, JOB, after_approval=True)
        assert rerouted is not None
        new = rerouted.assignment
        assert new.status == AssignmentStatus.REROUTED_AFTER_APPROVAL
        (row,) = new.post_schema_reroutes
        assert row.query_id == rerouted.moved_query_ids[0]
        assert (row.reason_category, row.approved_version) == ("source_of_truth", 1)

    def test_two_concurrent_reroutes_write_one_version(self, tmp_path) -> None:
        """Both read v1 before either writes: the second finds v2 already written,
        returns the first's version, and never rewrites it."""
        from unittest.mock import patch

        from src.agents.referee import assignment_overrides

        store = _local_legacy_split(tmp_path)
        with patch.object(
            assignment_overrides, "resolve_effective_assignment_version", return_value=1
        ):
            first = assignment_overrides.reroute_split_writes(store, DB, JOB)
            written = (tmp_path / DB / JOB / "assignment" / "v2" / "assignment.json").read_bytes()
            second = assignment_overrides.reroute_split_writes(store, DB, JOB)
        assert first is not None and second is not None
        assert second.assignment.version == first.assignment.version == 2
        assert (
            tmp_path / DB / JOB / "assignment" / "v2" / "assignment.json"
        ).read_bytes() == written
        assert not (tmp_path / DB / JOB / "assignment" / "v3").exists()

    def test_threads_racing_the_reroute_write_one_version(self, tmp_path) -> None:
        from concurrent.futures import ThreadPoolExecutor

        from src.agents.referee.assignment_overrides import reroute_split_writes

        store = _local_legacy_split(tmp_path)
        with ThreadPoolExecutor(max_workers=6) as pool:
            list(pool.map(lambda _: reroute_split_writes(store, DB, JOB), range(6)))
        versions = sorted(p.name for p in (tmp_path / DB / JOB / "assignment").iterdir())
        assert [v for v in versions if v.startswith("v")] == ["v1", "v2"]

    def test_the_schema_design_script_reroutes_only_a_design_run(self, tmp_path) -> None:
        from scripts.run_schema_design import _resolve_version

        store = _local_legacy_split(tmp_path)
        assert _resolve_version(store, JOB, DB, None) == 1  # --status, --merge, --finalize
        assert not (tmp_path / DB / JOB / "assignment" / "v2").exists()
        assert _resolve_version(store, JOB, DB, None, reroute=True) == 2
        v2 = store.read_json(f"{DB}/{JOB}/assignment/v2/assignment.json")
        assert v2["status"] == "rerouted_after_approval"
