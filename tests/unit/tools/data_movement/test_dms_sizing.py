# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1's replication instance, LOB settings and Aurora line, from the collector (#1798)."""

from __future__ import annotations

import pytest

from src.contracts.data_movement_plan import LobTable
from src.shared.catalog.dms_sizing import dms_sizing
from src.tools.data_movement.dms_sizing import (
    database_gb,
    fails_on_lob_truncation,
    lob_plan,
    size_from_collector,
    size_instance,
    target_engine_line,
    task_lob_mode,
    unbounded_varchar,
    write_rate,
)


def _collector(
    size_gb=0.5,
    writes=(("INSERT", 2.0), ("UPDATE", 1.5), ("SELECT", 40.0)),
    version="8.0.45",
    engine="mysql",
    columns=None,
):
    cols = columns or [{"column_name": "id", "data_type": "bigint"}]
    patterns = [
        {"query_id": f"q{i}", "query_type": t, "calls_per_second": r}
        for i, (t, r) in enumerate(writes)
    ]
    return {
        "metadata": {
            "source_database": {"engine": engine, "version": version, "database_size_gb": size_gb}
        },
        "database_schema": {
            "tables": [{"table_id": "shop.orders", "table_name": "orders", "columns": cols}]
        },
        "queries": {"query_patterns": patterns},
    }


def test_write_rate_sums_only_the_write_statements():
    assert write_rate(_collector()) == 3.5
    assert write_rate(_collector(writes=(("SELECT", 9.0),))) == 0.0


def test_a_collection_without_rates_has_no_write_rate():
    assert write_rate(_collector(writes=(("INSERT", None), ("SELECT", 3.0)))) is None
    assert write_rate(_collector(writes=())) is None


def test_database_size_falls_back_to_the_tables_sizes():
    c = _collector(size_gb=None)
    assert database_gb(c) is None
    c["database_schema"]["tables"][0]["size_mb"] = 2048
    assert database_gb(c) == 2.0


@pytest.mark.parametrize(
    ("gb", "wps", "cls"),
    [
        (10, 50, "dms.t3.medium"),
        (10.001, 1, "dms.r6i.large"),
        (1, 50.01, "dms.r6i.large"),
        (500, 1000, "dms.r6i.large"),
        (2000, 5000, "dms.r6i.xlarge"),
        (2000.5, 1, "dms.r6i.2xlarge"),
        (1, 5001, "dms.r6i.2xlarge"),
    ],
)
def test_row_boundaries_are_inclusive(gb, wps, cls):
    assert size_instance(gb, wps).instance_class == cls


def test_each_row_carries_its_reason_and_storage():
    for row in dms_sizing().rows:
        gb = row.max_database_gb or 9999
        wps = row.max_writes_per_second or 9999
        s = size_instance(gb, wps)
        assert (s.row, s.allocated_storage_gb) == (row.id, row.allocated_storage_gb)
        assert s.included_storage_gb == row.included_storage_gb
        assert row.why in s.reason and s.database_gb == gb and s.writes_per_second == wps


def test_the_largest_row_says_to_size_it_with_the_customer():
    assert "customer" in (size_instance(5000, 1).note or "")


@pytest.mark.parametrize(("gb", "wps", "missing"), [(None, 1.0, "database size"), (1.0, None, "write rate"),
                                                    (None, None, "database size or write rate")])  # fmt: skip
def test_unknown_size_or_rate_falls_back_to_the_smallest(gb, wps, missing):
    s = size_instance(gb, wps)
    assert s.instance_class == dms_sizing().rows[0].instance_class
    assert f"The collection has no {missing}" in s.reason and "InstanceClass" in s.reason


def _lob_cols(*cols):
    return [{"column_name": n, "data_type": t, "max_length": m} for n, t, m in cols]


def _plan(columns, dialect="mysql", **kw):
    c = _collector(engine=dialect, columns=_lob_cols(*columns), **kw)
    return lob_plan(c, ["shop.orders"], dialect)


def test_a_bounded_lob_table_runs_in_limited_mode_at_its_size():
    # 10,000 characters x 4 bytes = 40,000 bytes = 40 KB (DMS's limit is in bytes).
    size, tables = _plan([("id", "bigint", None), ("body", "mediumtext", 10000)])
    assert size == 40 and [(t.mode, t.columns, t.max_size_kb) for t in tables] == [
        ("limited", ["body"], 40)
    ]
    assert "4 bytes per character" in tables[0].reason and "40 KB" in tables[0].reason


