"""Per-query access-pattern classes (ADR-031 Decision 14, Amendment 5, #728).

Each query gets exactly one class from what it retrieves, read from the SQL
facade's facts (:func:`src.tools.sql.facade.access_facts`) and the schema's
keys -- never from its tables' other queries. The cases cover each class,
key-anchored joins (a key-scoped 1:N read, not a relational need), the
maintainer's row-limit rule (only a primary-key read with ``LIMIT 1``, at any
offset, is a key lookup; ``LIMIT n`` is 1:N) and the precedence when a query
shows several shapes.
"""

from __future__ import annotations

import pytest

from src.agents.referee.access_pattern import (
    AD_HOC_FILTER,
    AGGREGATION,
    CLASS_LABELS,
    KEY_LOOKUP,
    KEY_RANGE,
    KEY_SCOPED,
    REFERENCE_READ,
    SCORED_CLASSES,
    SMALL_TABLE_MAX_ROWS,
    TEXT_SEARCH,
    TRUNCATED_NOTE,
    UNANCHORED_JOIN,
    UNPARSED,
    UTILITY,
    SchemaKeys,
    classify_query,
    query_access,
)
from src.agents.referee.query_shape import is_primary_key_first_row_read


def _table(name, pk, columns, fks=(), indexes=(), types=None, cardinality=None, rows=None):
    types, cardinality = types or {}, cardinality or {}
    return {
        "table_id": f"shop.{name}",
        "table_name": name,
        "schema_name": "shop",
        "primary_key": list(pk),
        "row_count": rows,
        "columns": [
            {
                "column_name": c,
                "data_type": types.get(c, "integer"),
                "cardinality": cardinality.get(c),
            }
            for c in columns
        ],
        "foreign_keys": [
            {"columns": [c], "referenced_table": t, "referenced_columns": ["id"]} for c, t in fks
        ]
        or None,
        "indexes": [
            {"index_name": f"ix_{name}_{'_'.join(cols)}", "columns": list(cols), "is_unique": u}
            for cols, u in indexes
        ],
    }


COLLECTOR = {
    "database_schema": {
        "tables": [
            _table(
                "orders",
                ["id"],
                ["id", "customer_id", "status", "created_at", "total", "number"],
                fks=[("customer_id", "customers")],
                indexes=[(("created_at",), False), (("number",), False)],
                types={"created_at": "timestamp without time zone", "status": "varchar(20)"},
            ),
            _table(
                "order_items",
                ["id"],
                ["id", "order_id", "product_id", "qty"],
                fks=[("order_id", "orders")],
                indexes=[(("product_id",), False)],
            ),
            _table(
                "customers",
                ["id"],
                ["id", "email", "name"],
                indexes=[(("email",), True)],
            ),
            _table("products", ["id"], ["id", "title", "body", "price"]),
            _table("settings", ["name"], ["name", "value"]),
            _table(
                "posts",
                ["id"],
                ["id", "topic_id", "post_number", "user_id", "raw"],
                indexes=[(("topic_id", "post_number"), True), (("user_id",), False)],
            ),
            _table(
                "topics",
                ["id"],
                ["id", "category_id", "title"],
                indexes=[(("category_id",), False)],
            ),
            _table("categories", ["id"], ["id", "name", "slug"]),
            _table("flags", ["id"], ["id", "name", "enabled"], rows=40),
            _table("events", ["id"], ["id", "kind"], rows=SMALL_TABLE_MAX_ROWS + 1),
        ]
    }
}
KEYS = SchemaKeys.from_collector(COLLECTOR)


def _classify(sql, engine="postgresql", query_type=None):
    first = sql.split(None, 1)[0].upper()
    if first == "REPLACE":
        first = "INSERT"
    elif first not in {"SELECT", "INSERT", "UPDATE", "DELETE", "WITH"}:
        first = "OTHER"
    query = {"query_id": "q", "query_text": sql, "query_type": query_type or first}
    return classify_query(query, KEYS, engine).name


def _assert_class(expected, sql, engine="postgresql"):
    assert _classify(sql, engine) == expected, sql


class TestTheClassSet:
    def test_eight_scored_classes_in_severity_order(self):
        assert SCORED_CLASSES == (
            KEY_LOOKUP,
            KEY_SCOPED,
            REFERENCE_READ,
            KEY_RANGE,
            AD_HOC_FILTER,
            UNANCHORED_JOIN,
            AGGREGATION,
            TEXT_SEARCH,
        )

    def test_every_class_has_a_plain_language_label(self):
        for name in (*SCORED_CLASSES, UTILITY, UNPARSED):
            assert CLASS_LABELS[name] and "_" not in CLASS_LABELS[name]

    def test_a_classification_carries_a_reason(self):
        query = {"query_id": "q", "query_text": "SELECT * FROM orders WHERE id = $1"}
        result = classify_query(query, KEYS, "postgresql")
        assert result.name == KEY_LOOKUP
        assert "orders" in result.reason


