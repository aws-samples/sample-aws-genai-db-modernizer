"""Tests for the read-only statement filter applied to caller-supplied Cypher."""

from __future__ import annotations

import pytest

from src.graph.cypher_guard import DisallowedStatementError, validate_read_only_cypher

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
    "CALL threads=1",
    "CALL some_other_procedure() RETURN *",
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
