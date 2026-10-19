"""A design group never cuts an access pattern that fits in one (#1146).

The queries of one access pattern are designed together: they share the cluster of
the pattern's first query, sit next to each other, and neither the 20-query chunking
nor the input-size halving cuts a pattern that fits in one group. A pattern larger than
``MAX_GROUP_SIZE`` is still cut into chunks of that size.
"""

from __future__ import annotations

import pytest

from src.agents.schema_design.group_splitter import (
    MAX_GROUP_SIZE,
    build_groups,
    fit_groups_to_budget,
    query_pattern_index,
)

COLLECTOR = {"metadata": {"source_database": {"engine": "postgresql"}}}


def _q(qid: str, text: str, tables: list[str]) -> dict:
    return {"query_id": qid, "query_text": text, "tables_accessed": tables}


def _distinct(n: int, table: str = "users") -> list[dict]:
    return [
        _q(f"{table}{i}", f"SELECT a FROM {table} WHERE c{i} = $1", [f"db.{table}"])
        for i in range(n)
    ]


def _copies(n: int, start: int = 0) -> list[dict]:
    return [
        _q(
            f"p{i}",
            f"SELECT c{i} FROM users WHERE id IN ({', '.join(['$1'] * (i + 1))})",
            ["db.users"],
        )
        for i in range(start, start + n)
    ]


def _group_of(groups: list[dict]) -> dict[str, int]:
    return {q["query_id"]: n for n, g in enumerate(groups) for q in g["queries"]}


COPIES = _copies(3)
LAYOUTS = {
    # One copy first, the other two after 19 other queries.
    "apart": [COPIES[0], *_distinct(19), *COPIES[1:], *_distinct(25)[19:]],
    # All three together, at positions 19 to 21.
    "across_the_cut": [*_distinct(19), *COPIES, *_distinct(25)[19:]],
}


@pytest.mark.parametrize("layout", sorted(LAYOUTS))
def test_a_chunk_never_cuts_a_pattern_that_fits(layout: str) -> None:
    """A plain 20-query cut puts the copies in two groups in both layouts."""
    queries, copies = LAYOUTS[layout], COPIES
    groups = build_groups(queries, "db", COLLECTOR)
    where = _group_of(groups)
    assert len({where[q["query_id"]] for q in copies}) == 1
    assert max(len(g["queries"]) for g in groups) <= MAX_GROUP_SIZE
    assert sorted(where) == sorted(q["query_id"] for q in queries)


def test_a_pattern_stays_in_its_first_querys_cluster() -> None:
    first = _q("j1", "SELECT a.x FROM a JOIN b ON a.id = b.a_id WHERE b.k = $1", ["db.a", "db.b"])
    second = _q("j2", "SELECT b.y FROM b JOIN a ON a.id = b.a_id WHERE b.k = $2", ["db.b", "db.a"])
    groups = build_groups([first, *_distinct(6, "a"), *_distinct(6, "b"), second], "db", COLLECTOR)
    where = _group_of(groups)
    assert where["j1"] == where["j2"]


def test_halving_cuts_at_a_pattern_boundary() -> None:
    queries = [*_distinct(3), *_copies(4), *_distinct(5)[3:]]
    pattern_of = query_pattern_index(queries, COLLECTOR)
    group = {"group_name": "g", "primary_tables": ["db.users"], "queries": queries}

    def measure(g: dict) -> int:
        return 100 * len(g["queries"])

    parts = fit_groups_to_budget([group], measure, max_chars=800, pattern_of=pattern_of)
    where = _group_of(parts)
    assert len({where[f"p{i}"] for i in range(4)}) == 1
    assert len(parts) > 1
    assert sum((p["queries"] for p in parts), []) == queries
    assert all(measure(p) <= 800 for p in parts)


def test_a_pattern_larger_than_a_group_is_still_cut() -> None:
    copies = _copies(MAX_GROUP_SIZE + 5)
    groups = build_groups(copies, "db", COLLECTOR)
    assert [len(g["queries"]) for g in groups] == [MAX_GROUP_SIZE, 5]


def test_queries_of_one_pattern_each_keep_their_order() -> None:
    queries = _distinct(12)
    groups = build_groups(queries, "db", COLLECTOR)
    assert [q["query_id"] for g in groups for q in g["queries"]] == [q["query_id"] for q in queries]


def test_a_large_pattern_fills_the_open_chunk_and_leaves_no_tiny_group() -> None:
    """#1146 review: a pattern of more than 20 queries no longer flushes the open
    chunk before it, which left 1-query design groups."""
    queries = [*_distinct(3), *_copies(MAX_GROUP_SIZE + 5), *_distinct(7)[3:]]
    groups = build_groups(queries, "db", COLLECTOR)
    assert [len(g["queries"]) for g in groups] == [MAX_GROUP_SIZE, 12]