class TestKeyLookup:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM orders WHERE id = $1",
            'SELECT "orders".* FROM "orders" WHERE "orders"."id" = $1 LIMIT $2',
            "SELECT * FROM orders WHERE id IN ($1, $2, $3)",
            "SELECT * FROM orders WHERE id = ANY($1)",
            "SELECT * FROM customers WHERE email = $1",
            "SELECT * FROM orders WHERE id = $1 AND status = $2",
            "SELECT * FROM orders WHERE id = $1 FOR UPDATE",
            "SELECT * FROM orders WHERE id = $1 FOR NO KEY UPDATE NOWAIT",
            "UPDATE orders SET status = $1 WHERE id = $2",
            "DELETE FROM orders WHERE id = $1",
            "INSERT INTO orders (customer_id, status) VALUES ($1, $2)",
            "INSERT INTO orders (customer_id, status) VALUES ($1, $2) RETURNING id",
            "INSERT INTO settings (name, value) VALUES ($1, $2) "
            "ON CONFLICT (name) DO UPDATE SET value = excluded.value",
            "SELECT * FROM posts WHERE topic_id = $1 AND post_number = $2",
        ],
    )
    def test_a_read_or_write_of_one_row_by_its_key(self, sql):
        _assert_class(KEY_LOOKUP, sql)

    @pytest.mark.parametrize(
        "sql,engine",
        [
            ("SELECT * FROM orders ORDER BY id LIMIT 1", "postgresql"),
            ("SELECT * FROM orders ORDER BY id DESC LIMIT 1", "postgresql"),
            ("SELECT * FROM orders ORDER BY id LIMIT 1 OFFSET 5", "postgresql"),
            ("SELECT * FROM orders ORDER BY id LIMIT 1 OFFSET $1", "postgresql"),
            ("SELECT * FROM orders ORDER BY id OFFSET $1 LIMIT 1", "postgresql"),
            ("SELECT * FROM `orders` ORDER BY `id` LIMIT 5, 1", "mysql"),
            ("SELECT * FROM `orders` ORDER BY `id` LIMIT ?, 1", "mysql"),
            ("SELECT * FROM orders ORDER BY id FETCH FIRST ROW ONLY", "postgresql"),
            ("SELECT TOP 1 * FROM orders ORDER BY id", "sqlserver"),
            ("SELECT TOP (1) * FROM orders ORDER BY id", "sqlserver"),
            (
                "SELECT * FROM orders ORDER BY id OFFSET 5 ROWS FETCH NEXT 1 ROWS ONLY",
                "sqlserver",
            ),
            (
                "SELECT * FROM orders ORDER BY id OFFSET @skip ROWS FETCH NEXT 1 ROW ONLY",
                "sqlserver",
            ),
        ],
    )
    def test_one_row_in_primary_key_order_at_any_offset(self, sql, engine):
        """The maintainer rule (#857): ``ORDER BY <primary key> LIMIT 1`` is a key lookup."""
        _assert_class(KEY_LOOKUP, sql, engine)
        assert is_primary_key_first_row_read(sql, ["id"]), "agrees with Decision 14's check"


class TestKeyScopedOneToMany:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM order_items WHERE order_id = $1",
            "SELECT * FROM orders WHERE customer_id = $1 ORDER BY created_at DESC LIMIT 10",
            "SELECT * FROM posts WHERE topic_id = $1",
            "SELECT * FROM posts WHERE topic_id = $1 AND post_number > $2 ORDER BY post_number",
            "SELECT * FROM posts WHERE user_id = $1 AND raw IS NOT NULL",
            "UPDATE order_items SET qty = 0 WHERE order_id = $1",
            "DELETE FROM order_items WHERE order_id = $1",
            "SELECT COUNT(*) FROM order_items WHERE order_id = $1",
            "SELECT * FROM order_items WHERE order_id IN ($1, $2)",
            # A ranking window function combines nothing (the #875 definition).
            "SELECT id, row_number() OVER (ORDER BY created_at) FROM orders WHERE customer_id = $1",
        ],
    )
    def test_rows_addressed_by_a_non_unique_key(self, sql):
        _assert_class(KEY_SCOPED, sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT COUNT(*) FROM orders o JOIN order_items i ON i.order_id = o.id WHERE o.id = $1",
            "SELECT SUM(i.qty) FROM orders o JOIN order_items i ON i.order_id = o.id "
            "WHERE o.customer_id = $1",
        ],
    )
    def test_an_aggregate_over_a_key_anchored_join_is_key_scoped(self, sql):
        """The same rule as one table: it combines the rows of one item collection."""
        _assert_class(KEY_SCOPED, sql)

    @pytest.mark.parametrize(
        "sql,engine,expected",
        [
            ("SELECT * FROM products WHERE id = $1 AND title LIKE '%x%'", "postgresql", KEY_LOOKUP),
            (
                "SELECT * FROM orders WHERE customer_id = $1 AND status LIKE $2",
                "postgresql",
                KEY_SCOPED,
            ),
            (
                "SELECT * FROM `orders` WHERE `customer_id` = ? AND `status` LIKE ?",
                "mysql",
                KEY_SCOPED,
            ),
            (
                "SELECT * FROM orders o JOIN order_items i ON i.order_id = o.id "
                "WHERE o.id = $1 AND o.status ILIKE '%x%'",
                "postgresql",
                KEY_SCOPED,
            ),
        ],
    )
    def test_a_pattern_match_on_keyed_rows_only_filters_them(self, sql, engine, expected):
        """A ``LIKE`` never outranks key anchoring: it filters one item collection."""
        result = classify_query(
            {"query_id": "q", "query_text": sql, "query_type": "SELECT"}, KEYS, engine
        )
        assert result.name == expected
        assert "pattern match only filters the keyed rows" in result.reason

    def test_limit_one_on_a_foreign_key_is_one_to_many_not_a_key_lookup(self):
        """Only a primary-key read with ``LIMIT 1`` is key-value (maintainer decision)."""
        _assert_class(KEY_SCOPED, "SELECT * FROM orders WHERE customer_id = $1 LIMIT 1")

    @pytest.mark.parametrize(
        "sql",
        [
            # An entity and its children by key: one item collection.
            "SELECT * FROM orders o JOIN order_items i ON i.order_id = o.id WHERE o.id = $1",
            "SELECT o.*, i.* FROM orders o LEFT JOIN order_items i ON o.id = i.order_id "
            "WHERE o.id = $1",
            # Anchored on the child side, reaching the parent by its key.
            "SELECT * FROM order_items i JOIN orders o ON o.id = i.order_id WHERE i.order_id = $1",
            # A comma join anchored the same way.
            "SELECT * FROM orders o, order_items i WHERE i.order_id = o.id AND o.id = $1",
            # Three tables, every one reachable by key from the anchor.
            "SELECT * FROM customers c JOIN orders o ON o.customer_id = c.id "
            "JOIN order_items i ON i.order_id = o.id WHERE c.id = $1",
            # The #885 shape: a topic and its category by the topic's key.
            "SELECT t.*, c.name FROM topics t JOIN categories c ON c.id = t.category_id "
            "WHERE t.id = $1",
            # Unqualified columns resolved through the schema.
            "SELECT title, name FROM topics JOIN categories ON categories.id = category_id "
            "WHERE topics.id = $1",
            "SELECT * FROM orders JOIN order_items USING (id) WHERE orders.id = $1",
        ],
    )
    def test_a_key_anchored_join_is_a_key_scoped_read_not_a_relational_need(self, sql):
        _assert_class(KEY_SCOPED, sql)

    def test_a_key_anchored_join_in_mysql_digest_text(self):
        sql = (
            "SELECT `o` . * FROM `orders` `o` INNER JOIN `order_items` `i` "
            "ON `o` . `id` = `i` . `order_id` WHERE `o` . `id` = ?"
        )
        _assert_class(KEY_SCOPED, sql, "mysql")

    def test_keys_supplied_by_a_subquery_keep_the_outer_read_keyed(self):
        sql = (
            "SELECT * FROM orders WHERE id IN "
            "(SELECT order_id FROM order_items WHERE product_id = $1)"
        )
        _assert_class(KEY_SCOPED, sql)


