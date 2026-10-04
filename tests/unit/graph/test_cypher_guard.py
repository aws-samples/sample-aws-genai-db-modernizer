"""Tests for the read-only statement filter applied to caller-supplied Cypher."""

from __future__ import annotations

import time

import pytest

from src.graph.cypher_guard import (
    MAX_QUERY_LENGTH,
    DisallowedStatementError,
    bound_result_rows,
    validate_read_only_cypher,
)

ALLOWED = [
    "MATCH (q:Query) RETURN q.query_id",
    "MATCH (q:Query) RETURN q.query_id;",
    "match (q:Query) return q.query_id order by q.query_id limit 5",
    "OPTIONAL MATCH (q:Query)-[:ACCESSES]->(t:SourceTable) RETURN q, t",
    "MATCH (q:Query) WITH q, count(*) AS c WHERE c > 1 RETURN q.query_id, c",
    "UNWIND [1, 2, 3] AS x RETURN x",
    "UNWIND $ids AS id MATCH (q:Query {query_id: id}) RETURN q",
    "RETURN 1 AS one",
    "MATCH (a)--(b) RETURN count(*) AS c",
    "MATCH (q:Query) RETURN q.query_id ORDER BY q.calls_per_second DESC SKIP 2 LIMIT 10",
    "CALL show_tables() RETURN *",
    "CALL table_info('Query') RETURN *",
    # Keywords inside string literals, backtick identifiers and comments are inert.
    "MATCH (q:Query) WHERE q.query_text CONTAINS 'DELETE FROM orders' RETURN q",
    'MATCH (q:Query) WHERE q.query_text = "CREATE TABLE x; DROP TABLE y" RETURN q',
    "MATCH (q:Query) RETURN q.query_id AS `set`",
    "MATCH (q:Query) // CREATE (x:Foo)\nRETURN q",
    "MATCH (q:Query) /* SET q.x = 1; DELETE q */ RETURN q",
    "MATCH (q:Query) WHERE q.query_text = 'it\\'s DELETE' RETURN q",
    "MATCH (q:Query) WHERE q.query_text = 'http://example/x' RETURN q",
    # Property accesses and parameters that share a name with a keyword.
    "MATCH (n:Signal) RETURN n.set, $load",
    # Backtick-quoted keywords are plain identifiers.
    "MATCH (n:`CREATE`) RETURN n.`DELETE` AS `SET`",
    # Read subqueries.
    "MATCH (q:Query) WHERE EXISTS { MATCH (q)-[:READS_FROM]->(:SourceTable) } RETURN q",
    "MATCH (q:Query) RETURN COUNT { MATCH (q)-->() } AS n",
    "MATCH (q:Query) RETURN CASE WHEN q.x > 1 THEN 'a' ELSE 'b' END AS c",
    # Non-ASCII text inside literals is fine.
    "MATCH (q:Query) WHERE q.query_text CONTAINS 'caf\u00e9 \u200b' RETURN q",
    # Tabs and newlines are ordinary whitespace.
    "MATCH (q:Query)\n\tRETURN q",
]

