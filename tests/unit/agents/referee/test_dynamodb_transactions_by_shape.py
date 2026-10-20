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
    KEY_SCOPED,
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


def test_the_shape_reads_its_facts_in_the_source_engines_dialect(monkeypatch):
    """The single-table check behind a keyed write reads the query facts in the
    collected source engine's dialect (#893), and the DynamoDB analysis passes it."""
    from src.agents.referee import transaction_shape as module
    from src.agents.referee.table_resolution import SourceScope
    from src.tools.analysis.dynamodb_analysis_tools import analyze_dynamodb_use_cases

    seen = []
    real = module.single_table_access_shape

    def recording(query, *args, **kwargs):
        seen.append(kwargs.get("source"))
        return real(query, *args, **kwargs)

    monkeypatch.setattr(module, "single_table_access_shape", recording)
    query = {
        "query_id": "q1",
        "query_type": "DELETE",
        "query_text": "DELETE TOP (5000) FROM [UserSession] WHERE [ExpiresAt] < @now",
        "tables_accessed": ["trailgear.UserSession"],
    }
    module.transaction_shape(query, SourceScope(engine="sqlserver"))
    assert seen[-1] == SourceScope(engine="sqlserver")
    collector = {
        "metadata": {"source_database": {"engine": "sqlserver", "database_name": "trailgear"}},
        "database_schema": {"tables": [{"table_id": "trailgear.UserSession", "columns": []}]},
        "queries": {"query_patterns": [query]},
    }
    analyze_dynamodb_use_cases(collector)
    assert seen[-1] == SourceScope(name="trailgear", engine="sqlserver")


# --- Only a primary or unique key addresses one item (#915) ---------------------------


def _keyed_collector(*queries: dict, engine: str | None = None) -> dict:
    """``app.orders`` keyed by ``id``, a unique ``email``, a unique (tenant_id, code), a
    non-unique index on ``status``, a foreign key ``customer_id`` and an unindexed
    ``flag``; ``app.events`` collected with no primary or unique key, an index on
    ``source`` and an unindexed ``kind``."""
    orders = {
        "table_id": "app.orders",
        "table_name": "orders",
        "row_count": 10_000,
        "primary_key": ["id"],
        "columns": [
            {"column_name": c, "data_type": "text"}
            for c in ("id", "status", "email", "tenant_id", "code", "customer_id", "flag")
        ],
        "foreign_keys": [{"columns": ["customer_id"], "referenced_table": "app.customers"}],
        "indexes": [
            {"index_name": "ux_email", "columns": ["email"], "is_unique": True},
            {"index_name": "ux_code", "columns": ["tenant_id", "code"], "is_unique": True},
            {"index_name": "ix_status", "columns": ["status"], "is_unique": False},
        ],
    }
    events = {
        "table_id": "app.events",
        "table_name": "events",
        "row_count": 10_000,
        "primary_key": [],
        "columns": [
            {"column_name": "kind", "data_type": "text"},
            {"column_name": "source", "data_type": "text"},
        ],
        "indexes": [{"index_name": "ix_source", "columns": ["source"], "is_unique": False}],
    }
    collector: dict = {
        "database_schema": {"tables": [orders, events]},
        "queries": {"query_patterns": list(queries)},
    }
    if engine:
        collector["metadata"] = {"source_database": {"engine": engine, "database_name": "app"}}
    return collector


def _keyed_shape(query: dict, engine: str | None = "postgresql"):
    from src.agents.referee.access_pattern import SchemaKeys
    from src.agents.referee.table_resolution import SourceScope

    collector = _keyed_collector(query, engine=engine)
    shape = transaction_shape(
        query, SourceScope.from_collector(collector), SchemaKeys.from_collector(collector)
    )
    assert shape is not None
    return shape