def test_keeping_patterns_whole_never_adds_a_group() -> None:
    """Two 11-query patterns fit in 2 plain chunks of 20 but not whole in 2: the plain
    cut is kept, so the designer calls stay what main makes."""
    first = [
        _q(
            f"a{i}",
            f"SELECT c{i} FROM users WHERE id IN ({', '.join(['$1'] * (i + 1))})",
            ["db.users"],
        )
        for i in range(11)
    ]
    second = [
        _q(
            f"b{i}",
            f"SELECT c{i} FROM users WHERE k IN ({', '.join(['$1'] * (i + 1))})",
            ["db.users"],
        )
        for i in range(11)
    ]
    third = [
        _q(
            f"c{i}",
            f"SELECT c{i} FROM users WHERE j IN ({', '.join(['$1'] * (i + 1))})",
            ["db.users"],
        )
        for i in range(11)
    ]
    groups = build_groups([*first, *second, *third], "db", COLLECTOR)
    assert [len(g["queries"]) for g in groups] == [20, 13]


def test_halving_cuts_at_the_middle_when_the_only_boundary_is_lopsided() -> None:
    """One query, then a 9-query pattern: a cut at the boundary would leave a 1-query
    design group, so the group is halved at its middle."""
    queries = [*_distinct(1), *_copies(9)]
    pattern_of = query_pattern_index(queries, COLLECTOR)
    group = {"group_name": "g", "primary_tables": ["db.users"], "queries": queries}

    def measure(g: dict) -> int:
        return 100 * len(g["queries"])

    parts = fit_groups_to_budget([group], measure, max_chars=900, pattern_of=pattern_of)
    assert [len(p["queries"]) for p in parts] == [5, 5]


def test_halving_never_takes_more_parts_than_the_middle_cut() -> None:
    """Cutting at the boundaries (3 | 4 | 3) would take 3 parts where the middle cut
    takes 2: the middle cut is kept."""
    queries = [*_distinct(3), *_copies(4), *_distinct(6)[3:]]
    pattern_of = query_pattern_index(queries, COLLECTOR)
    group = {"group_name": "g", "primary_tables": ["db.users"], "queries": queries}

    def measure(g: dict) -> int:
        return 100 * len(g["queries"])

    parts = fit_groups_to_budget([group], measure, max_chars=500, pattern_of=pattern_of)
    assert [len(p["queries"]) for p in parts] == [5, 5]


def test_a_later_member_joins_the_first_members_fk_cluster() -> None:
    """The first member's table is in one FK cluster and the later member's primary
    table in another: the pattern is designed in the first one's group."""
    collector = {
        **COLLECTOR,
        "database_schema": {
            "tables": [
                {"table_id": "db.a", "table_name": "a", "foreign_keys": []},
                {
                    "table_id": "db.b",
                    "table_name": "b",
                    "foreign_keys": [{"referenced_table": "db.c", "columns": ["c_id"]}],
                },
                {"table_id": "db.c", "table_name": "c", "foreign_keys": []},
            ]
        },
    }
    first = _q("j1", "SELECT a.x FROM a JOIN b ON a.id = b.a_id WHERE b.k = $1", ["db.a", "db.b"])
    second = _q("j2", "SELECT b.y FROM b JOIN a ON a.id = b.a_id WHERE b.k = $1", ["db.b", "db.a"])
    queries = [first, *_distinct(6, "a"), *_distinct(6, "c"), second]
    groups = build_groups(queries, "db", collector)
    where = _group_of(groups)
    assert where["j1"] == where["j2"] == where["a0"]
    assert where["c0"] != where["j1"]


def test_the_chunk_after_a_large_pattern_is_refilled() -> None:
    """14 queries, a 25-query pattern, a 3-query pattern and 8 more: the large pattern
    tops up the open chunk (20), its last 5 stay open for the next queries, and the
    3-query pattern is not cut."""
    small = [
        _q(f"s{i}", f"SELECT a FROM users WHERE k IN ({', '.join(['$1'] * (i + 1))})", ["db.users"])
        for i in range(3)
    ]
    queries = [*_distinct(14), *_copies(25), *small, *_distinct(22)[14:]]
    groups = build_groups(queries, "db", COLLECTOR)
    assert [len(g["queries"]) for g in groups] == [20, 19, 11]
    where = _group_of(groups)
    assert len({where[q["query_id"]] for q in small}) == 1