DISALLOWED = [
    "CREATE (n:Foo {id: 'x'})",
    "MATCH (q:Query) SET q.x = 1",
    "MATCH (q:Query) DELETE q",
    "MATCH (q:Query) DETACH DELETE q",
    "MERGE (n:Foo {id: 'x'})",
    "MATCH (q:Query) REMOVE q.x",
    "DROP TABLE Query",
    "ALTER TABLE Query ADD x INT64",
    "COPY Query FROM 'x.csv'",
    "COPY (MATCH (q:Query) RETURN q) TO 'out.csv'",
    "LOAD FROM 'x.csv' RETURN *",
    "INSTALL json",
    "LOAD EXTENSION json",
    "ATTACH 'other.lbug' AS other",
    "USE other",
    "IMPORT DATABASE 'dir'",
    "EXPORT DATABASE 'dir'",
    "CHECKPOINT",
    "BEGIN TRANSACTION",
    "COPY (MATCH (q:Query) RETURN q.id) TO '/tmp/rows.parquet'",
    "EXPORT DATABASE '/tmp/dump' (format='csv')",
    "INSTALL httpfs",
    "UNINSTALL httpfs",
    # CALL is an allowlist: settings and other table functions stay rejected.
    "CALL threads=1",
    "CALL threads = 1",
    "CALL timeout=1000000",
    "CALL some_other_procedure() RETURN *",
    "CALL read_csv('x.csv') RETURN *",
    "CALL read_parquet('x.parquet') RETURN *",
    "CALL file_info('x') RETURN *",
    "CALL disk_size_info() RETURN *",
    "CALL storage_info('Query') RETURN *",
    "CALL current_setting('threads') RETURN *",
    "CALL show_functions() RETURN *",
    "CALL project_graph('g', ['Query'], []) RETURN *",
    "CALL `show_tables`() RETURN *",
    "CALL show_tables() RETURN * UNION CALL read_csv('x') RETURN *",
    "MATCH (q:Query) CALL read_csv('x') RETURN *",
    "CALL { MATCH (q:Query) RETURN q } RETURN q",
    # UNION cannot be bounded by a single trailing LIMIT.
    "MATCH (q:Query) RETURN q.id AS id UNION ALL MATCH (t:SourceTable) RETURN t.id AS id",
    # Mixed case.
    "MaTcH (q:Query) sEt q.x = 1",
    "mAtCh (q:Query) DeTaCh DeLeTe q",
    # Write clause after a read prefix.
    "MATCH (q:Query) WITH q CREATE (n:Foo {id: q.query_id})",
    "UNWIND [1] AS x MERGE (n:Foo {id: 'x'})",
    "MATCH (a)--(b) CREATE (c:Foo {id: 'x'})",
    # Multiple statements.
    "MATCH (q:Query) RETURN q; CREATE (n:Foo {id: 'x'})",
    "MATCH (q:Query) RETURN q; MATCH (t:SourceTable) RETURN t",
    # Literal / comment tricks that would hide a clause.
    "MATCH (q:Query) RETURN 'x' CREATE (n:Foo {id: 'y'})",
    "MATCH (q:Query) RETURN q /* unterminated CREATE (n:Foo)",
    "MATCH (q:Query) RETURN 'unterminated",
    # Unicode, zero-width and other unusual whitespace between keyword letters or
    # tokens is rejected outright rather than interpreted.
    "MATCH (q:Query)\u00a0SET q.x = 1",
    "MATCH (q:Query) S\u200bET q.x = 1",
    "MATCH (q:Query)\u2028RETURN q",
    "MATCH (q:Query)\u3000RETURN q",
    "MATCH (q:Query)\ufeffRETURN q",
    "MATCH (q:Query)\x0bRETURN q",
    "MATCH (q:Query)\x0cRETURN q",
    "MATCH (q:Query)\x00 SET q.x = 1",
    "MATCH (q:Query) \uff33\uff25\uff34 q.x = 1",
    # Nesting deeper than the engine handles safely.
    "RETURN " + "(" * 40 + "1" + ")" * 40,
    "RETURN " + "[" * 40 + "1" + "]" * 40,
    "RETURN " + "{a: " * 40 + "1" + "}" * 40,
    "RETURN " + "CASE WHEN true THEN " * 9 + "1" + " END" * 9,
    "MATCH (q:Query) WHERE " + "EXISTS { MATCH (q) WHERE " * 40 + "true" + " }" * 40 + " RETURN q",
    # Too long.
    "MATCH (q:Query) RETURN q" + " " * MAX_QUERY_LENGTH,
    # Not a read clause at all / empty.
    "",
    "   ",
    "// only a comment",
]


@pytest.mark.parametrize("cypher", ALLOWED)
def test_accepts_read_statements(cypher):
    validate_read_only_cypher(cypher)


@pytest.mark.parametrize("cypher", DISALLOWED)
def test_rejects_disallowed_statements(cypher):
    with pytest.raises(DisallowedStatementError):
        validate_read_only_cypher(cypher)


def test_error_message_names_the_keyword():
    with pytest.raises(DisallowedStatementError, match="SET"):
        validate_read_only_cypher("MATCH (q:Query) SET q.x = 1")


def test_cookbook_queries_are_accepted():
    """Every example query in the cookbook passes the filter."""
    import json
    import re
    from pathlib import Path

    doc = Path(__file__).parents[3] / "docs" / "guides" / "context-graph-query-cookbook.md"
    queries = []
    for block in re.findall(r"```[a-z]*\n(.*?)```", doc.read_text(), re.S):
        try:
            queries.append(json.loads(block)["cypher"])
        except (ValueError, KeyError, TypeError):
            continue
    assert len(queries) >= 10
    for cypher in queries:
        validate_read_only_cypher(cypher)


