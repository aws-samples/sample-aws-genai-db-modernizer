"""ADR-031 Decision 3: DynamoDB supports ACID transactions; the score reflects shape (#493).

A transaction is never a capability DynamoDB lacks: it is native for the
``acid_transactions`` capability and for the hard gate's ``multi_doc_acid``. What
can cost DynamoDB points is a write too wide for one ``TransactWriteItems`` call
(more than 100 items, or a set the statement does not bound), and only that
write: the ``wide-transaction`` anti-pattern, decided by the engine-neutral
``transaction_shape`` fact, recorded in DynamoDB's decision trace.
"""

from __future__ import annotations

import pytest

from src.agents.referee.capability_registry import can_engine_serve_capability
from src.agents.referee.query_scoring import (
    CAPABILITY_REDESIGN_PENALTIES,
    build_anti_pattern_map,
)
from src.agents.referee.transaction_shape import (
    BOUNDED,
    SINGLE_ITEM,
    TRANSACTION_MAX_ITEMS,
    UNBOUNDED,
    transaction_shape,
)
from src.shared.catalog.patterns import pattern
from src.shared.engine_capabilities import ACID_TRANSACTION_ENGINES
from src.tools.analysis.dynamodb_analysis_tools import (
    analyze_dynamodb_patterns,
    analyze_dynamodb_use_cases,
    build_decision_trace,
)


def _write(text: str, query_type: str, qid: str = "w1", **facts: object) -> dict:
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": query_type,
        "tables_accessed": ["app.orders"],
        "calls_per_second": 1.0,
        **facts,
    }


def _collector(*queries: dict) -> dict:
    table = {
        "table_id": "app.orders",
        "table_name": "orders",
        "row_count": 10_000,
        "size_mb": 10.0,
        "primary_key": ["id"],
        "columns": [
            {"column_name": "id", "data_type": "bigint"},
            {"column_name": "status", "data_type": "text"},
            {"column_name": "created_at", "data_type": "timestamp"},
        ],
    }
    return {"database_schema": {"tables": [table]}, "queries": {"query_patterns": list(queries)}}


def _shape(query: dict) -> str:
    shape = transaction_shape(query)
    assert shape is not None
    return shape.shape


IN_150 = ", ".join(f"${i}" for i in range(1, 151))


class TestATransactionIsNotAMissingCapability:
    def test_dynamodb_is_native_for_acid_transactions(self) -> None:
        assert "dynamodb" in ACID_TRANSACTION_ENGINES
        assert pattern("acid-transactions").engines["dynamodb"].kind == "native"

    def test_the_hard_gate_never_excludes_dynamodb_for_a_transaction(self) -> None:
        assert can_engine_serve_capability("dynamodb", ["multi_doc_acid"])

    def test_no_flat_transaction_redesign_penalty_remains(self) -> None:
        assert "acid-transactions" not in CAPABILITY_REDESIGN_PENALTIES

    def test_another_engines_transaction_fit_costs_dynamodb_nothing(self) -> None:
        # Another engine reports acid-transactions as a fit: before #493 every engine
        # outside the capability took the reduced redesign penalty, DynamoDB's 10.
        analysis: dict[str, dict] = {
            "aurora_postgresql": {
                "workload_analysis": {
                    "patterns_detected": [
                        {"pattern_type": "acid-transactions", "query_ids": ["w1"]}
                    ]
                }
            },
            "dynamodb": {"workload_analysis": {"patterns_detected": []}},
        }
        penalties, _ = build_anti_pattern_map(analysis)
        assert ("w1", "dynamodb") not in penalties