def test_each_lob_table_says_why_it_is_full():
    unbounded = _plan([("b", "longtext", None)])[1][0].reason
    assert "no declared maximum" in unbounded and "full LOB mode" in unbounded
    over = _plan([("b", "longtext", 4294967295)])[1][0].reason
    assert "1024 KB" in over and "full LOB mode" in over


def test_a_character_lob_counts_four_bytes_per_character_a_binary_one_one():
    # 300,000 characters is 1,171 KB of bytes: over the 1,024 KB limit. 300,000 bytes is 293 KB.
    assert _plan([("b", "longtext", 300000)])[1][0].mode == "full"
    size, tables = _plan([("b", "longblob", 300000)])
    assert (tables[0].mode, size) == ("limited", 293)
    assert (
        _plan([("b", "longtext", 262144)])[1][0].max_size_kb == 1024
    )  # exactly the limit stays limited
    assert _plan([("b", "longtext", 262145)])[1][0].mode == "full"


def test_types_dms_does_not_read_as_a_lob_are_not_listed():
    for t in ("text", "tinytext", "blob", "tinyblob"):
        assert _plan([("b", t, 65535)])[1] == [], t


def test_one_unbounded_column_makes_the_whole_table_full():
    _, tables = _plan([("a", "mediumtext", 100), ("b", "longtext", None)])
    assert (
        tables[0].mode == "full"
        and tables[0].columns == ["a", "b"]
        and tables[0].max_size_kb is None
    )


@pytest.mark.passes_on_main  # MySQL keeps full LOB mode (#1918 changes PostgreSQL only)
@pytest.mark.parametrize("dialect", ["mysql", "mariadb"])
def test_a_large_or_unknown_mysql_lob_runs_in_full_mode(dialect):
    for size in (4294967295, None):
        c = _collector(engine=dialect, columns=_lob_cols(("body", "longtext", size)))
        size_kb, tables = lob_plan(c, ["shop.orders"], dialect)
        assert (tables[0].mode, tables[0].max_size_kb) == ("full", None)
        assert size_kb == dms_sizing().lob.default_max_kb and "full LOB mode" in tables[0].reason


def test_a_postgresql_lob_of_unknown_size_runs_limited_at_the_catalogs_size():
    # #1918: full LOB mode wrote a NULL LOB as an empty value on Aurora PostgreSQL.
    lob = dms_sizing().lob
    size, tables = _plan([("id", "int4", None), ("body", "text", None)], dialect="postgresql")
    assert size == lob.undeclared_max_kb
    assert [(t.mode, t.columns, t.max_size_kb) for t in tables] == [
        ("limited", ["body"], lob.undeclared_max_kb)
    ]
    why = tables[0].reason
    assert "no declared maximum" in why and f"limited LOB mode at {lob.undeclared_max_kb} KB" in why
    # A live full load truncated a larger value despite FailTasksOnLobTruncation: the reason
    # says the size is not measured and a larger value is truncated, never that it stops the task.
    assert "did not measure" in why and "is truncated" in why and "stops the task" not in why
    assert "max(octet_length(<column>::text))" in why and "2 bytes per byte" in why
    assert "full LOB mode moves" not in why


@pytest.mark.passes_on_main  # a declared size within the limit was already limited mode
def test_a_postgresql_table_with_known_small_lobs_keeps_its_own_size():
    # A bounded LOB (a bytea of a known maximum) stays at its size: the rule only replaces
    # full LOB mode.
    size, tables = _plan([("b", "bytea", 2048)], dialect="postgresql")
    assert (size, tables[0].mode, tables[0].max_size_kb) == (2, "limited", 2)


def test_a_postgresql_lob_above_the_catalogs_size_runs_at_its_declared_size_up_to_dms_maximum():
    lob = dms_sizing().lob
    over = lob.undeclared_max_kb * 2 * 1024  # bytes, a bytea
    _, tables = _plan([("b", "bytea", over)], dialect="postgresql")
    assert (tables[0].mode, tables[0].max_size_kb) == ("limited", lob.undeclared_max_kb * 2)
    _, tables = _plan([("b", "bytea", 10**12)], dialect="postgresql")
    assert (tables[0].mode, tables[0].max_size_kb) == ("limited", 102400)