def test_long_input_is_rejected_quickly():
    """The filter is linear: worst-case inputs at and above the limit return fast."""
    near_limit = [
        "RETURN '" + "\\'" * (MAX_QUERY_LENGTH // 2 - 10) + "'",
        "RETURN " + "/* */" * (MAX_QUERY_LENGTH // 5 - 2),
        "RETURN " + "1+" * (MAX_QUERY_LENGTH // 2 - 10) + "1",
        "RETURN " + "(" * (MAX_QUERY_LENGTH - 20),
        "MATCH " + "CALL " * (MAX_QUERY_LENGTH // 5 - 2),
        "x" * 1_000_000,
    ]
    for cypher in near_limit:
        started = time.perf_counter()
        try:
            validate_read_only_cypher(cypher)
        except DisallowedStatementError:
            pass
        assert time.perf_counter() - started < 0.5


@pytest.mark.parametrize(
    ("cypher", "expected"),
    [
        ("MATCH (q) RETURN q", "MATCH (q) RETURN q LIMIT 1001"),
        ("MATCH (q) RETURN q;", "MATCH (q) RETURN q LIMIT 1001"),
        ("MATCH (q) RETURN q  // note", "MATCH (q) RETURN q LIMIT 1001"),
        ("MATCH (q) RETURN q /* LIMIT 5 */", "MATCH (q) RETURN q LIMIT 1001"),
        ("MATCH (q) RETURN q LIMIT 10", "MATCH (q) RETURN q LIMIT 10"),
        ("MATCH (q) RETURN q limit 1001", "MATCH (q) RETURN q limit 1001"),
        ("MATCH (q) RETURN q LIMIT 50000", "MATCH (q) RETURN q LIMIT 1001"),
        (
            "MATCH (q) RETURN q ORDER BY q.x SKIP 5",
            "MATCH (q) RETURN q ORDER BY q.x SKIP 5 LIMIT 1001",
        ),
        ("MATCH (q) WITH q LIMIT 5 RETURN q", "MATCH (q) WITH q LIMIT 5 RETURN q LIMIT 1001"),
        (
            "MATCH (q) WHERE q.t = 'LIMIT 3' RETURN q",
            "MATCH (q) WHERE q.t = 'LIMIT 3' RETURN q LIMIT 1001",
        ),
    ],
)
def test_bound_result_rows_sets_engine_limit(cypher, expected):
    assert bound_result_rows(cypher, 1_000) == expected


@pytest.mark.parametrize(
    "cypher",
    ["MATCH (q) RETURN q LIMIT $n", "MATCH (q) RETURN q LIMIT 1 + 2", "CREATE (n:Foo)"],
)
def test_bound_result_rows_rejects(cypher):
    with pytest.raises(DisallowedStatementError):
        bound_result_rows(cypher, 1_000)


@pytest.mark.parametrize(
    "cypher",
    [
        "UNWIND range(1, 1000) AS x RETURN x",
        "UNWIND range(0, 999) AS x RETURN x",
        "UNWIND range(1000, 1, -1) AS x RETURN x",
        "UNWIND range(1, 1000000, 1000) AS x RETURN x",
        "RETURN repeat('ab', 1000) AS r",
        "RETURN lpad('x', 10, '0') AS a, rpad(q.id, 20, ' ') AS b",
        "RETURN q.range, q.repeat",
    ],
)
def test_accepts_small_generated_values(cypher):
    validate_read_only_cypher(cypher)


@pytest.mark.parametrize(
    "cypher",
    [
        "UNWIND range(1, 1001) AS x RETURN x",
        "UNWIND RANGE(1, 200000000) AS x RETURN x",
        "UNWIND range(1, $n) AS x RETURN x",
        "UNWIND range(1, 10 * 1000) AS x RETURN x",
        "UNWIND range(1, size([1])) AS x RETURN x",
        "UNWIND range(1, 10, 0) AS x RETURN x",
        "UNWIND range (1, 5000) AS x RETURN x",
        "RETURN repeat('x', 1000000000) AS r",
        "RETURN repeat('x', $n) AS r",
        "RETURN lpad('x', 1000000000, 'y') AS r",
        "RETURN rpad('x', 1001, 'y') AS r",
        "RETURN `range`(1, 100000000) AS r",
        "RETURN `repeat`('x', 100000000) AS r",
    ],
)
def test_rejects_large_or_unbounded_generated_values(cypher):
    with pytest.raises(DisallowedStatementError):
        validate_read_only_cypher(cypher)


@pytest.mark.parametrize(
    ("cypher", "expected"),
    [
        ("RETURN 1 AS `a``b`", "RETURN 1 AS `a``b` LIMIT 1001"),
        ("RETURN 1 AS `a``b` LIMIT 5", "RETURN 1 AS `a``b` LIMIT 5"),
        ("RETURN 1 AS `a``LIMIT 5`", "RETURN 1 AS `a``LIMIT 5` LIMIT 1001"),
        ("RETURN 'LIMIT 5'", "RETURN 'LIMIT 5' LIMIT 1001"),
        ("RETURN 1 AS ````", "RETURN 1 AS ```` LIMIT 1001"),
    ],
)
def test_bound_result_rows_keeps_trailing_quoted_text(cypher, expected):
    assert bound_result_rows(cypher, 1_000) == expected
