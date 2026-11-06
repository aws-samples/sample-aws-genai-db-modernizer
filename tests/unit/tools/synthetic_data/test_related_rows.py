# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synthetic rows for tables that load together (#1942): foreign-key order, cycles,
unique keys, null rates, value ranges and rows a real engine accepts."""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from src.tools.synthetic_data.generator import (
    edge_values,
    generate_related_rows,
    generate_rows,
)
from tests.hypothesis_examples import scaled


def _col(name, data_type, nullable=False, **kw):
    return {"column_name": name, "data_type": data_type, "nullable": nullable, **kw}


def _fk(name, cols, table, refs):
    return {
        "constraint_name": name,
        "columns": cols,
        "referenced_table": table,
        "referenced_columns": refs,
    }


def _table(tid, columns, pk=("id",), fks=(), row_count=50, **kw):
    return {
        "table_id": tid,
        "table_name": tid.rpartition(".")[2],
        "row_count": row_count,
        "primary_key": list(pk),
        "columns": columns,
        "foreign_keys": list(fks),
        **kw,
    }


CUSTOMERS = _table(
    "s.customers", [_col("id", "integer"), _col("name", "varchar(20)", max_length=20)]
)
ORDERS = _table(
    "s.orders",
    [_col("id", "bigint"), _col("customer_id", "integer"), _col("note", "text", True)],
    fks=[_fk("orders_customer", ["customer_id"], "customers", ["id"])],
)
LINES = _table(
    "s.lines",
    [_col("order_id", "bigint"), _col("customer_id", "integer"), _col("qty", "int")],
    pk=("order_id", "customer_id"),
    fks=[
        _fk("l_order", ["order_id"], "orders", ["id"]),
        _fk("l_customer", ["customer_id"], "s.customers", ["id"]),
    ],
)


def _keys(rows, cols):
    return {tuple(r[c] for c in cols) for r in rows}


def test_parents_come_before_children_and_every_reference_resolves():
    out = generate_related_rows([LINES, ORDERS, CUSTOMERS], dialect="postgresql", seed=3)
    assert out.order == ("s.customers", "s.orders", "s.lines")
    customers = _keys(out.tables["s.customers"].rows, ["id"])
    orders = out.tables["s.orders"].rows
    assert orders and all((r["customer_id"],) in customers for r in orders)
    lines = out.tables["s.lines"].rows
    assert lines and all((r["order_id"],) in _keys(orders, ["id"]) for r in lines)
    assert all((r["customer_id"],) in customers for r in lines)
    # a key made only of foreign keys stays unique
    assert len(_keys(lines, ["order_id", "customer_id"])) == len(lines)
    assert out.cycle_breaks == () and out.unseeded == {}


def test_row_counts_are_capped_and_follow_a_small_collection_count():
    big = dict(CUSTOMERS, row_count=10**9)
    out = generate_related_rows([big], dialect="mysql", max_rows=7)
    edges = sum(len(edge_values(c, dialect="mysql", loadable=True)) for c in big["columns"])
    assert len(out.tables["s.customers"].rows) <= max(7, edges)
    small = generate_related_rows([dict(CUSTOMERS, row_count=2)], dialect="mysql", max_rows=7)
    assert len(small.tables["s.customers"].rows) == 2


def test_a_cycle_is_broken_at_its_nullable_foreign_key():
    a = _table(
        "s.a",
        [_col("id", "int"), _col("b_id", "int", True)],
        fks=[_fk("a_b", ["b_id"], "b", ["id"])],
    )
    b = _table(
        "s.b", [_col("id", "int"), _col("a_id", "int")], fks=[_fk("b_a", ["a_id"], "a", ["id"])]
    )
    out = generate_related_rows([b, a], dialect="postgresql")
    assert out.order == ("s.a", "s.b")
    assert out.cycle_breaks == (("s.a", "a_b"),)
    assert all(r["b_id"] is None for r in out.tables["s.a"].rows)
    a_ids = _keys(out.tables["s.a"].rows, ["id"])
    assert out.tables["s.b"].rows and all((r["a_id"],) in a_ids for r in out.tables["s.b"].rows)


def test_a_cycle_with_no_nullable_foreign_key_leaves_its_tables_unseeded_with_why():
    a = _table(
        "s.a", [_col("id", "int"), _col("b_id", "int")], fks=[_fk("a_b", ["b_id"], "b", ["id"])]
    )
    b = _table(
        "s.b", [_col("id", "int"), _col("a_id", "int")], fks=[_fk("b_a", ["a_id"], "a", ["id"])]
    )
    c = _table(
        "s.c",
        [_col("id", "int"), _col("b_id", "int", True)],
        fks=[_fk("c_b", ["b_id"], "b", ["id"])],
    )
    out = generate_related_rows([a, b, c], dialect="postgresql")
    assert "NOT NULL" in out.unseeded["s.a"]
    assert out.unseeded["s.b"] == "its parent s.a has no rows"
    assert out.tables["s.a"].rows == [] and out.tables["s.b"].rows == []
    # a nullable reference to an unseeded table is NULL, and the table still gets rows
    assert out.tables["s.c"].rows and all(r["b_id"] is None for r in out.tables["s.c"].rows)


def test_a_self_reference_points_at_an_earlier_row():
    tree = _table(
        "s.node",
        [_col("id", "int"), _col("parent_id", "int", True)],
        fks=[_fk("node_parent", ["parent_id"], "node", ["id"])],
    )
    rows = generate_related_rows([tree], dialect="mysql").tables["s.node"].rows
    assert rows[0]["parent_id"] is None
    seen = set()
    for r in rows:
        assert r["parent_id"] is None or r["parent_id"] in seen
        seen.add(r["id"])
    strict = dict(tree, columns=[_col("id", "int"), _col("parent_id", "int")])
    rows = generate_related_rows([strict], dialect="mysql").tables["s.node"].rows
    assert rows[0]["parent_id"] == rows[0]["id"]


def test_reserved_rows_are_held_back_and_the_last_rows_are_unreferenced():
    few = dict(CUSTOMERS, row_count=3)  # fewer customers than orders: every usable one is used
    out = generate_related_rows([few, ORDERS], dialect="postgresql", reserve={"s.customers": 2})
    customers = out.tables["s.customers"].rows
    held = out.held_back["s.customers"]
    assert len(held) == 2
    assert not _keys(held, ["id"]) & _keys(customers, ["id"])
    referenced = {r["customer_id"] for r in out.tables["s.orders"].rows}
    assert not {r["id"] for r in customers[-2:]} & referenced
    assert not {r["id"] for r in held} & referenced


def test_unique_indexes_stay_unique():
    t = _table(
        "s.u",
        [
            _col("id", "int"),
            _col("code", "varchar(3)", max_length=3),
            _col("a", "int"),
            _col("b", "int"),
        ],
        row_count=40,
        indexes=[
            {"index_name": "u_code", "columns": ["code"], "is_unique": True},
            {"index_name": "u_ab", "columns": ["a", "b"], "is_unique": True},
        ],
    )
    rows = generate_related_rows([t], dialect="mysql").tables["s.u"].rows
    assert len({r["code"] for r in rows}) == len(rows)
    assert len(_keys(rows, ["a", "b"])) == len(rows)


def test_a_unique_foreign_key_keeps_one_row_per_parent():
    """A one-to-one table (a unique foreign key) gets at most one row per parent row."""
    profile = _table(
        "s.profile",
        [_col("id", "int"), _col("customer_id", "integer")],
        row_count=40,
        fks=[_fk("p_customer", ["customer_id"], "customers", ["id"])],
        indexes=[{"index_name": "p_one", "columns": ["customer_id"], "is_unique": True}],
    )
    out = generate_related_rows([dict(CUSTOMERS, row_count=3), profile], dialect="mysql")
    rows = out.tables["s.profile"].rows
    assert 0 < len(rows) <= len(out.tables["s.customers"].rows)
    assert len({r["customer_id"] for r in rows}) == len(rows)


def test_a_reference_outside_the_set_is_ignored_and_listed():
    out = generate_related_rows([ORDERS], dialect="postgresql")
    assert out.ignored_references == (("s.orders", "orders_customer"),)
    assert out.tables["s.orders"].rows


def test_skip_and_an_unmodelled_not_null_column_leave_a_table_unseeded():
    odd = _table("s.odd", [_col("id", "int"), _col("shape", "geometry")])
    out = generate_related_rows([odd, CUSTOMERS], dialect="postgresql", skip={"s.customers": "x"})
    assert out.unseeded == {"s.customers": "x", "s.odd": out.unseeded["s.odd"]}
    assert "shape" in out.unseeded["s.odd"]
    nullable = _table("s.odd", [_col("id", "int"), _col("shape", "geometry", True)])
    rows = generate_related_rows([nullable], dialect="postgresql").tables["s.odd"].rows
    assert rows and all(r["shape"] is None for r in rows)


def test_the_same_seed_gives_the_same_rows_and_another_seed_others():
    def run(seed):
        return generate_related_rows([LINES, ORDERS, CUSTOMERS], dialect="mysql", seed=seed)

    assert run(5) == run(5)
    assert run(5).tables["s.customers"].rows != run(6).tables["s.customers"].rows


# ------------------------------------------------------------------ single-table extensions


def test_null_rate_sets_the_share_of_nulls():
    t = _table("s.t", [_col("id", "int"), _col("x", "int", True, null_rate=0.5)], row_count=10)
    rows = generate_rows(t, dialect="mysql", max_rows=10).rows
    assert sum(r["x"] is None for r in rows) == 5
    never = _table("s.t", [_col("id", "int"), _col("x", "int", True, null_rate=0)], row_count=10)
    assert all(r["x"] is not None for r in generate_rows(never, dialect="mysql").rows)
    always = _table("s.t", [_col("id", "int"), _col("x", "int", True, null_rate=1)], row_count=4)
    assert all(r["x"] is None for r in generate_rows(always, dialect="mysql").rows)


def test_a_value_range_bounds_random_values_and_adds_both_bounds_as_edges():
    c = _col("qty", "int", min_value=10, max_value=20)
    assert edge_values(c, dialect="postgresql") == [10, 20]
    rows = generate_rows(
        _table("s.t", [_col("id", "int"), c], row_count=30), dialect="postgresql"
    ).rows
    assert all(10 <= r["qty"] <= 20 for r in rows)
    assert {10, 20} <= {r["qty"] for r in rows}
    d = _col("price", "decimal(6,2)", min_value="1.5", max_value=3)
    rows = generate_rows(_table("s.t", [_col("id", "int"), d], row_count=30), dialect="mysql").rows
    assert all(Decimal("1.5") <= r["price"] <= Decimal("9999.99") for r in rows)
    assert all(r["price"] <= 3 for r in rows if r["price"] != Decimal("9999.99"))
    # a range on a text column, or a range that is not a number, is ignored
    assert edge_values(_col("s", "text", min_value=1, max_value=2), dialect="mysql") == []
    assert edge_values(_col("q", "int", min_value="x", max_value=None), dialect="mysql") == []


@pytest.mark.parametrize(
    ("column", "dialect", "default", "loadable"),
    [
        (_col("d", "date"), "mysql", ["0000-00-00"], []),
        (_col("d", "datetime"), "mysql", ["0000-00-00 00:00:00"], []),
        (_col("s", "varchar(3)", max_length=3), "mysql", ["\U0001d11e" * 3], ["zzz"]),
        (_col("d", "date"), "postgresql", ["infinity", "-infinity"], ["infinity", "-infinity"]),
    ],
)
def test_loadable_edges_are_ones_the_engine_takes_by_default(column, dialect, default, loadable):
    assert edge_values(column, dialect=dialect) == default
    assert edge_values(column, dialect=dialect, loadable=True) == loadable


@pytest.mark.parametrize("data_type", ["bit(1)", "boolean"])
def test_loadable_mysql_bits_and_booleans_are_integers(data_type):
    t = _table("s.t", [_col("id", "int"), _col("flag", data_type)], row_count=5)
    rows = generate_rows(t, dialect="mysql", loadable=True).rows
    assert {type(r["flag"]) for r in rows} == {int}
    assert {type(r["flag"]) for r in generate_rows(t, dialect="mysql").rows} == {str}


@pytest.mark.passes_on_main
def test_a_table_without_the_new_fields_gets_the_rows_it_always_got():
    """The shape check's rows (ADR-033) do not move: no new field, no new argument."""
    t = _table("s.t", [_col("id", "int"), _col("x", "int", True), _col("d", "datetime")])
    rows = generate_rows(t, dialect="mysql", seed=2).rows
    assert rows[0]["d"] == "0000-00-00 00:00:00"
    assert [i for i, r in enumerate(rows) if r["x"] is None][:2] == [4, 9]