@pytest.mark.parametrize(
    "data_type",
    ["_int4", "_varchar", "ARRAY", "integer[]", "composite", "USER-DEFINED", "hstore", "jsonb"],
)
def test_postgresql_arrays_composites_and_unclassified_types_are_lobs_of_unknown_size(data_type):
    _, tables = _plan([("v", data_type, None)], dialect="postgresql", version="16.4")
    assert [(t.mode, t.columns, t.max_size_kb) for t in tables] == [
        ("limited", ["v"], dms_sizing().lob.undeclared_max_kb)
    ]


def test_a_named_postgresql_type_runs_its_table_as_a_lob_of_unknown_size_scalars_do_not():
    assert _plan([("a", "int4", None), ("b", "varchar", 40), ("c", "timestamp without time zone", None)],
                 dialect="postgresql")[1] == []  # fmt: skip
    for udt in ("my_enum", "halfvec", "some_composite"):
        _, tables = _plan([("a", "int4", None), ("c", udt, None)], dialect="postgresql")
        assert [(t.mode, t.columns) for t in tables] == [("limited", ["c"])]
        assert "c has a type not known to map to a non-LOB DMS type" in tables[0].reason


def test_the_task_lob_mode_is_full_only_off_postgresql():
    lob = dms_sizing().lob
    full = [LobTable(table="s.a", mode="full", columns=["x"])]
    limited = [LobTable(table="s.b", mode="limited", columns=["y"], max_size_kb=40)]
    for dialect in ("mysql", "mariadb", None):
        assert task_lob_mode(dialect, full + limited, 40) == (True, 40)
        assert task_lob_mode(dialect, limited, 40) == (False, 40)
        assert not fails_on_lob_truncation(dialect)
    # A plan written before #1918 lists a PostgreSQL table in full mode: it runs limited.
    assert task_lob_mode("postgresql", full + limited, 40) == (False, lob.undeclared_max_kb)
    assert task_lob_mode("postgresql", limited, 40) == (False, 40)
    assert task_lob_mode("postgresql", [], 32) == (False, 32)
    assert fails_on_lob_truncation("postgresql")


@pytest.mark.parametrize("data_type", ["double precision", "character varying", "time with time zone",
                                       "numeric(10,2)", "int4range", "bit varying", "money", "oid"])  # fmt: skip
def test_postgresql_long_spellings_of_scalars_are_not_lobs(data_type):
    assert _plan([("a", data_type, 10)], dialect="postgresql")[1] == []


def test_mysql_data_types_are_not_judged_by_the_scalar_list():
    assert (
        _plan([("a", "enum('a','b')", None), ("b", "varchar(10)", 10), ("c", "geometry_x", None)])[
            1
        ]
        == []
    )


def test_the_first_word_is_the_base_type():
    _, tables = _plan([("a", "longtext character set utf8mb4", 100), ("b", "LONGBLOB(3)", 100)])
    assert tables[0].columns == ["a", "b"] and tables[0].mode == "limited"


def test_mysql_spatial_types_count_as_binary():
    # 300,000 bytes is 293 KB; four bytes per character would put it over the 1,024 KB limit.
    size, tables = _plan([("g", "geometry", 300000)])
    assert (tables[0].mode, size) == ("limited", 293)


def test_a_zero_length_lob_is_one_kb():
    size, tables = _plan([("b", "mediumtext", 0)])
    assert (tables[0].mode, tables[0].max_size_kb, size) == ("limited", 1, 1)


def test_an_unknown_table_is_refused():
    with pytest.raises(ValueError, match="shop.nope"):
        lob_plan(_collector(), ["shop.nope"], "mysql")
    with pytest.raises(ValueError, match="shop.nope"):
        unbounded_varchar(_collector(), ["shop.nope"], "postgresql")


def _pg(*cols):
    return _collector(engine="postgresql", version="16.4", columns=_lob_cols(*cols))


def test_unbounded_postgresql_varchars_raise_the_endpoint_limit_and_are_named():
    c = _pg(("a", "character varying", None), ("b", "varchar", None), ("c", "bpchar", None),
            ("d", "character varying", 40), ("e", "varchar(20)", None), ("f", "text", None))  # fmt: skip
    u = unbounded_varchar(c, ["shop.orders"], "postgresql")
    assert u.max_bytes == 10485760 and u.columns == 2
    assert u.column_names == ["shop.orders.a", "shop.orders.b"]
    assert "2 " in u.reason and "8000 bytes" in u.reason and "UnboundedVarcharMaxSize" in u.reason
    assert "shop.orders.a" in u.reason and "shop.orders.b" in u.reason
    assert (
        "up to 10485760 bytes (10 MB) move whole" in u.reason
        and "larger values are truncated" in u.reason
    )
    assert "no value is truncated" not in u.reason