@pytest.mark.parametrize(
    ("text", "query_type", "stats", "shape"),
    [
        ("UPDATE orders SET status = $1 WHERE id = $2", "UPDATE", {}, SINGLE_ITEM),
        ("INSERT INTO orders (id, status) VALUES ($1, $2)", "INSERT", {}, SINGLE_ITEM),
        ("INSERT INTO orders (id) VALUES ($1), ($2), ($3)", "INSERT", {}, BOUNDED),
        ("DELETE FROM orders WHERE id IN ($1, $2, $3)", "DELETE", {}, BOUNDED),
        (f"DELETE FROM orders WHERE id IN ({IN_150})", "DELETE", {}, UNBOUNDED),
        ("DELETE FROM orders WHERE created_at < $1", "DELETE", {}, UNBOUNDED),
        ("INSERT INTO orders (id) SELECT id FROM carts WHERE done", "INSERT", {}, UNBOUNDED),
        (
            "UPDATE orders o SET status = $1 FROM carts c WHERE c.order_id = o.id",
            "UPDATE",
            {"tables_accessed": ["app.orders", "app.carts"], "has_joins": True},
            UNBOUNDED,
        ),
        (
            "UPDATE orders SET status = $1 WHERE status = $2",
            "UPDATE",
            {"rows_returned_avg": 40},
            BOUNDED,
        ),
        # Conditional writes are ACID on one item (ADR-031 Decision 3, review of #910).
        (
            "INSERT INTO orders (id, status) SELECT $1, $2 "
            "WHERE NOT EXISTS (SELECT 1 FROM orders WHERE id = $1)",
            "INSERT",
            {},
            SINGLE_ITEM,
        ),
        (
            "UPDATE orders SET status = $1 WHERE id = $2 "
            "AND NOT EXISTS (SELECT 1 FROM orders WHERE status = $3)",
            "UPDATE",
            {},
            SINGLE_ITEM,
        ),
        (
            "UPDATE orders SET status = $1 WHERE id = $2 "
            "AND EXISTS (SELECT 1 FROM carts WHERE carts.order_id = $2)",
            "UPDATE",
            {"tables_accessed": ["app.orders", "app.carts"]},
            SINGLE_ITEM,
        ),
        (
            "MERGE INTO [orders] AS t USING (VALUES (@Id, @Status)) AS s (id, status) "
            "ON t.id = s.id WHEN MATCHED THEN UPDATE SET status = s.status "
            "WHEN NOT MATCHED THEN INSERT (id, status) VALUES (s.id, s.status);",
            "MERGE",
            {},
            SINGLE_ITEM,
        ),
        (
            "INSERT INTO orders (id, price) VALUES ($1, (SELECT price FROM carts WHERE id = $2))",
            "INSERT",
            {},
            SINGLE_ITEM,
        ),
        (
            "INSERT INTO orders (id, status) SELECT id, $1 FROM orders WHERE id = $2",
            "INSERT",
            {},
            SINGLE_ITEM,
        ),
        # Still unbounded: the condition guards nothing keyed, or the source is a set.
        (
            "UPDATE orders SET status = $1 WHERE NOT EXISTS (SELECT 1 FROM carts WHERE id = $2)",
            "UPDATE",
            {},
            UNBOUNDED,
        ),
        (
            "MERGE INTO orders AS t USING carts AS s ON t.id = s.order_id "
            "WHEN MATCHED THEN UPDATE SET status = s.status;",
            "MERGE",
            {},
            UNBOUNDED,
        ),
        ("INSERT INTO orders (id) SELECT unnest($1)", "INSERT", {}, UNBOUNDED),
        # A key array has as many items as the caller binds (review of #910).
        ("UPDATE orders SET status = $1 WHERE id = ANY($2)", "UPDATE", {}, UNBOUNDED),
        ('DELETE FROM "orders" WHERE "orders"."id" = ANY ($1)', "DELETE", {}, UNBOUNDED),
        (
            "UPDATE orders SET status = $1 WHERE status = $2",
            "UPDATE",
            {"rows_affected_avg": 324},
            UNBOUNDED,
        ),
    ],
)
def test_the_shape_of_a_write(text: str, query_type: str, stats: dict, shape: str) -> None:
    assert _shape(_write(text, query_type, **stats)) == shape


def test_a_read_has_no_transaction_shape() -> None:
    assert (
        transaction_shape(_write("SELECT * FROM orders WHERE id = $1 FOR UPDATE", "SELECT")) is None
    )


def test_the_limit_is_dynamodbs_transaction_item_quota() -> None:
    assert TRANSACTION_MAX_ITEMS == 100
    assert _shape(_write("x", "UPDATE", rows_affected_avg=100)) == BOUNDED
    assert _shape(_write("x", "UPDATE", rows_affected_avg=101)) == UNBOUNDED


class TestTheScoreReflectsTheShape:
    def _run(self):
        collector = _collector(
            _write("UPDATE orders SET status = $1 WHERE id = $2", "UPDATE", "single"),
            _write("INSERT INTO orders (id) VALUES ($1), ($2)", "INSERT", "bounded"),
            _write("DELETE FROM orders WHERE created_at < $1", "DELETE", "wide"),
        )
        workload = analyze_dynamodb_use_cases(collector)
        return collector, workload

    def test_only_the_write_one_transaction_cannot_hold_is_a_wide_transaction(self) -> None:
        _, workload = self._run()
        wide = [
            ap
            for ap in workload.anti_patterns_detected or []
            if ap.anti_pattern_id == "dynamodb-ap-04"
        ]
        assert [ap.query_ids for ap in wide] == [["wide"]]
        assert wide[0].anti_pattern_type == "wide-transaction"

    def test_the_penalty_lands_on_dynamodb_only_and_only_for_that_write(self) -> None:
        _, workload = self._run()
        analysis = {"dynamodb": {"workload_analysis": workload.model_dump()}}
        penalties, _ = build_anti_pattern_map(analysis)
        assert penalties.get(("wide", "dynamodb")) == pattern("wide-transaction").penalty
        assert ("single", "dynamodb") not in penalties
        assert ("bounded", "dynamodb") not in penalties
        for engine, support in pattern("wide-transaction").engines.items():
            if engine != "dynamodb":
                assert support.kind in ("unscored", "future"), engine

    def test_the_decision_trace_states_the_shape_and_why(self) -> None:
        collector, workload = self._run()
        recs = analyze_dynamodb_patterns(collector, workload)
        trace = build_decision_trace(collector, workload, recs)
        shapes = {m["query_id"]: m.get("transaction_shape") for m in trace["query_matches"]}
        assert shapes["single"]["shape"] == SINGLE_ITEM
        assert shapes["bounded"] == {
            "shape": BOUNDED,
            "items": 2,
            "reason": "inserts 2 row(s) per call",
        }
        assert shapes["wide"]["shape"] == UNBOUNDED
        assert "does not bound them" in shapes["wide"]["reason"]
