# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The read/write split and placeholder tables in the context graph (#306).

``WRITES`` (Query to SourceTable) marks every table a query writes or locks, as
routing's own one-writer rule reads it (``write_groups.written_tables``, ADR-031
Amendment 9, in the collection's source dialect); ``locking`` marks a locking
read. READS_FROM stays on every table a query accesses. A SourceTable named by
a collector placeholder (``unknown``, a dummy table) is flagged ``placeholder``,
with the reason; it gets WRITES whenever routing counts it as written.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from src.agents.referee.table_resolution import SourceScope
from src.agents.referee.write_groups import written_tables
from src.graph.populators import PLACEHOLDER_REASON, populate_from_collector, rebuild_graph
from src.graph.schema import initialize_schema
from src.graph.store import GraphStore
from src.storage.local_store import LocalArtifactStore
from src.tools.sql.query_facts import source_engine_of

REAL_WORDPRESS = Path(__file__).parents[1] / "fixtures/real_runs/wordpress-both/job"


def _q(qid, text, tables, kind):
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": kind,
        "tables_accessed": list(tables),
        "calls_per_second": 1.0,
    }


def _collector(engine, *queries):
    return {
        "metadata": {"source_database": {"engine": engine}},
        "queries": {"query_patterns": list(queries)},
    }


MYSQL = _collector(
    "mysql",
    _q("ins", "INSERT INTO orders (total) VALUES (1)", ["orders"], "INSERT"),
    _q("sel", "SELECT * FROM orders WHERE id = 1", ["orders"], "SELECT"),
    _q(
        "lock",
        "SELECT * FROM orders o JOIN customers c ON o.customer_id = c.id WHERE o.id = 1 FOR UPDATE",
        ["orders", "customers"],
        "SELECT",
    ),
    _q(
        "upd",
        "UPDATE orders o JOIN customers c ON o.customer_id = c.id SET o.total = 0 WHERE c.vip = 1",
        ["orders", "customers"],
        "UPDATE",
    ),
    _q("set", "SET SESSION sql_mode = ''", ["unknown"], "OTHER"),
    _q("dual", "SELECT 1 FROM DUAL", ["DUAL"], "SELECT"),
)


def _writes(store):
    rows = store.query(
        "MATCH (q:Query)-[w:WRITES]->(st:SourceTable) "
        "RETURN q.id AS q, st.id AS t, w.locking AS locking"
    )
    return sorted((r["q"], r["t"], r["locking"]) for r in rows)


def _reads(store):
    rows = store.query(
        "MATCH (q:Query)-[:READS_FROM]->(st:SourceTable) RETURN q.id AS q, st.id AS t"
    )
    return sorted((r["q"], r["t"]) for r in rows)


def test_writes_locks_and_reads(graph_store):
    populate_from_collector(MYSQL, graph_store)
    assert _writes(graph_store) == [
        ("ins", "orders", False),
        # A locking read locks every table it reads.
        ("lock", "customers", True),
        ("lock", "orders", True),
        # A multi-table UPDATE writes its target, not the table it joins to read.
        ("upd", "orders", False),
    ]


def test_writes_agree_with_routing(graph_store):
    """The graph's WRITES are routing's own ``written_tables`` for every query."""
    populate_from_collector(MYSQL, graph_store)
    engine, scope = source_engine_of(MYSQL), SourceScope.from_collector(MYSQL)
    expected = sorted(
        (q["query_id"], t)
        for q in MYSQL["queries"]["query_patterns"]
        for t in written_tables(q, engine, scope)
    )
    assert [(q, t) for q, t, _ in _writes(graph_store)] == expected


def test_reads_from_stays_on_every_accessed_table(graph_store):
    """READS_FROM is every table a query accesses (an UPDATE reads the rows it
    changes, a locking read reads what it locks); 'reads only' is READS_FROM with
    no WRITES."""
    populate_from_collector(MYSQL, graph_store)
    assert _reads(graph_store) == sorted(
        (q["query_id"], t) for q in MYSQL["queries"]["query_patterns"] for t in q["tables_accessed"]
    )
    read_only = graph_store.query(
        "MATCH (q:Query)-[:READS_FROM]->(st:SourceTable) "
        "WHERE NOT (q)-[:WRITES]->(st) RETURN q.id AS q, st.id AS t"
    )
    assert sorted((r["q"], r["t"]) for r in read_only) == [
        ("dual", "DUAL"),
        ("sel", "orders"),
        ("set", "unknown"),
        ("upd", "customers"),
    ]