def test_the_reason_names_the_first_few_columns_and_counts_the_rest():
    c = _pg(*[(f"c{i}", "varchar", None) for i in range(8)])
    u = unbounded_varchar(c, ["shop.orders"], "postgresql")
    assert u.columns == 8 and len(u.column_names) == 8
    assert (
        "shop.orders.c0" in u.reason and "shop.orders.c4" in u.reason and "and 3 more" in u.reason
    )
    assert "shop.orders.c5" not in u.reason


def test_an_unbounded_bpchar_is_unsupported_not_covered_by_the_setting():
    u = unbounded_varchar(
        _pg(("c", "bpchar", None), ("d", "bpchar", 5)), ["shop.orders"], "postgresql"
    )
    assert u.max_bytes is None and u.columns == 0
    assert u.unsupported == ["shop.orders.c"]
    assert (
        "shop.orders.c" in u.reason
        and "bpchar" in u.reason
        and "UnboundedVarcharMaxSize" in u.reason
    )


def test_bounded_varchars_and_other_dialects_leave_the_limit_alone():
    c = _pg(("d", "character varying", 40), ("e", "int4", None))
    u = unbounded_varchar(c, ["shop.orders"], "postgresql")
    assert (u.max_bytes, u.columns) == (None, 0)
    mysql = _collector(columns=_lob_cols(("a", "varchar", None)))
    assert unbounded_varchar(mysql, ["shop.orders"], "mysql").max_bytes is None


def test_a_postgresql_varchar_without_a_length_is_not_a_lob():
    assert lob_plan(_pg(("a", "varchar", None)), ["shop.orders"], "postgresql")[1] == []


def test_unrated_write_statements_are_named_in_the_reason():
    c = _collector(writes=(("INSERT", 2.0), ("UPDATE", None), ("DELETE", None), ("SELECT", 5.0)))
    s = size_from_collector(c)
    assert s.writes_per_second == 2.0 and "2 write statements had no rate" in s.reason
    assert "had no rate" not in size_from_collector(_collector()).reason
    assert (
        "1 write statement had no rate"
        in size_from_collector(_collector(writes=(("INSERT", 1.0), ("MERGE", None)))).reason
    )


def test_mysql_types_carry_a_modifier_and_case():
    assert _plan([("a", "LONGTEXT", 100)])[1][0].mode == "limited"


def test_a_table_without_lobs_is_not_listed_and_mariadb_reads_mysql_types():
    assert lob_plan(_collector(), ["shop.orders"], "mysql")[1] == []
    c = _collector(engine="mariadb", columns=_lob_cols(("body", "mediumtext", 16777215)))
    assert lob_plan(c, ["shop.orders"], "mariadb")[1][0].mode == "full"


def test_only_the_asked_tables_are_planned():
    c = _collector(columns=_lob_cols(("body", "longtext", 10)))
    assert lob_plan(c, [], "mysql") == (dms_sizing().lob.default_max_kb, [])


@pytest.mark.parametrize(
    ("engine", "version", "target", "line"),
    [
        ("mysql", "8.0.45", "aurora_mysql", "8.0"),
        ("mysql", "8.4.2", "aurora_mysql", "8.4"),
        ("mysql", "9.1.0", "aurora_mysql", "8.4"),
        ("mysql", "5.7.44-log", "aurora_mysql", "8.0"),
        ("postgresql", "16.10", "aurora_postgresql", "16"),
        ("postgresql", "PostgreSQL 16.10 on x86_64", "aurora_postgresql", "16"),
        ("postgresql", "13.4", "aurora_postgresql", "14"),
        ("postgresql", "19.0", "aurora_postgresql", "18"),
        ("mariadb", "10.6.12-MariaDB", "aurora_mysql", None),
        ("mysql", "", "aurora_mysql", None),
    ],
)
def test_the_aurora_line_is_the_oldest_at_or_above_the_source(engine, version, target, line):
    assert target_engine_line(_collector(engine=engine, version=version), target) == line
