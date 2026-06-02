"""
Property 8: Denormalization sub-type classification

For any collector output, detect_denormalization_subtypes shall classify each
denormalization opportunity into exactly one of: bounded-parent-child (FK exists,
child/parent row ratio <= 100), many-to-many-junction (exactly 2 FKs, PK =
composite of both FK columns), co-accessed-tables (co-access frequency > 50
calls/hour), or adjacency-list (self-referential FK or multi-level hierarchy
traversal). The output shall never contain the old denormalizable-relationship
pattern type.

Feature: enhanced-dynamodb-analysis, Property 8: Denormalization sub-type classification
Validates: Requirements 4.1, 4.2, 4.3, 4.4, 4.5
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from src.tools.analysis.dynamodb_analysis_tools import (
    detect_denormalization_subtypes,
    detect_relationships,
)

# ---------------------------------------------------------------------------
# Valid sub-types
# ---------------------------------------------------------------------------

VALID_SUBTYPES = frozenset(
    {"bounded-parent-child", "many-to-many-junction", "co-accessed-tables", "adjacency-list"}
)

# ---------------------------------------------------------------------------
# Hypothesis strategies
# ---------------------------------------------------------------------------

_column_name = st.from_regex(r"[a-z][a-z0-9_]{0,9}", fullmatch=True)
_table_id = st.from_regex(r"[a-z]{2,6}\.[a-z_]{2,10}", fullmatch=True)


def _parent_child_collector(
    ratio: st.SearchStrategy[int] = st.integers(min_value=1, max_value=100),
) -> st.SearchStrategy[dict]:
    """Generate a collector output with a parent-child FK relationship.

    The child:parent row ratio is controlled by the ratio strategy.
    """

    @st.composite
    def build(draw):
        parent_id = draw(_table_id)
        child_id = draw(_table_id.filter(lambda x: x != parent_id))
        parent_rows = draw(st.integers(min_value=1, max_value=100_000))
        r = draw(ratio)
        child_rows = parent_rows * r

        parent = {
            "table_id": parent_id,
            "table_name": parent_id.split(".")[-1],
            "row_count": parent_rows,
            "size_mb": 1.0,
            "columns": [
                {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
            ],
            "indexes": [
                {
                    "index_name": "PRIMARY",
                    "columns": ["id"],
                    "is_unique": True,
                    "is_primary": True,
                    "index_type": "btree",
                },
            ],
            "primary_key": ["id"],
        }
        child = {
            "table_id": child_id,
            "table_name": child_id.split(".")[-1],
            "row_count": child_rows,
            "size_mb": 1.0,
            "columns": [
                {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
                {
                    "column_name": "parent_id",
                    "ordinal_position": 2,
                    "data_type": "int",
                    "nullable": False,
                },
            ],
            "indexes": [
                {
                    "index_name": "PRIMARY",
                    "columns": ["id"],
                    "is_unique": True,
                    "is_primary": True,
                    "index_type": "btree",
                },
            ],
            "primary_key": ["id"],
            "foreign_keys": [
                {
                    "constraint_name": "fk_parent",
                    "columns": ["parent_id"],
                    "referenced_table": parent_id.split(".")[-1],
                    "referenced_columns": ["id"],
                },
            ],
        }
        return {
            "database_schema": {"tables": [parent, child]},
            "queries": {"query_patterns": []},
        }

    return build()


def _junction_table_collector() -> st.SearchStrategy[dict]:
    """Generate a collector output with a many-to-many junction table."""

    @st.composite
    def build(draw):
        table_a_id = draw(_table_id)
        table_b_id = draw(_table_id.filter(lambda x: x != table_a_id))
        junction_id = draw(_table_id.filter(lambda x: x not in (table_a_id, table_b_id)))

        table_a = {
            "table_id": table_a_id,
            "table_name": table_a_id.split(".")[-1],
            "row_count": draw(st.integers(min_value=100, max_value=100_000)),
            "size_mb": 1.0,
            "columns": [
                {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
            ],
            "indexes": [
                {
                    "index_name": "PRIMARY",
                    "columns": ["id"],
                    "is_unique": True,
                    "is_primary": True,
                    "index_type": "btree",
                },
            ],
            "primary_key": ["id"],
        }
        table_b = {
            "table_id": table_b_id,
            "table_name": table_b_id.split(".")[-1],
            "row_count": draw(st.integers(min_value=100, max_value=100_000)),
            "size_mb": 1.0,
            "columns": [
                {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
            ],
            "indexes": [
                {
                    "index_name": "PRIMARY",
                    "columns": ["id"],
                    "is_unique": True,
                    "is_primary": True,
                    "index_type": "btree",
                },
            ],
            "primary_key": ["id"],
        }
        junction = {
            "table_id": junction_id,
            "table_name": junction_id.split(".")[-1],
            "row_count": draw(st.integers(min_value=100, max_value=1_000_000)),
            "size_mb": 1.0,
            "columns": [
                {
                    "column_name": "a_id",
                    "ordinal_position": 1,
                    "data_type": "int",
                    "nullable": False,
                },
                {
                    "column_name": "b_id",
                    "ordinal_position": 2,
                    "data_type": "int",
                    "nullable": False,
                },
            ],
            "indexes": [
                {
                    "index_name": "PRIMARY",
                    "columns": ["a_id", "b_id"],
                    "is_unique": True,
                    "is_primary": True,
                    "index_type": "btree",
                },
            ],
            "primary_key": ["a_id", "b_id"],
            "foreign_keys": [
                {
                    "constraint_name": "fk_a",
                    "columns": ["a_id"],
                    "referenced_table": table_a_id.split(".")[-1],
                    "referenced_columns": ["id"],
                },
                {
                    "constraint_name": "fk_b",
                    "columns": ["b_id"],
                    "referenced_table": table_b_id.split(".")[-1],
                    "referenced_columns": ["id"],
                },
            ],
        }
        return {
            "database_schema": {"tables": [table_a, table_b, junction]},
            "queries": {"query_patterns": []},
        }

    return build()


def _co_accessed_collector(
    freq: st.SearchStrategy[float] = st.floats(min_value=51.0, max_value=10_000.0),
) -> st.SearchStrategy[dict]:
    """Generate a collector output with two co-accessed tables."""

    @st.composite
    def build(draw):
        table_a_id = draw(_table_id)
        table_b_id = draw(_table_id.filter(lambda x: x != table_a_id))
        frequency = draw(freq)

        table_a = {
            "table_id": table_a_id,
            "table_name": table_a_id.split(".")[-1],
            "row_count": 1000,
            "size_mb": 1.0,
            "columns": [
                {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
            ],
            "indexes": [
                {
                    "index_name": "PRIMARY",
                    "columns": ["id"],
                    "is_unique": True,
                    "is_primary": True,
                    "index_type": "btree",
                },
            ],
            "primary_key": ["id"],
        }
        table_b = {
            "table_id": table_b_id,
            "table_name": table_b_id.split(".")[-1],
            "row_count": 1000,
            "size_mb": 1.0,
            "columns": [
                {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
            ],
            "indexes": [
                {
                    "index_name": "PRIMARY",
                    "columns": ["id"],
                    "is_unique": True,
                    "is_primary": True,
                    "index_type": "btree",
                },
            ],
            "primary_key": ["id"],
        }
        query = {
            "query_id": "co-001",
            "query_text": f"SELECT * FROM {table_a_id.split('.')[-1]} a JOIN {table_b_id.split('.')[-1]} b ON a.id = b.id",  # nosec B608 — test fixture, not executed
            "query_type": "SELECT",
            "frequency_per_hour": frequency,
            "calls_per_second": frequency / 3600.0,
            "tables_accessed": [table_a_id, table_b_id],
            "rows_returned_avg": 10.0,
            "filter_columns": ["id"],
            "has_joins": True,
            "join_count": 1,
            "execution_time_ms_avg": 2.0,
        }
        return {
            "database_schema": {"tables": [table_a, table_b]},
            "queries": {"query_patterns": [query]},
        }

    return build()


def _self_ref_collector() -> st.SearchStrategy[dict]:
    """Generate a collector output with a self-referential FK (adjacency-list)."""

    @st.composite
    def build(draw):
        tid = draw(_table_id)
        tname = tid.split(".")[-1]

        table = {
            "table_id": tid,
            "table_name": tname,
            "row_count": draw(st.integers(min_value=10, max_value=100_000)),
            "size_mb": 1.0,
            "columns": [
                {"column_name": "id", "ordinal_position": 1, "data_type": "int", "nullable": False},
                {
                    "column_name": "parent_id",
                    "ordinal_position": 2,
                    "data_type": "int",
                    "nullable": True,
                },
            ],
            "indexes": [
                {
                    "index_name": "PRIMARY",
                    "columns": ["id"],
                    "is_unique": True,
                    "is_primary": True,
                    "index_type": "btree",
                },
            ],
            "primary_key": ["id"],
            "foreign_keys": [
                {
                    "constraint_name": "fk_self",
                    "columns": ["parent_id"],
                    "referenced_table": tname,
                    "referenced_columns": ["id"],
                },
            ],
        }
        return {
            "database_schema": {"tables": [table]},
            "queries": {"query_patterns": []},
        }

    return build()


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


class TestDenormSubtypeClassification:
    """Property 8: Denormalization sub-type classification."""

    @given(data=_parent_child_collector(ratio=st.integers(min_value=1, max_value=100)))
    @settings(max_examples=100)
    def test_bounded_parent_child_detected(self, data: dict):
        """FK with child:parent ratio <= 100 yields bounded-parent-child."""
        relationships = detect_relationships(data)
        result = detect_denormalization_subtypes(data, relationships)

        bounded = [o for o in result if o.subtype == "bounded-parent-child"]
        assert len(bounded) >= 1
        for opp in bounded:
            assert opp.cardinality_ratio is not None
            assert opp.cardinality_ratio <= 100.0
            assert len(opp.tables) == 2

    @given(data=_parent_child_collector(ratio=st.integers(min_value=101, max_value=500)))
    @settings(max_examples=100)
    def test_unbounded_parent_child_not_detected(self, data: dict):
        """FK with child:parent ratio > 100 does NOT yield bounded-parent-child."""
        relationships = detect_relationships(data)
        result = detect_denormalization_subtypes(data, relationships)

        bounded = [o for o in result if o.subtype == "bounded-parent-child"]
        assert len(bounded) == 0

    @given(data=_junction_table_collector())
    @settings(max_examples=100)
    def test_many_to_many_junction_detected(self, data: dict):
        """Junction table with 2 FKs and composite PK yields many-to-many-junction."""
        relationships = detect_relationships(data)
        result = detect_denormalization_subtypes(data, relationships)

        junctions = [o for o in result if o.subtype == "many-to-many-junction"]
        assert len(junctions) >= 1
        for opp in junctions:
            assert len(opp.tables) == 3  # junction + 2 referenced tables
            assert "junction_table" in opp.evidence

    @given(data=_co_accessed_collector(freq=st.floats(min_value=51.0, max_value=10_000.0)))
    @settings(max_examples=100)
    def test_co_accessed_tables_detected(self, data: dict):
        """Co-access frequency > 50 calls/hour yields co-accessed-tables."""
        relationships = detect_relationships(data)
        result = detect_denormalization_subtypes(data, relationships)

        co_accessed = [o for o in result if o.subtype == "co-accessed-tables"]
        assert len(co_accessed) >= 1
        for opp in co_accessed:
            assert opp.co_access_frequency is not None
            assert opp.co_access_frequency > 50.0
            assert len(opp.tables) == 2

    @given(data=_co_accessed_collector(freq=st.floats(min_value=0.1, max_value=50.0)))
    @settings(max_examples=100)
    def test_low_frequency_co_access_not_detected(self, data: dict):
        """Co-access frequency <= 50 calls/hour does NOT yield co-accessed-tables."""
        relationships = detect_relationships(data)
        result = detect_denormalization_subtypes(data, relationships)

        co_accessed = [o for o in result if o.subtype == "co-accessed-tables"]
        assert len(co_accessed) == 0

    @given(data=_self_ref_collector())
    @settings(max_examples=100)
    def test_adjacency_list_detected(self, data: dict):
        """Self-referential FK yields adjacency-list."""
        relationships = detect_relationships(data)
        result = detect_denormalization_subtypes(data, relationships)

        adj = [o for o in result if o.subtype == "adjacency-list"]
        assert len(adj) >= 1
        for opp in adj:
            assert opp.is_cyclic is True  # nosemgrep: is-function-without-parentheses
            assert "self-referential FK" in opp.evidence.get("reason", "")

    @given(
        data=st.one_of(
            _parent_child_collector(),
            _junction_table_collector(),
            _co_accessed_collector(),
            _self_ref_collector(),
        )
    )
    @settings(max_examples=200)
    def test_all_subtypes_are_valid(self, data: dict):
        """Every detected opportunity has a valid sub-type from the four allowed."""
        relationships = detect_relationships(data)
        result = detect_denormalization_subtypes(data, relationships)

        for opp in result:
            assert opp.subtype in VALID_SUBTYPES

    @given(
        data=st.one_of(
            _parent_child_collector(),
            _junction_table_collector(),
            _co_accessed_collector(),
            _self_ref_collector(),
        )
    )
    @settings(max_examples=200)
    def test_no_old_denormalizable_relationship_type(self, data: dict):
        """Output never contains the old denormalizable-relationship pattern type."""
        relationships = detect_relationships(data)
        result = detect_denormalization_subtypes(data, relationships)

        for opp in result:
            assert opp.subtype != "denormalizable-relationship"

    def test_empty_tables_returns_empty(self):
        """An input with no tables produces no opportunities."""
        data = {"database_schema": {"tables": []}, "queries": {"query_patterns": []}}
        result = detect_denormalization_subtypes(data, [])
        assert result == []

    def test_no_relationships_no_fk_subtypes(self):
        """Tables with no FKs and no co-access produce no opportunities."""
        data = {
            "database_schema": {
                "tables": [
                    {
                        "table_id": "app.a",
                        "table_name": "a",
                        "row_count": 100,
                        "size_mb": 1.0,
                        "columns": [
                            {
                                "column_name": "id",
                                "ordinal_position": 1,
                                "data_type": "int",
                                "nullable": False,
                            }
                        ],
                        "primary_key": ["id"],
                    },
                ]
            },
            "queries": {"query_patterns": []},
        }
        result = detect_denormalization_subtypes(data, [])
        assert result == []