# ------------------------------------------------------------------ property


@st.composite
def _graphs(draw):
    n = draw(st.integers(1, 5))
    tables = []
    for i in range(n):
        cols = [_col("id", "int")]
        fks = []
        for j in range(draw(st.integers(0, 2))):
            parent = draw(st.integers(0, n - 1))
            name = f"fk{i}_{j}"
            cols.append(_col(f"r{j}", "int", draw(st.booleans())))
            fks.append(_fk(name, [f"r{j}"], f"t{parent}", ["id"]))
        tables.append(_table(f"s.t{i}", cols, fks=fks, row_count=draw(st.integers(0, 30))))
    return tables


@settings(max_examples=scaled(150), deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(tables=_graphs(), seed=st.integers(0, 3), data=st.data())
def test_seeded_rows_always_load_in_order(tables, seed, data):
    """For any graph of tables (cycles and self references included) and any reserve:
    the order puts each parent first unless the key is a cycle break, every non-NULL
    reference resolves to an earlier-inserted row, keys are unique, a NOT NULL column is
    never NULL, and the reserved rows (held back, and as many inserted ones for a test
    DELETE) are pointed at by no inserted or held-back row."""
    reserve = {
        t["table_id"]: n for t in tables if (n := data.draw(st.integers(0, 4), label=t["table_id"]))
    }
    out = generate_related_rows(tables, dialect="postgresql", seed=seed, reserve=reserve)
    assert sorted(out.order) == sorted(t["table_id"] for t in tables)
    pos = {t: i for i, t in enumerate(out.order)}
    by_id = {t["table_id"]: t for t in tables}
    pointed: dict[str, set] = {t: set() for t in by_id}
    for tid in out.order:
        rows = out.tables[tid].rows
        held = out.held_back.get(tid, [])
        assert len({r["id"] for r in rows + held}) == len(rows) + len(held)
        assert len(held) <= reserve.get(tid, 0)
        for c in by_id[tid]["columns"]:
            if not c["nullable"]:
                assert all(r[c["column_name"]] is not None for r in rows + held)
        for fk in by_id[tid]["foreign_keys"]:
            parent = "s." + fk["referenced_table"]
            col = fk["columns"][0]
            pointed[parent].update(r[col] for r in rows + held if r[col] is not None)
            if (tid, fk["constraint_name"]) in out.cycle_breaks:
                assert all(r[col] is None for r in rows + held)
                continue
            if parent == tid:
                inserted: set = set()
                for r in rows:
                    assert r[col] is None or r[col] in inserted | {r["id"]}
                    inserted.add(r["id"])
                assert all(r[col] is None or r[col] in inserted for r in held)
                continue
            assert pos[parent] < pos[tid]
            ids = {r["id"] for r in out.tables[parent].rows}
            assert all(r[col] is None or r[col] in ids for r in rows + held)
    for tid, held in out.held_back.items():
        reserved = held + out.tables[tid].rows[len(out.tables[tid].rows) - len(held) :]
        assert not {r["id"] for r in reserved} & pointed[tid]