@pytest.mark.parametrize(
    ("text", "query_type", "stats", "shape"),
    [
        # A full primary or unique key: one item, or as many as the IN list names.
        ("UPDATE orders SET status = $1 WHERE id = $2", "UPDATE", {}, SINGLE_ITEM),
        ("DELETE FROM orders WHERE email = $1", "DELETE", {}, SINGLE_ITEM),
        ("DELETE FROM orders WHERE tenant_id = $1 AND code = $2", "DELETE", {}, SINGLE_ITEM),
        ("DELETE FROM orders WHERE id IN ($1, $2, $3)", "DELETE", {}, BOUNDED),
        ("UPDATE orders SET code = $1 WHERE id = $2 AND status = $3", "UPDATE", {}, SINGLE_ITEM),
        # A key anchor that is not a full key: the item collection one value addresses
        # (maintainer decision on #915): an index, a foreign key, part of a unique key.
        ("UPDATE orders SET status = $1 WHERE status = $2", "UPDATE", {}, KEY_SCOPED),
        ("DELETE FROM orders WHERE status IN ($1, $2)", "DELETE", {}, KEY_SCOPED),
        ("DELETE FROM orders WHERE customer_id = $1", "DELETE", {}, KEY_SCOPED),
        ("DELETE FROM orders WHERE tenant_id = $1", "DELETE", {}, KEY_SCOPED),
        ("DELETE FROM orders WHERE customer_id = $1 AND flag = $2", "DELETE", {}, KEY_SCOPED),
        (
            "DELETE FROM events WHERE source = $1",
            "DELETE",
            {"tables_accessed": ["app.events"]},
            KEY_SCOPED,
        ),
        # A filter with no key anchor changes every row that matches: a set the statement
        # does not bound.
        ("UPDATE orders SET status = $1 WHERE flag = $2", "UPDATE", {}, UNBOUNDED),
        ("DELETE FROM orders WHERE flag IN ($1, $2)", "DELETE", {}, UNBOUNDED),
        ("DELETE FROM orders WHERE id = $1 OR flag = $2", "DELETE", {}, UNBOUNDED),
        ("DELETE FROM orders WHERE NOT (id = $1)", "DELETE", {}, UNBOUNDED),
        # An OR of full-key values is as many items as branches (review of #1038).
        ("DELETE FROM orders WHERE id = $1 OR id = $2", "DELETE", {}, BOUNDED),
        ("DELETE FROM orders WHERE id = $1 OR email = $2", "DELETE", {}, BOUNDED),
        (
            "DELETE FROM orders WHERE tenant_id = $1 AND (code = $2 OR code = $3)",
            "DELETE",
            {},
            BOUNDED,
        ),
        ("DELETE FROM orders WHERE (id = $1)", "DELETE", {}, SINGLE_ITEM),
        (
            "UPDATE events SET kind = $1 WHERE kind = $2",
            "UPDATE",
            {"tables_accessed": ["app.events"]},
            UNBOUNDED,
        ),
        ("DELETE FROM orders WHERE customer_id = $1 OR flag = $2", "DELETE", {}, UNBOUNDED),
        # Every branch pins a key anchor: one item collection per branch.
        ("DELETE FROM orders WHERE customer_id = $1 OR status = $2", "DELETE", {}, KEY_SCOPED),
        # The condition's own key is not the write's (it guards, it changes nothing).
        (
            "UPDATE orders SET status = $1 WHERE flag = $2 "
            "AND NOT EXISTS (SELECT 1 FROM orders o2 WHERE o2.id = $3)",
            "UPDATE",
            {},
            UNBOUNDED,
        ),
        (
            "UPDATE orders SET status = $1 WHERE id = $2 "
            "AND NOT EXISTS (SELECT 1 FROM orders o2 WHERE o2.status = $3)",
            "UPDATE",
            {},
            SINGLE_ITEM,
        ),
        # An INSERT ... SELECT copies the rows its SELECT picks.
        (
            "INSERT INTO orders (id, status) SELECT id, $1 FROM orders WHERE id = $2",
            "INSERT",
            {},
            SINGLE_ITEM,
        ),
        (
            "INSERT INTO orders (id, status) SELECT id, $1 FROM orders WHERE flag = $2",
            "INSERT",
            {},
            UNBOUNDED,
        ),
        (
            "INSERT INTO orders (id, status) SELECT id, $1 FROM orders WHERE status = $2",
            "INSERT",
            {},
            KEY_SCOPED,
        ),
        # A measurement still decides.
        (
            "UPDATE orders SET status = $1 WHERE status = $2",
            "UPDATE",
            {"rows_affected_avg": 1},
            SINGLE_ITEM,
        ),
        # A table outside the collected schema has no key facts: the text check stands.
        (
            "UPDATE other SET status = $1 WHERE status = $2",
            "UPDATE",
            {"tables_accessed": ["app.other"]},
            SINGLE_ITEM,
        ),
    ],
)
def test_only_a_primary_or_unique_key_addresses_one_item(
    text: str, query_type: str, stats: dict, shape: str
) -> None:
    assert _keyed_shape(_write(text, query_type, **stats)).shape == shape