class TestRangeOrOrderedReadByKey:
    @pytest.mark.parametrize(
        "sql,engine",
        [
            ("SELECT * FROM orders ORDER BY id LIMIT 10", "postgresql"),
            ("SELECT * FROM orders ORDER BY id LIMIT $1", "postgresql"),
            ("SELECT * FROM orders ORDER BY id LIMIT 5 OFFSET $1", "postgresql"),
            ("SELECT * FROM `orders` ORDER BY `id` LIMIT ?", "mysql"),
            ("SELECT TOP (1) PERCENT * FROM orders ORDER BY id", "sqlserver"),
            ("SELECT TOP (1) WITH TIES * FROM orders ORDER BY id", "sqlserver"),
            (
                "SELECT * FROM orders ORDER BY id OFFSET 5 ROWS FETCH NEXT 1 ROW WITH TIES",
                "sqlserver",
            ),
        ],
    )
    def test_limit_n_in_primary_key_order_is_one_to_many(self, sql, engine):
        _assert_class(KEY_RANGE, sql, engine)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM orders WHERE id BETWEEN $1 AND $2",
            "SELECT * FROM orders WHERE id > $1 ORDER BY id LIMIT 50",
            "SELECT * FROM orders WHERE number >= $1",
            "SELECT * FROM customers WHERE email LIKE 'a%'",
            "SELECT * FROM orders ORDER BY number DESC LIMIT 20",
            "DELETE FROM orders WHERE number < $1",
        ],
    )
    def test_a_range_or_order_over_a_key_with_no_anchoring_value(self, sql):
        _assert_class(KEY_RANGE, sql)

    def test_a_full_read_with_no_predicate_is_the_whole_key_range(self):
        _assert_class(KEY_RANGE, "SELECT name, value FROM settings")

    def test_an_order_with_no_row_limit_does_not_change_what_is_read(self):
        """Sorting every row is presentation; only a row limit makes the order pick rows."""
        _assert_class(KEY_RANGE, "SELECT * FROM settings ORDER BY value")


class TestSmallReferenceRead:
    """#920: every row of a small table, with no predicate and no row limit, is a
    reference read (feature flags, settings, a lookup list), not a range read."""

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT id, name, enabled FROM flags",
            "SELECT * FROM flags ORDER BY name",  # an order with no limit picks no rows
            "SELECT DISTINCT name FROM flags",
        ],
    )
    def test_a_small_table_read_whole_is_a_reference_read(self, sql):
        _assert_class(REFERENCE_READ, sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT id, kind FROM events",  # one row over the limit
            "SELECT name, value FROM settings",  # no collected row count: not known small
            "SELECT * FROM flags ORDER BY id LIMIT $1",  # a row limit picks rows
            "SELECT * FROM flags FOR UPDATE SKIP LOCKED",  # an unknown row
        ],
    )
    def test_anything_else_is_not(self, sql):
        assert _classify(sql) == KEY_RANGE, sql

    def test_a_predicate_or_an_aggregate_is_not_a_reference_read(self):
        _assert_class(KEY_LOOKUP, "SELECT * FROM flags WHERE id = $1")
        _assert_class(AD_HOC_FILTER, "SELECT * FROM flags WHERE enabled = $1")
        _assert_class(AGGREGATION, "SELECT count(*) FROM flags")


