# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Synthetic rows from collector metadata, with edge values (#1763)."""

from __future__ import annotations

from decimal import Decimal

from src.tools.synthetic_data.generator import DEFAULT_ROWS, edge_values, generate_rows


def _col(name, data_type, normalized, nullable=False, **kw):
    return {"column_name": name, "data_type": data_type, "normalized_data_type": normalized,
            "nullable": nullable, **kw}  # fmt: skip


POSTS = {"table_id": "wordpress.wp_posts", "row_count": 11990, "primary_key": ["ID"], "columns": [
    _col("ID", "bigint unsigned", "integer"),
    _col("post_date", "datetime", "datetime"),
    _col("post_title", "varchar(20)", "string", max_length=20),
    _col("menu_order", "tinyint(1)", "integer", nullable=True),
    _col("price", "decimal(5,2)", "decimal", numeric_precision=5, numeric_scale=2, nullable=True),
]}  # fmt: skip


def test_the_same_seed_gives_the_same_rows():
    assert (
        generate_rows(POSTS, dialect="mysql", seed=7).rows
        == generate_rows(POSTS, dialect="mysql", seed=7).rows
    )
    assert (
        generate_rows(POSTS, dialect="mysql", seed=7).rows
        != generate_rows(POSTS, dialect="mysql", seed=8).rows
    )


def test_edge_values_per_kind():
    assert edge_values(POSTS["columns"][1], dialect="mysql") == ["0000-00-00 00:00:00"]
    assert edge_values(POSTS["columns"][0], dialect="mysql") == [18446744073709551615]
    assert edge_values(POSTS["columns"][3], dialect="mysql") == [2]
    assert edge_values(POSTS["columns"][4], dialect="mysql") == [Decimal("999.99")]
    assert edge_values(POSTS["columns"][2], dialect="mysql") == ["𝄞" * 20]
    pg = _col("created", "timestamp without time zone", "datetime")
    assert edge_values(pg, dialect="postgresql") == ["infinity", "-infinity"]


def test_every_edge_value_appears():
    t = generate_rows(POSTS, dialect="mysql", seed=1)
    titles = [r["post_title"] for r in t.rows]
    assert "𝄞" * 20 in titles and t.edge_rows >= 1
    assert any(r["post_date"] == "0000-00-00 00:00:00" for r in t.rows)


def test_nullability_is_respected():
    t = generate_rows(POSTS, dialect="mysql", seed=1)
    assert any(r["menu_order"] is None for r in t.rows)
    assert all(r["post_title"] is not None for r in t.rows)


def test_primary_key_is_unique_including_edge_rows():
    composite = dict(POSTS, primary_key=["ID", "post_title"])
    t = generate_rows(composite, dialect="mysql", seed=3)
    keys = [(r["ID"], r["post_title"]) for r in t.rows]
    assert len(keys) == len(set(keys))


def test_unknown_row_count_uses_the_default():
    t = generate_rows(dict(POSTS, row_count=None), dialect="mysql", seed=1)
    assert t.row_count_basis == "default" and len(t.rows) == DEFAULT_ROWS
    small = generate_rows(dict(POSTS, row_count=3), dialect="mysql", seed=1)
    assert small.row_count_basis == "collector" and len(small.rows) >= small.edge_rows


def _table(cols, pk, row_count=20):
    return {"table_id": "t.t", "row_count": row_count, "primary_key": pk, "columns": cols}


def test_keys_have_the_column_kind_and_are_unique():
    cols = [
        _col("a", "tinyint(1)", "integer"),
        _col("d", "date", "date"),
        _col("m", "decimal(2,2)", "decimal", numeric_precision=2, numeric_scale=2),
        _col("s", "varchar(3)", "string", max_length=3),
    ]
    for key in (["a"], ["d"], ["m"], ["s"], ["a", "s"]):
        t = generate_rows(_table(cols, key), dialect="mysql", seed=1)
        keys = [tuple(r[k] for k in key) for r in t.rows]
        assert len(keys) == len(set(keys)), key
    rows = generate_rows(_table(cols, ["a", "d", "m", "s"]), dialect="mysql", seed=1).rows
    assert all(isinstance(r["a"], int) and -128 <= r["a"] <= 127 for r in rows)
    assert all(len(r["s"]) <= 3 for r in rows)
    assert all(
        r["d"] == "0000-00-00" or len(r["d"]) == 10 and r["d"].startswith("2024-") for r in rows
    )
    assert all(isinstance(r["m"], Decimal) and r["m"] < 1 for r in rows)


def test_int_key_skips_its_edge_value():
    cols = [_col("a", "tinyint(1)", "integer")]
    rows = generate_rows(_table(cols, ["a"]), dialect="mysql", seed=1).rows
    assert [r["a"] for r in rows].count(2) == 1
    assert len({r["a"] for r in rows}) == len(rows)


def test_strings_never_exceed_max_length():
    cols = [
        _col("s", "varchar(3)", "string", max_length=3),
        _col("k", "varchar(3)", "string", max_length=3),
    ]
    t = generate_rows(_table(cols, ["k"]), dialect="mysql", seed=1)
    assert all(len(r["s"]) <= 3 and len(r["k"]) <= 3 for r in t.rows)