def test_the_reason_names_the_columns_that_are_not_a_key() -> None:
    shape = _keyed_shape(_write("DELETE FROM orders WHERE flag = $1", "DELETE"))
    assert shape.reason == (
        "addresses its rows by flag, no key of orders: the statement does not bound them"
    )
    assert shape.schema_note is None
    either = _keyed_shape(_write("DELETE FROM orders WHERE id = $1 OR flag = $2", "DELETE"))
    assert either.reason == (
        "addresses its rows by a predicate that pins no primary or unique key of orders: "
        "the statement does not bound them"
    )
    copied = _keyed_shape(
        _write("INSERT INTO orders (id) SELECT id FROM orders WHERE flag = $1", "INSERT")
    )
    assert copied.reason == "inserts the rows a query returns: the statement does not bound them"


def test_without_schema_keys_any_key_predicate_still_counts() -> None:
    """Callers that pass no keys keep the text check's answer (no fact to read)."""
    assert _shape(_write("UPDATE orders SET status = $1 WHERE status = $2", "UPDATE")) == (
        SINGLE_ITEM
    )


def test_without_a_dialect_an_empty_reading_is_no_evidence() -> None:
    """With no source engine, ``$2`` may not read as a value, so the facade pins
    nothing: that is not evidence of a non-unique key. A column it does pin still is."""
    pk = _write("UPDATE orders SET status = $1 WHERE id = $2", "UPDATE")
    assert _keyed_shape(pk, engine=None).shape == SINGLE_ITEM
    flag = _write("UPDATE orders SET code = ? WHERE flag = ?", "UPDATE")
    assert _keyed_shape(flag, engine=None).shape == UNBOUNDED


@pytest.mark.parametrize(
    ("engine", "text", "shape"),
    [
        ("sqlserver", "UPDATE [orders] SET [Status] = @s WHERE [Id] = @id", SINGLE_ITEM),
        ("sqlserver", "UPDATE [orders] SET [Status] = @s WHERE [Status] = @old", KEY_SCOPED),
        ("sqlserver", "UPDATE [orders] SET [Status] = @s WHERE [Flag] = @old", UNBOUNDED),
        ("mysql", "UPDATE `orders` SET `status` = ? WHERE `email` = ?", SINGLE_ITEM),
        ("mysql", "DELETE FROM `orders` WHERE `status` = ?", KEY_SCOPED),
        ("mysql", "DELETE FROM `orders` WHERE `flag` = ?", UNBOUNDED),
    ],
)
def test_the_key_check_reads_the_source_dialect(engine: str, text: str, shape: str) -> None:
    query = _write(text, text.split()[0])
    assert _keyed_shape(query, engine=engine).shape == shape


def test_the_key_check_parses_in_the_source_engines_dialect(monkeypatch) -> None:
    from src.agents.referee import transaction_shape as module

    seen = []
    real = module.query_access

    def recording(query, keys, source_engine):
        seen.append(source_engine)
        return real(query, keys, source_engine)

    monkeypatch.setattr(module, "query_access", recording)
    _keyed_shape(
        _write("UPDATE [orders] SET [Status] = @s WHERE [Id] = @id", "UPDATE"), "sqlserver"
    )
    assert seen == ["sqlserver"]