@pytest.mark.parametrize(
    "engine, text, tables",
    [
        ("sqlserver", "UPDATE [dbo].[Orders] SET [Total] = 0 WHERE [Id] = 1", ["dbo.Orders"]),
        ("postgresql", 'UPDATE "Orders" SET "Total" = 0 WHERE "Id" = 1', ["public.Orders"]),
        ("postgresql", 'DELETE FROM public."Orders" WHERE "Id" = 1', ["public.Orders"]),
    ],
)
def test_quoted_names_are_read(graph_store, engine, text, tables):
    """SQL Server brackets and PostgreSQL quoted names name the written table.
    (These read the same in the default dialect; the dialect cases are
    ``test_the_source_dialect_decides_a_locking_read``.)"""
    collector = _collector(engine, _q("w", text, tables, text.split()[0].upper()))
    populate_from_collector(collector, graph_store)
    assert _writes(graph_store) == [("w", tables[0], False)]


def _placeholders(store):
    rows = store.query(
        "MATCH (st:SourceTable) RETURN st.id AS id, st.placeholder AS p, "
        "st.placeholder_reason AS reason"
    )
    return {r["id"]: (r["p"], r["reason"]) for r in rows}


def test_placeholder_tables_are_kept_and_flagged(graph_store):
    populate_from_collector(MYSQL, graph_store)
    tables = _placeholders(graph_store)
    assert tables == {
        "orders": (False, None),
        "customers": (False, None),
        "unknown": (True, PLACEHOLDER_REASON),
        "DUAL": (True, PLACEHOLDER_REASON),
    }
    # Neither statement writes: SET and a SELECT from DUAL write no table.
    assert not [w for w in _writes(graph_store) if w[1] in ("unknown", "DUAL")]


@pytest.mark.parametrize("name", ["UNKNOWN", "DUAL", "dual", "None", "null", "SYSIBM.SYSDUMMY1"])
def test_a_placeholder_is_never_written(graph_store, name):
    """Routing's ``written_tables`` never writes a placeholder (#1817), and the graph
    copies routing: the placeholder is kept and flagged, with no WRITES edge."""
    collector = _collector("mysql", _q("w", "INSERT INTO x VALUES (1)", [name], "INSERT"))
    engine, scope = source_engine_of(collector), SourceScope.from_collector(collector)
    assert written_tables(collector["queries"]["query_patterns"][0], engine, scope) == []
    populate_from_collector(collector, graph_store)
    assert _writes(graph_store) == []
    assert _placeholders(graph_store)[name] == (True, PLACEHOLDER_REASON)


def test_the_lowercase_unknown_is_never_written(graph_store):
    collector = _collector("mysql", _q("w", "INSERT INTO x VALUES (1)", ["unknown"], "INSERT"))
    populate_from_collector(collector, graph_store)
    assert _writes(graph_store) == []


@pytest.mark.passes_on_main  # the graph already flagged an empty name; pinned with #1817
@pytest.mark.parametrize("name", ["", " "])
def test_an_empty_name_is_a_placeholder(graph_store, name):
    collector = _collector("mysql", _q("r", "SELECT 1", [name], "SELECT"))
    populate_from_collector(collector, graph_store)
    assert _placeholders(graph_store) == {name: (True, PLACEHOLDER_REASON)}


@pytest.mark.parametrize("name", ["Unknown", "NULL", "none", "sysibm.sysdummy1"])
def test_a_real_table_that_only_looks_like_a_placeholder_is_a_table(graph_store, name):
    """Placeholders match exactly, the same check routing uses (#1817): a real table
    named ``Unknown`` or ``NULL`` is a table, written like any other."""
    collector = _collector("mysql", _q("w", "INSERT INTO x VALUES (1)", [name], "INSERT"))
    populate_from_collector(collector, graph_store)
    assert _placeholders(graph_store) == {name: (False, None)}
    assert _writes(graph_store) == [("w", name, False)]