class TestSqlFeatureFacts:
    """#985: the SQL features a class score may credit, read from the shared query facts
    in the source's dialect (one definition each), never from the classifier's own text."""

    def _facts(self, sql, engine="postgresql", query_type="SELECT"):
        query = {"query_id": "q", "query_text": sql, "query_type": query_type}
        return classify_query(query, KEYS, engine).facts

    @pytest.mark.parametrize(
        ("sql", "engine", "fact"),
        [
            (
                "WITH RECURSIVE t AS (SELECT id FROM categories WHERE id = $1 UNION ALL "
                "SELECT c.id FROM categories c JOIN t ON c.id = t.id) SELECT * FROM t",
                "postgresql",
                "recursive_cte",
            ),
            (
                "WITH [T] AS (SELECT [id] FROM [categories] WHERE [id] = @Id UNION ALL "
                "SELECT c.[id] FROM [categories] c JOIN [T] t ON c.[id] = t.[id]) SELECT * FROM [T]",
                "sqlserver",
                "recursive_cte",
            ),
            (
                "SELECT o.id, x.n FROM orders o CROSS JOIN LATERAL (SELECT count(*) n "
                "FROM order_items i WHERE i.order_id = o.id) x",
                "postgresql",
                "lateral_join",
            ),
            ("CALL refresh_totals(?)", "mysql", "stored_routine"),
        ],
    )
    def test_the_feature_is_read_from_the_query_facts(self, sql, engine, fact):
        assert fact in self._facts(sql, engine)

    @pytest.mark.parametrize(
        "sql",
        [
            "WITH t AS (SELECT id FROM categories WHERE id = $1) SELECT * FROM t",  # not recursive
            "SELECT * FROM orders WHERE note = 'lateral (x)'",  # a word in a literal
            "SELECT * FROM orders WHERE id = $1",
        ],
    )
    def test_not_without_the_feature(self, sql):
        assert not {"recursive_cte", "lateral_join", "stored_routine"} & self._facts(sql)


class TestPartitionRangeFact:
    """#933's shape, one definition (``AccessClassification.facts``): a scope that reads
    one table, pins a key column of it and ranges another of its columns."""

    def _facts(self, sql):
        query = {"query_id": "q", "query_text": sql, "query_type": sql.split()[0].upper()}
        return classify_query(query, KEYS, "postgresql").facts

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM orders WHERE customer_id = $1 AND created_at > $2",
            "SELECT * FROM posts WHERE topic_id = $1 AND post_number BETWEEN $2 AND $3",
            "SELECT * FROM topics WHERE category_id IN ($1, $2) AND id < $3",
        ],
    )
    def test_a_key_equality_plus_a_range_on_one_table(self, sql):
        assert "partition_range" in self._facts(sql)

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM orders WHERE customer_id = $1",  # no range
            "SELECT * FROM orders WHERE created_at > $1",  # no pinned key
            "SELECT * FROM orders WHERE status = $1 AND created_at > $2",  # status is no key
            "SELECT * FROM orders WHERE id = $1 AND id > $2",  # the same column
            # a join: its other table is an item collection, rated by the join patterns
            "SELECT o.id FROM orders o JOIN order_items i ON i.order_id = o.id "
            "WHERE o.customer_id = $1 AND o.created_at > $2",
        ],
    )
    def test_not_without_both_on_one_table(self, sql):
        assert "partition_range" not in self._facts(sql)

    def test_the_key_access_reader_reads_the_same_fact(self):
        from src.tools.analysis.key_access import KeyAccessReader

        collector = {**COLLECTOR, "metadata": {"source_database": {"engine": "postgresql"}}}
        reader = KeyAccessReader.from_collector(collector, "postgresql")
        for sql in (
            "SELECT * FROM orders WHERE customer_id = $1 AND created_at > $2",
            "SELECT * FROM orders WHERE customer_id = $1",
            "SELECT o.id FROM orders o JOIN order_items i ON i.order_id = o.id "
            "WHERE o.customer_id = $1 AND o.created_at > $2",
        ):
            query = {"query_id": "q", "query_text": sql, "query_type": "SELECT"}
            assert reader.access(query).partition_range is ("partition_range" in self._facts(sql))

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM orders WHERE id = $1 FOR UPDATE SKIP LOCKED",
            "SELECT * FROM orders ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED",
        ],
    )
    def test_skip_locked_returns_an_unknown_row_so_it_is_never_a_key_lookup(self, sql):
        """Decision 14 (#840): at most an ordered 1:N read."""
        _assert_class(KEY_RANGE, sql)

    def test_readpast_hint_is_skip_locked_too(self):
        sql = "SELECT TOP 1 * FROM orders WITH (READPAST, UPDLOCK) ORDER BY id"
        _assert_class(KEY_RANGE, sql, "sqlserver")


class TestAdHocFilter:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM orders WHERE status = $1",
            "SELECT * FROM orders WHERE total > 100",
            "SELECT * FROM orders WHERE status = $1 AND total > $2",
            "SELECT * FROM orders WHERE status = $1 ORDER BY id LIMIT 1",
            "SELECT * FROM orders ORDER BY total DESC LIMIT 5",
            "SELECT * FROM orders WHERE id = $1 OR status = $2",
            "SELECT * FROM orders WHERE id NOT IN ($1, $2)",
            "SELECT * FROM products WHERE title LIKE 'boot%'",
            "SELECT * FROM products WHERE title LIKE $1 || '%'",
            "UPDATE orders SET status = 'closed' WHERE status = 'open'",
            "UPDATE orders SET status = 'closed'",
        ],
    )
    def test_filters_on_non_key_attributes_with_no_anchoring_key(self, sql):
        _assert_class(AD_HOC_FILTER, sql)


