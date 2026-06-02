"""Unit tests for the schema design group splitter."""

from src.agents.schema_design.group_splitter import (
    MAX_GROUP_SIZE,
    build_groups,
    get_primary_table,
    recommendations_for_tables,
    tables_for_queries,
)


def _make_query(qid: str, tables: list[str]) -> dict:
    return {"query_id": qid, "tables_accessed": tables}


class TestGetPrimaryTable:
    def test_returns_schema_qualified_table(self):
        q = _make_query("q1", ["db.users", "db.posts"])
        assert get_primary_table(q, "db") == "db.users"

    def test_returns_first_table_without_prefix(self):
        q = _make_query("q1", ["other.users"])
        assert get_primary_table(q, "db") == "other.users"

    def test_returns_unknown_for_no_tables(self):
        q = _make_query("q1", [])
        assert get_primary_table(q, "db") == "unknown"


class TestBuildGroups:
    def test_single_large_table_one_group(self):
        queries = [_make_query(f"q{i}", ["db.users"]) for i in range(10)]
        groups = build_groups(queries, "db")
        assert len(groups) == 1
        assert groups[0]["group_name"] == "users"
        assert len(groups[0]["queries"]) == 10

    def test_oversized_table_splits_into_chunks(self):
        queries = [_make_query(f"q{i}", ["db.users"]) for i in range(MAX_GROUP_SIZE + 5)]
        groups = build_groups(queries, "db")
        assert len(groups) == 2
        assert len(groups[0]["queries"]) == MAX_GROUP_SIZE
        assert len(groups[1]["queries"]) == 5

    def test_small_tables_batched_together(self):
        queries = []
        for i in range(4):
            queries.append(_make_query(f"q_a{i}", ["db.table_a"]))
        for i in range(3):
            queries.append(_make_query(f"q_b{i}", ["db.table_b"]))
        groups = build_groups(queries, "db")
        # Both < SMALL_GROUP_THRESHOLD, should be batched
        assert len(groups) == 1
        assert "misc_batch" in groups[0]["group_name"]
        assert len(groups[0]["queries"]) == 7

    def test_mix_of_large_and_small(self):
        queries = []
        # Large table
        for i in range(8):
            queries.append(_make_query(f"q_big{i}", ["db.big_table"]))
        # Small tables
        for i in range(2):
            queries.append(_make_query(f"q_sm{i}", ["db.small_table"]))
        groups = build_groups(queries, "db")
        assert len(groups) == 2
        group_names = {g["group_name"] for g in groups}
        assert "big_table" in group_names

    def test_empty_queries_returns_empty(self):
        assert build_groups([], "db") == []

    def test_small_batch_flushed_at_max_size(self):
        # Create enough small tables to exceed MAX_GROUP_SIZE
        queries = []
        for i in range(MAX_GROUP_SIZE + 3):
            queries.append(_make_query(f"q{i}", [f"db.table_{i}"]))
        groups = build_groups(queries, "db")
        # Should have at least 2 groups (one flushed at MAX, one remainder)
        assert len(groups) >= 2
        total = sum(len(g["queries"]) for g in groups)
        assert total == MAX_GROUP_SIZE + 3


class TestTablesForQueries:
    def test_filters_by_table_id(self):
        queries = [_make_query("q1", ["db.users", "db.posts"])]
        all_tables = [
            {"table_id": "db.users", "table_name": "users"},
            {"table_id": "db.posts", "table_name": "posts"},
            {"table_id": "db.orders", "table_name": "orders"},
        ]
        result = tables_for_queries(queries, all_tables)
        assert len(result) == 2
        names = {t["table_name"] for t in result}
        assert names == {"users", "posts"}


class TestRecommendationsForTables:
    def test_filters_by_table_id(self):
        analysis = {
            "table_recommendations": [
                {"table_id": "db.users", "confidence_score": 90},
                {"table_id": "db.orders", "confidence_score": 70},
            ]
        }
        result = recommendations_for_tables({"db.users"}, analysis)
        assert len(result) == 1
        assert result[0]["table_id"] == "db.users"