def test_the_dynamodb_analysis_reads_the_schema_keys() -> None:
    """Only a write whose filter has no key anchor is a wide transaction; a key-scoped
    write takes no penalty and its trace carries the schema-design note."""
    collector = _keyed_collector(
        _write("UPDATE orders SET status = $1 WHERE id = $2", "UPDATE", "by-pk"),
        _write("UPDATE orders SET code = $1 WHERE status = $2", "UPDATE", "by-status"),
        _write("UPDATE orders SET code = $1 WHERE flag = $2", "UPDATE", "by-flag"),
        engine="postgresql",
    )
    workload = analyze_dynamodb_use_cases(collector)
    wide = [
        ap
        for ap in workload.anti_patterns_detected or []
        if ap.anti_pattern_type == "wide-transaction"
    ]
    assert [ap.query_ids for ap in wide] == [["by-flag"]]
    trace = build_decision_trace(
        collector, workload, analyze_dynamodb_patterns(collector, workload)
    )
    shapes = {m["query_id"]: m.get("transaction_shape") for m in trace["query_matches"]}
    assert shapes["by-pk"]["shape"] == SINGLE_ITEM
    assert shapes["by-status"]["shape"] == KEY_SCOPED
    assert shapes["by-status"]["items"] is None
    assert shapes["by-status"]["reason"] == (
        "changes the rows one status value of orders addresses, a key that is not unique: "
        "a key-scoped multi-item write (a Query plus a batch write)"
    )
    assert shapes["by-status"]["schema_note"] == (
        "orders.status must become the partition key of an item collection (or a GSI's) "
        "so this write reads its items by key"
    )
    assert shapes["by-flag"]["shape"] == UNBOUNDED
    assert "schema_note" not in shapes["by-flag"]
    assert "flag, no key of orders" in shapes["by-flag"]["reason"]


def test_an_or_of_full_keys_counts_its_branches() -> None:
    shape = _keyed_shape(_write("DELETE FROM orders WHERE id = $1 OR id = $2", "DELETE"))
    assert (shape.shape, shape.items) == (BOUNDED, 2)
    assert shape.reason == "addresses its rows by 2 key value(s) on one table"


def test_an_insert_select_over_many_keys_keeps_its_item_count() -> None:
    """More than 100 key values is unbounded, and the trace keeps the count (review
    of #1038)."""
    shape = _keyed_shape(
        _write(f"INSERT INTO orders (id) SELECT id FROM orders WHERE id IN ({IN_150})", "INSERT")
    )
    assert (shape.shape, shape.items) == (UNBOUNDED, 150)
    assert shape.reason.startswith("copies the rows of 150 key value(s) on one table")


def _view_collector(query: dict) -> dict:
    """A collected table ``public.users`` and an updatable view ``public.active_users``."""
    return {
        "metadata": {"source_database": {"engine": "postgresql", "database_name": "app"}},
        "database_schema": {
            "tables": [
                {
                    "table_id": "public.users",
                    "table_name": "users",
                    "row_count": 1000,
                    "primary_key": ["id"],
                    "columns": [
                        {"column_name": "id", "data_type": "bigint"},
                        {"column_name": "email", "data_type": "text"},
                    ],
                }
            ],
            "views": [{"view_id": "public.active_users", "view_name": "active_users"}],
        },
        "queries": {"query_patterns": [query]},
    }


def test_a_write_through_a_view_keeps_the_text_check() -> None:
    """A view resolves to a collected id but has no keys: no verdict from the keys, the
    text check stands (ADR-031 Decision 3), and nothing raises (review of #1038)."""
    from src.agents.referee.access_pattern import SchemaKeys
    from src.agents.referee.table_resolution import SourceScope

    query = {
        "query_id": "v1",
        "query_text": "DELETE FROM active_users WHERE email = $1",
        "query_type": "DELETE",
        "tables_accessed": ["public.active_users"],
        "calls_per_second": 1.0,
    }
    collector = _view_collector(query)
    shape = transaction_shape(
        query, SourceScope.from_collector(collector), SchemaKeys.from_collector(collector)
    )
    assert shape is not None and shape.shape == SINGLE_ITEM
    workload = analyze_dynamodb_use_cases(collector)
    trace = build_decision_trace(
        collector, workload, analyze_dynamodb_patterns(collector, workload)
    )
    (match,) = [m for m in trace["query_matches"] if m["query_id"] == "v1"]
    assert match["transaction_shape"]["shape"] == SINGLE_ITEM