class TestUnanchoredJoin:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM orders o JOIN order_items i ON i.order_id = o.id WHERE o.status = $1",
            "SELECT * FROM orders o JOIN customers c ON c.id = o.customer_id",
            # The anchor reaches orders, but customers joins on non-key columns.
            "SELECT * FROM orders o JOIN customers c ON c.name = o.status WHERE o.id = $1",
            # Anchored, but a third table is not reachable from it by key.
            "SELECT * FROM orders o JOIN order_items i ON i.order_id = o.id "
            "CROSS JOIN products p WHERE o.id = $1",
            "UPDATE orders o SET status = 'x' FROM customers c "
            "WHERE c.id = o.customer_id AND c.name = $1",
        ],
    )
    def test_joins_with_no_key_predicate_anchoring_every_table(self, sql):
        _assert_class(UNANCHORED_JOIN, sql)


class TestAggregation:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT status, COUNT(*) FROM orders GROUP BY status",
            "SELECT COUNT(*) FROM orders",
            "SELECT SUM(total) FROM orders WHERE created_at > $1",
            "SELECT customer_id, SUM(total) FROM orders WHERE customer_id = $1 "
            "GROUP BY customer_id",
            "SELECT id, SUM(total) OVER (ORDER BY created_at) FROM orders WHERE customer_id = $1",
            "SELECT o.status, COUNT(*) FROM orders o JOIN order_items i "
            "ON i.order_id = o.id GROUP BY o.status",
            "SELECT status FROM orders GROUP BY status HAVING COUNT(*) > 1",
            # An aggregate over a join no key anchors.
            "SELECT SUM(i.qty) FROM orders o JOIN order_items i ON i.order_id = o.id "
            "WHERE o.status = $1",
        ],
    )
    def test_group_by_unanchored_aggregates_and_window_functions(self, sql):
        _assert_class(AGGREGATION, sql)


class TestGroupByWithNoAggregate:
    """A ``GROUP BY`` that combines no values only de-duplicates the rows it reads
    (like ``DISTINCT``), so it does not make the read an aggregation."""

    @pytest.mark.parametrize(
        "sql,engine,expected",
        [
            (
                # The wordpress shape (`GROUP BY wp_posts.ID`), as a digest.
                "SELECT `orders` . `id` FROM `orders` WHERE `orders` . `id` IN (...) "
                "GROUP BY `orders` . `id`",
                "mysql",
                KEY_LOOKUP,
            ),
            (
                "SELECT customer_id FROM orders WHERE customer_id = $1 GROUP BY customer_id",
                "postgresql",
                KEY_SCOPED,
            ),
            (
                "SELECT status FROM orders WHERE status = $1 GROUP BY status",
                "postgresql",
                AD_HOC_FILTER,
            ),
            (
                "SELECT o.id FROM orders o JOIN order_items i ON i.order_id = o.id "
                "WHERE o.id = $1 GROUP BY o.id",
                "postgresql",
                KEY_SCOPED,
            ),
        ],
    )
    def test_the_class_is_the_one_of_the_rows_it_reads(self, sql, engine, expected):
        _assert_class(expected, sql, engine)

    def test_a_group_by_with_an_aggregate_is_still_aggregation(self):
        _assert_class(
            AGGREGATION,
            "SELECT customer_id, COUNT(*) FROM orders WHERE customer_id = $1 GROUP BY customer_id",
        )
        _assert_class(AGGREGATION, "SELECT status FROM orders GROUP BY status HAVING COUNT(*) > 1")


class TestTextSearch:
    @pytest.mark.parametrize(
        "sql,engine",
        [
            ("SELECT * FROM products WHERE title LIKE '%boot%'", "postgresql"),
            ("SELECT * FROM products WHERE title NOT LIKE '%x'", "postgresql"),
            ("SELECT * FROM products WHERE title ~* $1", "postgresql"),
            (
                "SELECT * FROM products WHERE to_tsvector(body) @@ to_tsquery($1)",
                "postgresql",
            ),
            ("SELECT * FROM products WHERE similarity(title, $1) > 0.3", "postgresql"),
            ("SELECT * FROM `products` WHERE MATCH(`title`) AGAINST ('boots')", "mysql"),
            ("SELECT * FROM products WHERE CONTAINS(body, @p)", "sqlserver"),
            ("SELECT * FROM products WHERE title LIKE '%' + @term + '%'", "sqlserver"),
            # Text search wins over aggregation when no key anchors the rows.
            ("SELECT COUNT(*) FROM products WHERE title ILIKE '%x%'", "postgresql"),
        ],
    )
    def test_wildcard_and_full_text_matches(self, sql, engine):
        _assert_class(TEXT_SEARCH, sql, engine)

    @pytest.mark.parametrize(
        "sql,engine",
        [
            ("SELECT * FROM products WHERE title ILIKE $1", "postgresql"),
            ("SELECT * FROM `products` WHERE `title` LIKE ?", "mysql"),
        ],
    )
    def test_a_parameterised_like_shows_no_text_search(self, sql, engine):
        """#920 (was text search): the bound pattern is unknown (#912), so the query is
        classed by its other predicates: here a filter on a non-key column."""
        _assert_class(AD_HOC_FILTER, sql, engine)