def test_long_text_edge_value_is_capped():
    col = _col("body", "longtext", "string", max_length=4294967295)
    assert edge_values(col, dialect="mysql") == ["𝄞" * 16384]


def test_random_integers_stay_in_range():
    cols = [
        _col("a", "tinyint", "integer"),
        _col("b", "smallint", "integer"),
        _col("c", "tinyint unsigned", "integer"),
    ]
    rows = generate_rows(_table(cols, []), dialect="mysql", seed=2).rows
    assert all(-128 <= r["a"] <= 127 and -32768 <= r["b"] <= 32767 for r in rows)
    assert all(0 <= r["c"] <= 255 for r in rows)


def test_every_nullable_non_key_column_has_a_null():
    cols = [_col("id", "int", "integer"), _col("n", "int", "integer", nullable=True)]
    t = generate_rows(_table(cols, ["id"], row_count=3), dialect="mysql", seed=1)
    assert any(r["n"] is None for r in t.rows)
    t = generate_rows(dict(POSTS, row_count=5), dialect="mysql", seed=1)
    assert any(r["price"] is None for r in t.rows) and any(r["menu_order"] is None for r in t.rows)
    pg = _table([_col("ts", "timestamp", "datetime", nullable=True)], [], row_count=1)
    t = generate_rows(pg, dialect="postgresql", seed=1)
    assert any(r["ts"] is None for r in t.rows) and t.rows[0]["ts"] == "infinity"


def test_random_decimals_fit_their_precision():
    cols = [_col("m", "decimal(2,2)", "decimal", numeric_precision=2, numeric_scale=2)]
    rows = generate_rows(_table(cols, []), dialect="mysql", seed=4).rows
    assert all(Decimal(0) <= r["m"] < 1 for r in rows if r["m"] != Decimal("0.99"))
    assert all(r["m"] < 1 for r in rows)


def _values(data_type, normalized="string", dialect="postgresql", **kw):
    t = generate_rows(_table([_col("c", data_type, normalized, **kw)], []), dialect=dialect, seed=1)
    return t, [r["c"] for r in t.rows]


def test_boolean_columns_get_boolean_values():
    t, v = _values("boolean")
    assert set(v) <= {"true", "false"} and len(set(v)) == 2
    assert t.unchecked_source_columns == ()


def test_time_uuid_json_year_and_bit_columns_get_valid_values():
    import json
    import re
    import uuid

    _, v = _values("time without time zone")
    assert all(re.fullmatch(r"\d\d:\d\d:\d\d", x) for x in v)
    _, v = _values("uuid")
    assert all(str(uuid.UUID(x)) == x for x in v)
    _, v2 = _values("uuid")
    assert v == v2 and len(set(v)) > 1
    for kind in ("json", "jsonb"):
        _, v = _values(kind)
        assert all(isinstance(json.loads(x), dict) for x in v)
    t, v = _values("year", dialect="mysql")
    assert all(1901 <= int(x) <= 2155 for x in v)
    t, v = _values("bit(1)")
    assert set(v) <= {"0", "1"}
    assert t.unchecked_source_columns == ()


def test_keys_of_the_new_kinds_are_unique():
    for dt in ("uuid", "time", "jsonb", "year"):
        t = generate_rows(_table([_col("c", dt, "string")], ["c"]), dialect="mysql", seed=1)
        keys = [r["c"] for r in t.rows]
        assert len(keys) == len(set(keys)), dt
    t = generate_rows(_table([_col("c", "boolean", "string")], ["c"]), dialect="postgresql", seed=1)
    assert len({r["c"] for r in t.rows}) == len(t.rows)


def test_unrecognised_kinds_are_marked_unchecked():
    cols = [_col("g", "geometry", "other"), _col("s", "varchar(3)", "string", max_length=3)]
    t = generate_rows(_table(cols, []), dialect="mysql", seed=1)
    assert t.unchecked_source_columns == ("g",)
    assert all(r["g"] is not None for r in t.rows)


def test_decimal_precision_is_parsed_from_data_type_when_the_fields_are_absent():
    col = _col("avg", "decimal(3,2)", "decimal")
    assert edge_values(col, dialect="mysql") == [Decimal("9.99")]
    assert edge_values(_col("n", "numeric(10)", "decimal"), dialect="postgresql") == [
        Decimal("9" * 10)
    ]
    rows = generate_rows(_table([col], []), dialect="mysql", seed=2).rows
    assert all(Decimal(0) <= r["avg"] <= Decimal("9.99") for r in rows)


def test_mysql_text_families_are_modelled_even_with_a_text_normalized_type():
    for name in ("tinytext", "text", "mediumtext", "longtext"):
        col = _col("t", name, "text", max_length=4294967295)
        t = generate_rows(_table([col], []), dialect="mysql", seed=1)
        assert t.unchecked_source_columns == (), name
        assert all(isinstance(r["t"], str) for r in t.rows)