def test_a_key_scoped_write_takes_no_wide_transaction_penalty() -> None:
    shape = _keyed_shape(_write("DELETE FROM orders WHERE customer_id = $1", "DELETE"))
    assert shape.shape == KEY_SCOPED and not shape.needs_redesign
    copied = _keyed_shape(
        _write("INSERT INTO orders (id) SELECT id FROM orders WHERE status = $1", "INSERT")
    )
    assert copied.reason.startswith("copies the rows one status value of orders addresses")


def test_a_saved_1_0_trace_still_loads() -> None:
    """The contract adds a shape and an optional field (1.1): a 1.0 trace loads."""
    from src.contracts.dynamodb_decision_trace import TransactionShapeTrace

    old = TransactionShapeTrace.model_validate({"shape": "unbounded", "items": None, "reason": "r"})
    assert old.schema_note is None
    new = TransactionShapeTrace.model_validate(
        {"shape": "key scoped", "items": None, "reason": "r", "schema_note": "n"}
    )
    assert new.model_dump()["schema_note"] == "n"


def test_an_or_of_key_anchors_names_each_one() -> None:
    shape = _keyed_shape(
        _write("DELETE FROM orders WHERE customer_id = $1 OR status = $2", "DELETE")
    )
    assert shape.reason == (
        "changes the rows of orders that one value of each of customer_id, status addresses, "
        "a key that is not unique: a key-scoped multi-item write (a Query plus a batch write)"
    )
    assert shape.schema_note == (
        "orders.customer_id, orders.status must each become the partition key of an item "
        "collection (or a GSI's) so this write reads its items by key"
    )


class TestDecision3HoldsThroughTheProductionScorer:
    """ADR-031 Decision 3 through the production path (#1231 review): the context every
    routing pass builds (``ScoringContext.from_pipeline``, which labels each finding with
    the redesign) keeps the wide-transaction penalty on a write a range or a subquery
    picks. Only a write by a plain filter a redesign key anchors becomes key scoped,
    by Decision 3's own shape rule, and loses it. (``?`` placeholders: the generic
    dialect this fixture's collector reads in parses ``$n`` as no value.)"""

    def _ctx(self):
        from src.agents.referee.query_scoring import ScoringContext

        collector = _collector(
            _write("DELETE FROM orders WHERE created_at < ?", "DELETE", "by_range"),
            _write(
                "DELETE FROM orders WHERE id IN (SELECT id FROM orders WHERE region = ?)",
                "DELETE",
                "by_subquery",
            ),
            _write("UPDATE orders SET status = ? WHERE sku = ?", "UPDATE", "by_filter"),
        )
        collector["database_schema"]["tables"][0]["columns"] += [
            {"column_name": "region", "data_type": "text"},
            {"column_name": "sku", "data_type": "varchar(32)"},
        ]
        workload = analyze_dynamodb_use_cases(collector)
        analysis = {"dynamodb": {"workload_analysis": workload.model_dump()}}
        return ScoringContext.from_pipeline(analysis, {"query_capabilities": {}}, collector)

    @pytest.mark.passes_on_main  # Decision 3 already holds on main; this keeps it
    def test_a_write_a_range_or_a_subquery_picks_keeps_its_penalty(self) -> None:
        ctx = self._ctx()
        penalty = pattern("wide-transaction").penalty
        assert ctx.anti_pattern_map.get(("by_range", "dynamodb")) == penalty
        assert ctx.anti_pattern_map.get(("by_subquery", "dynamodb")) == penalty

    def test_a_write_a_redesign_key_anchors_is_key_scoped_and_unpenalised(self) -> None:
        ctx = self._ctx()
        assert ("by_filter", "dynamodb") not in ctx.anti_pattern_map
        kinds = {f.query_id: f.kind for f in ctx.anti_pattern_findings}
        assert kinds == {
            "by_range": "intrinsic",
            "by_subquery": "intrinsic",
            "by_filter": "fixable",
        }