class TestPrecedence:
    def test_the_most_demanding_shape_in_any_part_of_the_statement_wins(self):
        sql = "SELECT * FROM orders WHERE id = $1 UNION ALL SELECT * FROM orders WHERE status = $2"
        _assert_class(AD_HOC_FILTER, sql)

    def test_a_subquery_with_its_own_aggregation_makes_the_statement_aggregation(self):
        sql = "SELECT * FROM orders WHERE total > (SELECT AVG(total) FROM orders)"
        _assert_class(AGGREGATION, sql)

    def test_insert_select_takes_the_class_of_the_rows_it_reads(self):
        sql = "INSERT INTO order_items (order_id, qty) SELECT id, 1 FROM orders WHERE status = $1"
        _assert_class(AD_HOC_FILTER, sql)

    def test_an_insert_select_from_dual_is_a_keyed_write_plus_its_condition(self):
        sql = (
            "INSERT INTO `orders` (`status`) SELECT ? FROM DUAL WHERE "
            "(SELECT `id` FROM `orders` WHERE `id` = ? LIMIT ?) IS NULL"
        )
        _assert_class(KEY_LOOKUP, sql, "mysql")

    def test_an_anchored_derived_table_on_the_optional_side_anchors_nothing(self):
        """``p LEFT JOIN (keyed subquery)`` still returns every row of ``p``."""
        sql = (
            "SELECT o.id FROM orders o LEFT JOIN "
            "(SELECT order_id FROM order_items WHERE order_id = $1) x "
            "ON x.order_id = o.id WHERE x.order_id IS NULL"
        )
        _assert_class(UNANCHORED_JOIN, sql)

    @pytest.mark.parametrize(
        "sql,engine",
        [
            (
                "SELECT o.id, j.qty FROM orders o CROSS APPLY OPENJSON(o.status) "
                "WITH (qty int '$.qty') j WHERE o.id = @id",
                "sqlserver",
            ),
            ("SELECT * FROM orders o, unnest($2::int[]) AS u(v) WHERE o.id = $1", "postgresql"),
        ],
    )
    def test_a_join_to_a_function_of_the_row_reads_no_other_table(self, sql, engine):
        _assert_class(KEY_LOOKUP, sql, engine)

    def test_a_cte_read_by_key_stays_keyed(self):
        sql = (
            "WITH o AS (SELECT * FROM orders WHERE id = $1) "
            "SELECT * FROM o JOIN order_items i ON i.order_id = o.id"
        )
        _assert_class(KEY_SCOPED, sql)


class TestNotAnAccessPattern:
    @pytest.mark.parametrize(
        "sql,engine",
        [
            ("SHOW search_path", "postgresql"),
            ("SET NAMES utf8mb4", "mysql"),
            ("BEGIN", "postgresql"),
            ("COMMIT", "postgresql"),
            ("SELECT 1", "postgresql"),
            ("SELECT pg_advisory_lock($1)", "postgresql"),
            ("SELECT to_tsvector($1, $2)", "postgresql"),
            ("SAVEPOINT active_record_1", "postgresql"),
            ("RELEASE SAVEPOINT active_record_1", "postgresql"),
            ("CREATE TABLE t (id int)", "postgresql"),
        ],
    )
    def test_statements_that_read_or_write_no_table_data(self, sql, engine):
        _assert_class(UTILITY, sql, engine)

    def test_text_the_parser_cannot_read_is_unparsed_not_guessed(self):
        _assert_class(UNPARSED, "SELECT * FROM orders WHERE ((( id = ")

    def test_a_write_the_parser_cannot_read_is_unparsed_not_utility(self):
        """A data statement the facade cannot read is never ``utility`` (#986)."""
        _assert_class(UNPARSED, "UPDATE settings SET value = (((", "mysql")
        assert _classify("UPDATE settings SET value = (((", "mysql", "UPDATE") == UNPARSED


class TestMysqlReplaceInto:
    """#986: MySQL ``REPLACE INTO`` writes rows by key, like ``INSERT ... ON DUPLICATE KEY
    UPDATE``; sqlglot reads it only as a generic command, so the facade reads it as
    the ``INSERT`` it is."""

    @pytest.mark.parametrize("engine", ["mysql", "mariadb"])
    def test_is_a_write_by_key_like_an_insert_upsert(self, engine):
        replace = "REPLACE INTO `settings` (`name`, `value`) VALUES (?, ?)"
        upsert = (
            "INSERT INTO `settings` (`name`, `value`) VALUES (?, ?) "
            "ON DUPLICATE KEY UPDATE `value` = ?"
        )
        assert _classify(replace, engine) == KEY_LOOKUP
        assert _classify(replace, engine) == _classify(upsert, engine)

    def test_low_priority_and_lower_case_and_leading_space(self):
        for sql in (
            "REPLACE LOW_PRIORITY INTO settings (name, value) VALUES (?, ?)",
            "  replace into settings (name, value) VALUES (?, ?)",
        ):
            _assert_class(KEY_LOOKUP, sql, "mysql")

    def test_replace_select_is_a_write_too(self):
        assert _classify(
            "REPLACE INTO settings (name, value) SELECT name, 1 FROM flags", "mysql"
        ) == _classify("INSERT INTO settings (name, value) SELECT name, 1 FROM flags", "mysql")

    def test_only_mysql_reads_it_so(self):
        """PostgreSQL and SQL Server have no ``REPLACE INTO``: not guessed."""
        for engine in ("postgresql", "sqlserver"):
            _assert_class(UNPARSED, "REPLACE INTO settings (name, value) VALUES (?, ?)", engine)

    @pytest.mark.passes_on_main
    def test_a_replace_without_into_is_left_alone(self):
        """``REPLACE t`` with no ``INTO`` falls back (a safe default)."""
        _assert_class(UNPARSED, "REPLACE settings (name) VALUES (?)", "mysql")

    def test_a_replace_after_a_leading_comment_reads_like_its_twin(self):
        """A leading comment no longer hides the REPLACE (#1178: it used to fall back)."""
        plain = "REPLACE INTO settings (name) VALUES (?)"
        assert _classify(plain, "mysql", "INSERT") != UNPARSED
        for prefix in ("/* x */ ", "-- x\n", "# x\n"):
            assert _classify(prefix + plain, "mysql", "INSERT") == _classify(
                plain, "mysql", "INSERT"
            )

    def test_the_replace_function_is_not_a_statement_keyword(self):
        """``REPLACE(...)`` in a select list or a string stays what it was."""
        _assert_class(
            KEY_LOOKUP, "SELECT REPLACE(value, 'a', 'b') FROM settings WHERE name = ?", "mysql"
        )
        _assert_class(
            AD_HOC_FILTER, "SELECT * FROM settings WHERE value = 'REPLACE INTO x'", "mysql"
        )