class TestAffinityGrouping:
    """Test that analysis signals cluster related tables into the same group."""

    def _make_collector(self, tables, fks=None):
        """Build minimal collector output with optional FKs."""
        table_dicts = []
        for t in tables:
            td = {"table_id": t, "table_name": t.split(".")[-1], "foreign_keys": []}
            if fks:
                for fk_from, fk_to in fks:
                    if fk_from == t:
                        td["foreign_keys"].append(
                            {"referenced_table": fk_to, "constraint_name": f"fk_{t}_{fk_to}"}
                        )
            table_dicts.append(td)
        return {"database_schema": {"tables": table_dicts}}

    def _make_analysis(self, aggregates=None, patterns=None):
        """Build minimal analysis output."""
        return {
            "aggregate_recommendations": aggregates or [],
            "workload_analysis": {"patterns_detected": patterns or []},
        }

    def test_fk_clusters_tables_together(self):
        """Tables linked by FK should end up in the same group."""
        queries = [
            _make_query("q1", ["db.orders"]),
            _make_query("q2", ["db.orders"]),
            _make_query("q3", ["db.orders"]),
            _make_query("q4", ["db.order_items"]),
            _make_query("q5", ["db.order_items"]),
            _make_query("q6", ["db.products"]),
        ]
        collector = self._make_collector(
            ["db.orders", "db.order_items", "db.products"],
            fks=[("db.order_items", "db.orders")],
        )
        analysis = self._make_analysis()

        groups = build_groups(queries, "db", collector, analysis)
        # orders + order_items should be in the same group (FK cluster)
        orders_group = None
        for g in groups:
            qids = {q["query_id"] for q in g["queries"]}
            if "q1" in qids:
                orders_group = g
                break
        assert orders_group is not None
        qids = {q["query_id"] for q in orders_group["queries"]}
        assert "q4" in qids or "q5" in qids, "FK-linked tables should be in same group"

    def test_aggregate_clusters_tables_together(self):
        """Tables in the same aggregate recommendation should be grouped together."""
        queries = [
            _make_query("q1", ["db.posts"]),
            _make_query("q2", ["db.posts"]),
            _make_query("q3", ["db.posts"]),
            _make_query("q4", ["db.postmeta"]),
            _make_query("q5", ["db.postmeta"]),
            _make_query("q6", ["db.comments"]),
        ]
        collector = self._make_collector(["db.posts", "db.postmeta", "db.comments"])
        analysis = self._make_analysis(
            aggregates=[
                {
                    "aggregate_id": "agg-posts",
                    "root_table": "db.posts",
                    "member_tables": ["db.posts", "db.postmeta"],
                }
            ]
        )

        groups = build_groups(queries, "db", collector, analysis)
        # posts + postmeta should be clustered
        posts_group = None
        for g in groups:
            qids = {q["query_id"] for q in g["queries"]}
            if "q1" in qids:
                posts_group = g
                break
        assert posts_group is not None
        qids = {q["query_id"] for q in posts_group["queries"]}
        assert "q4" in qids, "Aggregate-linked tables should be in same group"

    def test_co_access_pattern_clusters(self):
        """Co-accessed-tables pattern should cluster tables together."""
        queries = [
            _make_query("q1", ["db.users"]),
            _make_query("q2", ["db.users"]),
            _make_query("q3", ["db.users"]),
            _make_query("q4", ["db.usermeta"]),
            _make_query("q5", ["db.usermeta"]),
            _make_query("q6", ["db.sessions"]),
        ]
        collector = self._make_collector(["db.users", "db.usermeta", "db.sessions"])
        analysis = self._make_analysis(
            patterns=[
                {
                    "pattern_type": "bounded-parent-child",
                    "query_ids": ["q1", "q4"],
                    "table_ids": ["db.users", "db.usermeta"],
                }
            ]
        )

        groups = build_groups(queries, "db", collector, analysis)
        users_group = None
        for g in groups:
            qids = {q["query_id"] for q in g["queries"]}
            if "q1" in qids:
                users_group = g
                break
        assert users_group is not None
        qids = {q["query_id"] for q in users_group["queries"]}
        assert "q4" in qids, "Co-access pattern should cluster tables"

    def test_no_analysis_falls_back_to_primary_table(self):
        """Without analysis, grouping should still work (primary table only)."""
        queries = [
            _make_query("q1", ["db.users"]),
            _make_query("q2", ["db.posts"]),
        ]
        groups = build_groups(queries, "db")
        # No clustering, just primary table grouping
        assert len(groups) == 1  # both < threshold → misc batch
        assert len(groups[0]["queries"]) == 2

    def test_cluster_respects_max_group_size(self):
        """Even clustered tables should be split when exceeding MAX_GROUP_SIZE."""
        queries = [_make_query(f"q{i}", ["db.orders"]) for i in range(MAX_GROUP_SIZE + 5)]
        queries += [_make_query(f"qoi{i}", ["db.order_items"]) for i in range(3)]
        collector = self._make_collector(
            ["db.orders", "db.order_items"],
            fks=[("db.order_items", "db.orders")],
        )
        analysis = self._make_analysis()

        groups = build_groups(queries, "db", collector, analysis)
        total = sum(len(g["queries"]) for g in groups)
        assert total == MAX_GROUP_SIZE + 5 + 3
        # Should have been split into chunks
        assert all(len(g["queries"]) <= MAX_GROUP_SIZE for g in groups)
