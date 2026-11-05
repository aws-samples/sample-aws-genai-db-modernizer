# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""PostgreSQL foreign keys, read from a real server (#1868).

Both foreign key queries (``scripts/collect-postgresql.sql`` and the live collector's
``collect_foreign_keys``) run against a PostgreSQL started by ``pgserver`` on a Unix
socket in a temp folder (no TCP listener). Each foreign key must give n rows, one per
column pair, aligned and in constraint order, with the right referenced table, whatever
its shape. The tests skip when ``pgserver`` is not installed or cannot start.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

import pytest

from src.tools.database.offline_parser import parse_offline_collection
from src.tools.database.postgres_tools import PostgreSQLRemoteCollector

pgserver = pytest.importorskip("pgserver")

_ROOT = Path(__file__).resolve().parents[4]

_SCHEMA = """
CREATE SCHEMA other;

-- onto a primary key whose order differs from the foreign key's
CREATE TABLE pk_parent (a int, b int, c int, PRIMARY KEY (b, c, a));
CREATE TABLE fk_pk_child (x int, y int, z int,
  CONSTRAINT fk_pk FOREIGN KEY (x, y, z) REFERENCES pk_parent (c, a, b));

-- onto a unique constraint
CREATE TABLE uq_parent (u1 int, u2 int, u3 int, UNIQUE (u3, u1, u2));
CREATE TABLE fk_uq_child (c1 int, c2 int, c3 int,
  CONSTRAINT fk_uq FOREIGN KEY (c3, c1, c2) REFERENCES uq_parent (u2, u3, u1));

-- onto a unique index with no constraint
CREATE TABLE ui_parent (u1 int, u2 int, u3 int);
CREATE UNIQUE INDEX ui_parent_idx ON ui_parent (u2, u3, u1);
CREATE TABLE fk_ui_child (c1 int, c2 int, c3 int,
  CONSTRAINT fk_ui FOREIGN KEY (c2, c3, c1) REFERENCES ui_parent (u3, u1, u2));

-- across schemas, both ways
CREATE TABLE other.xs_parent (p int, q int, PRIMARY KEY (q, p));
CREATE TABLE xs_child (p2 int, q2 int,
  CONSTRAINT fk_xs FOREIGN KEY (q2, p2) REFERENCES other.xs_parent (p, q));

-- self-referencing
CREATE TABLE tree (a int, b int, pa int, pb int, PRIMARY KEY (a, b),
  CONSTRAINT fk_tree FOREIGN KEY (pb, pa) REFERENCES tree (b, a));

-- the same foreign key name on two tables
CREATE TABLE dup_parent_a (a1 int, a2 int, PRIMARY KEY (a1, a2));
CREATE TABLE dup_parent_b (b1 int PRIMARY KEY);
CREATE TABLE other.xs_child2 (m int, n int,
  CONSTRAINT fk_xs2 FOREIGN KEY (n, m) REFERENCES public.dup_parent_a (a2, a1));
CREATE TABLE dup_child_a (d1 int, d2 int,
  CONSTRAINT fk_dup FOREIGN KEY (d2, d1) REFERENCES dup_parent_a (a1, a2));
CREATE TABLE dup_child_b (d1 int,
  CONSTRAINT fk_dup FOREIGN KEY (d1) REFERENCES dup_parent_b (b1));

-- a partitioned table with two partitions carrying the foreign key
CREATE TABLE part_ref (tenant int, region int, PRIMARY KEY (tenant, region));
CREATE TABLE part_child (tenant int, region int, v int)
  PARTITION BY LIST (tenant);
ALTER TABLE part_child ADD CONSTRAINT fk_part
  FOREIGN KEY (region, tenant) REFERENCES part_ref (region, tenant);
CREATE TABLE part_child_1 PARTITION OF part_child FOR VALUES IN (1);
CREATE TABLE part_child_2 PARTITION OF part_child FOR VALUES IN (2);

-- a foreign key pointing at a partitioned table
CREATE TABLE part_target (k1 int, k2 int, PRIMARY KEY (k1, k2))
  PARTITION BY LIST (k1);
CREATE TABLE part_target_1 PARTITION OF part_target FOR VALUES IN (1);
CREATE TABLE part_target_2 PARTITION OF part_target FOR VALUES IN (2);
CREATE TABLE refs_part (r1 int, r2 int,
  CONSTRAINT fk_refs_part FOREIGN KEY (r2, r1) REFERENCES part_target (k2, k1));

-- the role the script header asks for: SELECT only, owns nothing
CREATE ROLE ro_collector;
GRANT USAGE ON SCHEMA public, other TO ro_collector;
GRANT SELECT ON ALL TABLES IN SCHEMA public, other TO ro_collector;
"""