class TestACutShortDigest:
    """#986: the collector cuts a long digest at a length cap, often mid-predicate. The
    facade reads it up to its last complete predicate, and the class is never an anchor
    the cut-off predicates could not show."""

    ORDERS = "SELECT * FROM orders WHERE id = ? AND status = ? AND"

    def _result(self, sql, engine="mysql"):
        query = {"query_id": "q", "query_text": sql, "query_type": sql.split(None, 1)[0].upper()}
        return classify_query(query, KEYS, engine)

    def test_is_classified_not_unparsed_and_says_so(self):
        result = self._result(self.ORDERS)
        assert result.name != UNPARSED
        assert result.reason.endswith(TRUNCATED_NOTE)

    def test_the_same_statement_complete_keeps_its_key_anchor(self):
        complete = self._result("SELECT * FROM orders WHERE id = ? AND status = ?")
        assert complete.name == KEY_LOOKUP
        assert TRUNCATED_NOTE not in complete.reason

    def test_a_cut_short_prefix_is_never_key_anchored(self):
        """The prefix pins the key, but the unseen tail is no evidence of an anchor."""
        assert self._result(self.ORDERS).name == AD_HOC_FILTER

    def test_a_cut_short_join_is_an_unanchored_join(self):
        sql = (
            "SELECT o.id FROM orders o JOIN order_items i ON ( o.id = i.order_id ) "
            "WHERE o.id = ? AND i.qty > ? AND ( o.status = ? AND"
        )
        assert self._result(sql).name == UNANCHORED_JOIN

    def test_a_cut_short_aggregate_is_an_aggregation(self):
        sql = "SELECT SUM(total) FROM orders WHERE customer_id = ? AND created_at >= ? AND"
        assert self._result(sql).name == AGGREGATION

    def test_a_prefix_already_text_search_keeps_it(self):
        sql = "SELECT * FROM products WHERE title LIKE '%x%' AND price > ? AND"
        assert self._result(sql).name == TEXT_SEARCH

    def test_text_cut_where_no_predicate_is_complete_stays_unparsed(self):
        for sql in ("SELECT * FROM orders WHERE", "SELECT * FROM orders WHERE ((( id = "):
            assert self._result(sql).name == UNPARSED, sql

    def test_a_cut_inside_a_quoted_and_is_not_a_cut_point(self):
        sql = "SELECT * FROM orders WHERE status = 'a AND b' AND"
        result = self._result(sql)
        assert result.name == AD_HOC_FILTER
        assert result.reason.endswith(TRUNCATED_NOTE)

    def test_a_complete_oracle_statement_with_an_alternative_quote_is_read_whole(self):
        """Review of #986: a complete statement is never read as a cut-short prefix. Since
        #1481 the facade reads the ``q'[...]'`` string, so the statement is classed whole."""
        sql = "SELECT * FROM orders o WHERE o.id = :1 AND o.note = q'[don't AND stop]'"
        result = self._result(sql, "oracle")
        assert result.name != UNPARSED and TRUNCATED_NOTE not in result.reason

    def test_a_cut_short_write_is_classed_without_its_outer_where(self):
        sql = "UPDATE orders SET total = ( SELECT max(qty) FROM order_items WHERE order_id = orders.id AND"
        result = self._result(sql, "postgresql")
        assert result.name != UNPARSED and result.reason.endswith(TRUNCATED_NOTE)

    def test_postgresql_digests_are_repaired_in_their_dialect(self):
        sql = "SELECT * FROM orders WHERE customer_id = $1 AND status = $2 AND"
        assert self._result(sql, "postgresql").name == AD_HOC_FILTER

    def test_the_columns_it_pins_are_not_handed_to_the_analyses(self):
        """The key-access and attribute analyses read the columns a query pins; a cut-short
        digest's tail may pin others."""
        query = {"query_id": "q", "query_text": self.ORDERS, "query_type": "SELECT"}
        assert query_access(query, KEYS, "mysql").scopes == ()
        full = dict(query, query_text="SELECT * FROM orders WHERE id = ? AND status = ?")
        assert query_access(full, KEYS, "mysql").scopes != ()

    def test_a_digest_the_facade_falls_back_on_is_unparsed(self):
        """The digest step turns ``AGAINST (?)`` into ``AGAINST (NULL)``, which sqlglot
        rejects; the facade falls back, so the class is not guessed (a facade gap)."""
        _assert_class(
            UNPARSED, "SELECT * FROM `products` WHERE MATCH(`title`) AGAINST (?)", "mysql"
        )

    def test_a_table_outside_the_schema_has_no_keys(self):
        _assert_class(AD_HOC_FILTER, "SELECT * FROM pg_class WHERE oid = $1")