def test_a_locking_read_marks_only_the_tables_it_locks(graph_store):
    """``INSERT ... SELECT ... FOR UPDATE``: the insert target is a write, the table
    the read locks is locking."""
    collector = _collector(
        "postgresql",
        _q(
            "w",
            "INSERT INTO archive SELECT * FROM orders WHERE id = 1 FOR UPDATE",
            ["archive", "orders"],
            "INSERT",
        ),
    )
    populate_from_collector(collector, graph_store)
    assert _writes(graph_store) == [("w", "archive", False), ("w", "orders", True)]


def test_every_shared_placeholder_name_is_flagged(graph_store):
    from src.shared.report_facts import PLACEHOLDER_TABLE_NAMES

    names = sorted(PLACEHOLDER_TABLE_NAMES)
    collector = _collector(
        "mysql", *(_q(f"q{i}", "SELECT 1", [n], "SELECT") for i, n in enumerate(names))
    )
    populate_from_collector(collector, graph_store)
    flagged = {t for t, (p, reason) in _placeholders(graph_store).items() if p and reason}
    assert flagged == set(names)


def test_real_wordpress_run_agrees_with_routing(tmp_path):
    """The real WordPress run: WRITES equals routing's ``written_tables`` for every
    collected query, read in the collection's dialect; its placeholders are flagged."""
    base = tmp_path / "artifacts"
    shutil.copytree(REAL_WORDPRESS, base / "wordpress" / "job")
    store = GraphStore(str(tmp_path / "g.lbug"))
    initialize_schema(store)
    rebuild_graph("wordpress", "job", LocalArtifactStore(str(base)), store)
    collector = json.loads((REAL_WORDPRESS / "collector/output.json").read_text())
    engine, scope = source_engine_of(collector), SourceScope.from_collector(collector)
    expected = sorted(
        (q["query_id"], t)
        for q in collector["queries"]["query_patterns"]
        for t in written_tables(q, engine, scope)
    )
    found = _writes(store)
    assert [(q, t) for q, t, _ in found] == expected
    locking = sum(1 for _, _, lock in found if lock)
    placeholders = sorted(t for t, (p, _) in _placeholders(store).items() if p)
    # 23 (query, table) writes, no locking read, one placeholder ('unknown').
    assert (len(found), locking, placeholders) == (23, 0, ["unknown"])
    store.close()


def test_the_cookbook_read_only_query_runs(graph_store):
    cookbook = Path(__file__).parents[2] / "docs/guides/context-graph-query-cookbook.md"
    text = cookbook.read_text()
    start = text.index("`MATCH (q:Query)-[:READS_FROM]->(st:SourceTable) WHERE NOT (q)-[:WRITES]")
    cypher = text[start + 1 : text.index("`", start + 1)]
    populate_from_collector(MYSQL, graph_store)
    rows = graph_store.query(cypher)
    assert sorted((r["q.id"], r["st.id"]) for r in rows) == [
        ("dual", "DUAL"),
        ("sel", "orders"),
        ("set", "unknown"),
        ("upd", "customers"),
    ]


@pytest.mark.parametrize(
    "engine, text",
    [
        # A MySQL '#' comment and a PostgreSQL dollar-quoted string hide 'FOR UPDATE':
        # read in another dialect, the statement would look like a locking read.
        ("mysql", "SELECT * FROM orders WHERE id = 1 # FOR UPDATE"),
        ("postgresql", "SELECT $$ FOR UPDATE $$ AS note FROM orders WHERE id = 1"),
    ],
)
def test_the_source_dialect_decides_a_locking_read(graph_store, engine, text):
    collector = _collector(engine, _q("r", text, ["orders"], "SELECT"))
    populate_from_collector(collector, graph_store)
    assert _writes(graph_store) == []
    assert _reads(graph_store) == [("r", "orders")]