# (table, constraint) -> (referenced table, [(column, referenced column), ...] in order)
_EXPECTED = {
    ("fk_pk_child", "fk_pk"): ("pk_parent", [("x", "c"), ("y", "a"), ("z", "b")]),
    ("fk_uq_child", "fk_uq"): ("uq_parent", [("c3", "u2"), ("c1", "u3"), ("c2", "u1")]),
    ("fk_ui_child", "fk_ui"): ("ui_parent", [("c2", "u3"), ("c3", "u1"), ("c1", "u2")]),
    ("xs_child", "fk_xs"): ("xs_parent", [("q2", "p"), ("p2", "q")]),
    ("xs_child2", "fk_xs2"): ("dup_parent_a", [("n", "a2"), ("m", "a1")]),
    ("tree", "fk_tree"): ("tree", [("pb", "b"), ("pa", "a")]),
    ("dup_child_a", "fk_dup"): ("dup_parent_a", [("d2", "a1"), ("d1", "a2")]),
    ("dup_child_b", "fk_dup"): ("dup_parent_b", [("d1", "b1")]),
    ("part_child", "fk_part"): ("part_ref", [("region", "region"), ("tenant", "tenant")]),
    ("refs_part", "fk_refs_part"): ("part_target", [("r2", "k2"), ("r1", "k1")]),
}
# The live collector reads one table of the public schema at a time.
_PUBLIC = {k: v for k, v in _EXPECTED.items() if k[0] != "xs_child2"}


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    try:
        srv = pgserver.get_server(tmp_path_factory.mktemp("pgdata"), cleanup_mode="delete")
    except Exception as exc:  # no bundled server for this platform, or it cannot start
        pytest.skip(f"pgserver cannot start a PostgreSQL here: {exc}")
    srv.psql("\\set ON_ERROR_STOP on\n" + _SCHEMA)  # a bad schema fails, never skips
    yield srv
    srv.cleanup()


def _run(server, sql: str, role: str | None) -> list[dict]:
    set_role = f"SET ROLE {role};\n" if role else ""
    wrapped = f"SELECT coalesce(jsonb_agg(to_jsonb(t)), '[]') FROM ({sql}) t;"
    out = server.psql(f"\\set QUIET on\n\\t on\n\\a\n{set_role}{wrapped}\n")
    rows: list[dict] = json.loads([ln for ln in out.splitlines() if ln.strip()][-1])
    return rows


def _offline_rows(server, role) -> list[dict]:
    script = (_ROOT / "scripts/collect-postgresql.sql").read_text()
    cte = script.split("foreign_keys_data AS (", 1)[1].split("\n),\n", 1)[0]
    sql = (
        f"WITH foreign_keys_data AS ({cte}\n) "
        "SELECT jsonb_array_elements(data::jsonb) AS fk FROM foreign_keys_data"
    )
    return [r["fk"] for r in _run(server, sql, role)]


@pytest.mark.parametrize("role", [None, "ro_collector"], ids=["owner", "select-only"])
def test_the_offline_query_gives_one_aligned_row_per_column_pair(server, role):
    groups = defaultdict(list)
    for r in _offline_rows(server, role):
        groups[(r["table_name"], r["constraint_name"])].append(r)

    assert set(groups) == set(_EXPECTED)
    for key, (parent, pairs) in _EXPECTED.items():
        rows = groups[key]
        assert {r["referenced_table_name"] for r in rows} == {parent}, key
        assert [(r["column_name"], r["referenced_column_name"]) for r in rows] == pairs, key
        assert [r["ordinal_position"] for r in rows] == list(range(1, len(pairs) + 1)), key


@pytest.mark.parametrize("role", [None, "ro_collector"], ids=["owner", "select-only"])
def test_the_live_collector_gives_the_same_foreign_keys(server, role):
    class Live(PostgreSQLRemoteCollector):
        def _query(self, sql):
            return _run(server, sql.strip().rstrip(";"), role)

    collector = Live.__new__(Live)
    got = {
        (table, fk["constraint_name"]): (
            fk["referenced_table"],
            list(zip(fk["columns"], fk["referenced_columns"], strict=True)),
        )
        for table in sorted({t for t, _ in _PUBLIC})
        for fk in collector.collect_foreign_keys(table)
    }

    assert got == _PUBLIC


def test_the_parser_keeps_the_pairs_from_the_real_rows_when_shuffled(server):
    rows = _offline_rows(server, None)
    data = {
        "metadata": {"database_name": "db", "engine": "postgresql"},
        "tables": [
            {"table_name": t, "row_count": 1} for t in sorted({r["table_name"] for r in rows})
        ],
        "columns": [],
        "indexes": [],
        "primary_keys": [],
        "foreign_keys": rows[::-1],
    }

    parsed = {t["table_name"]: t for t in parse_offline_collection(data)["tables"]}

    for (table, name), (parent, pairs) in _EXPECTED.items():
        [fk] = [f for f in parsed[table]["foreign_keys"] if f["constraint_name"] == name]
        assert fk["referenced_table"] == parent
        assert list(zip(fk["columns"], fk["referenced_columns"], strict=True)) == pairs