def test_the_classifier_reads_facts_and_parses_no_sql_itself():
    """Each class is detected from the shared facts, not new regexes (#728)."""
    import ast
    from pathlib import Path

    import src.agents.referee.access_pattern as classifier
    import src.agents.referee.query_pattern as model
    import src.shared.catalog.access_classes as classes

    for module in (classifier, model, classes):
        tree = ast.parse(Path(module.__file__).read_text())
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in (
                node.names if isinstance(node, ast.Import) else [ast.alias(node.module or "")]
            )
        }
        assert not imported & {"re", "sqlglot"}, module.__name__


class TestWhichIndexColumnsAreKeys:
    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM orders WHERE created_at >= $1",
            "SELECT * FROM orders WHERE created_at = $1",
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT 20",
            "DELETE FROM orders WHERE created_at < $1",
        ],
    )
    def test_a_timestamp_index_orders_rows_it_does_not_address_them(self, sql):
        _assert_class(AD_HOC_FILTER, sql)

    def _keys(self, cardinality, rows):
        table = _table(
            "events",
            ["id"],
            ["id", "kind"],
            indexes=[(("kind",), False)],
            cardinality={"kind": cardinality},
            rows=rows,
        )
        return SchemaKeys.from_collector({"database_schema": {"tables": [table]}})

    def test_statistics_that_mark_a_column_low_cardinality_take_its_key_away(self):
        query = {"query_id": "q", "query_text": "SELECT * FROM events WHERE kind = $1"}
        low = classify_query(query, self._keys(cardinality=5, rows=1_000_000), "postgresql")
        high = classify_query(query, self._keys(cardinality=200_000, rows=1_000_000), "postgresql")
        assert low.name == AD_HOC_FILTER
        assert high.name == KEY_SCOPED and "cardinality" not in high.reason

    def test_with_no_statistics_the_rule_holds_and_the_reason_says_so(self):
        query = {"query_id": "q", "query_text": "SELECT * FROM events WHERE kind = $1"}
        result = classify_query(query, self._keys(cardinality=None, rows=None), "postgresql")
        assert result.name == KEY_SCOPED
        assert "events.kind: an index's leading column, with no cardinality statistics" in (
            result.reason
        )

    def test_a_primary_or_foreign_key_needs_no_statistics(self):
        query = {"query_id": "q", "query_text": "SELECT * FROM order_items WHERE order_id = $1"}
        assert "cardinality" not in classify_query(query, KEYS, "postgresql").reason

    def _indexed_by(self, method):
        """``parts`` with one non-unique index on ``code``, built with ``method``."""
        table = _table(
            "parts",
            ["id"],
            ["id", "code"],
            indexes=[(("code",), False)],
            types={"code": "varchar(32)"},
        )
        table["indexes"][0]["index_type"] = method
        return SchemaKeys.from_collector({"database_schema": {"tables": [table]}})

    @pytest.mark.parametrize(
        "sql",
        [
            "SELECT * FROM parts WHERE code = $1",
            "SELECT * FROM parts WHERE code BETWEEN $1 AND $2",
            "SELECT * FROM parts ORDER BY code LIMIT 20",
        ],
    )
    def test_a_brin_index_makes_no_key(self, sql):
        """A BRIN index only summarizes ranges of table blocks: an equality on its column
        reads every row of each range whose summary may hold the value, so, like a GIN or
        GiST index, it addresses no entity, and it keeps no order, so a range or an ordered
        read over it is a filter, not a read by key. Collections before #293 typed it
        ``other``, which stays a key."""
        keys = self._indexed_by("brin")
        assert "code" not in keys.tables["shop.parts"].key_columns
        query = {"query_id": "q", "query_text": sql}
        assert classify_query(query, keys, "postgresql").name == AD_HOC_FILTER

    @pytest.mark.passes_on_main  # the BRIN change must not take these keys away
    @pytest.mark.parametrize("method", ["btree", "hash", "spgist", "other"])
    def test_an_index_that_serves_an_equality_keeps_its_key(self, method):
        """A hash index and SP-GiST's text, inet and range operator classes serve an
        equality on their column, so its leading column stays a key like a B-tree's;
        so does ``other``, which collections before #293 gave a BRIN index."""
        query = {"query_id": "q", "query_text": "SELECT * FROM parts WHERE code = $1"}
        assert classify_query(query, self._indexed_by(method), "postgresql").name == KEY_SCOPED


def test_no_bundled_sample_query_is_unparsed():
    """#986: ADR-031 Decision 14 Amendment 7 says no bundled-sample statement keeps the
    pre-switch rule for want of a parse. The committed routing snapshot records each
    query's class (``k``)."""
    import json
    from pathlib import Path

    baseline = Path(__file__).resolve().parents[3] / "e2e" / "baselines" / "routing_snapshot.json"
    samples = json.loads(baseline.read_text())["samples"]
    assert samples
    for name, sample in samples.items():
        classes = {row.get("k") for row in sample["queries"].values()}
        assert UNPARSED not in classes, name
        assert classes - {UTILITY, None}, name
