# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The DMS sizing catalog (#1798): rows in order, a reason per row, one unbounded last row."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.shared.catalog.dms_sizing import DmsSizingTable, dms_sizing, load_dms_sizing
from src.shared.catalog.engines import CatalogError


def test_the_committed_rows_are_the_decided_ones():
    rows = dms_sizing().rows
    assert [(r.instance_class, r.max_database_gb, r.max_writes_per_second) for r in rows] == [
        ("dms.t3.medium", 10, 50),
        ("dms.r6i.large", 500, 1000),
        ("dms.r6i.xlarge", 2000, 5000),
        ("dms.r6i.2xlarge", None, None),
    ]
    assert all(r.why for r in rows) and rows[-1].note and "customer" in rows[-1].note


def test_lob_rules_name_types_per_dialect():
    lob = dms_sizing().lob
    assert {"mysql", "postgresql"} <= set(lob.types) and "longtext" in lob.types["mysql"]
    assert "bytea" in lob.types["postgresql"] and 1 <= lob.default_max_kb <= lob.limited_max_kb


def test_postgresql_and_mysql_never_run_full_lob_mode_and_the_size_is_within_dms_limit():
    # #1918: full LOB mode turns a NULL LOB into an empty value on Aurora PostgreSQL. #2123: it
    # inserts NULL into a NOT NULL LONGTEXT column on Aurora MySQL (MariaDB reads MySQL's rules).
    lob = dms_sizing().lob
    assert set(lob.limited_only) == {"postgresql", "mysql"}
    assert 1 <= lob.undeclared_max_kb <= 102400
    assert "#1918" in lob.limited_only_why and "#2123" in lob.limited_only_why
    # Only the PostgreSQL source endpoint has FailTasksOnLobTruncation; MySQL's LOB types
    # declare their type's ceiling.
    assert lob.fails_on_truncation == ("postgresql",)
    assert lob.declared_is_type_ceiling == ("mysql",)


def test_the_engine_version_has_its_reasoning_per_source():
    engine = dms_sizing().engine
    assert set(engine.source_why) == {"postgresql", "mysql"}
    assert engine.why_for("mysql") == f"{engine.why} {engine.source_why['mysql']}"
    assert engine.why_for("mariadb") == engine.why_for("mysql") and "#2123" in engine.why_for(
        "mysql"
    )
    assert engine.why_for(None) == engine.why


def test_truncation_failing_dialects_must_be_limited_only(tmp_path):
    path = _write(tmp_path, _LAST)
    text = path.read_text()
    path.write_text(
        text.replace(
            "[lob]\n",
            '[lob]\nlimited_only = ["postgresql"]\nfails_on_truncation = ["postgresql"]\n',
        )
    )
    assert load_dms_sizing(path).lob.fails_on_truncation == ("postgresql",)
    path.write_text(text.replace("[lob]\n", '[lob]\nfails_on_truncation = ["oracle"]\n'))
    with pytest.raises(CatalogError, match="not in lob.limited_only"):
        load_dms_sizing(path)


def test_the_gate_lag_limit_is_30_seconds_with_a_reason():
    gate = dms_sizing().gate
    assert gate.max_lag_seconds == 30 and "EventsPollInterval" in gate.why


def _write(tmp_path: Path, rows: str) -> Path:
    path = tmp_path / "dms_sizing.toml"
    path.write_text(rows + '\n[engine]\nversion = "3.6.1"\nwhy = "w"\n[gate]\nmax_lag_seconds = 30\nwhy = "w"\n[lob]\nlimited_max_kb = 1024\ndefault_max_kb = 32\nwhy = "w"\n'
                    'scalars_why = "w"\nunbounded_varchar_max_bytes = 100\nundeclared_max_kb = 8192\nlimited_only_why = "w"\n'
                    'measured_bytes_per_character = 2\nmeasured_headroom = 1.25\nmeasured_round_kb = 1024\nmeasured_why = "w"\ntable_settings_why = "w"\n'
                    '[[lob.instance_classes]]\ninstance_class = "dms.t3.medium"\nmemory_gib = 4\nmax_lob_kb = 64\nmax_table_lob_kb = 64\nmax_total_lob_kb = 16384\n'
                    'included_storage_gb = 50\nwhy = "w"\n'
                    '[lob.types]\nmysql = ["text"]\npostgresql = ["text"]\n'
                    '[lob.scalars]\npostgresql = ["int4"]\n[not_moved]\npostgresql = ["tsvector"]\nwhy = "w"\n')  # fmt: skip
    return path


_ROW = '[[rows]]\nid = "{id}"\ninstance_class = "{cls}"\n{bounds}allocated_storage_gb = 50\nincluded_storage_gb = 50\nwhy = "w"\n'


def test_a_bounded_last_row_is_refused(tmp_path):
    path = _write(tmp_path, _ROW.format(id="a", cls="dms.t3.medium",
                                        bounds="max_database_gb = 10\nmax_writes_per_second = 50\n"))  # fmt: skip
    with pytest.raises(CatalogError, match="last row"):
        load_dms_sizing(path)


def test_rows_out_of_order_are_refused(tmp_path):
    rows = (_ROW.format(id="b", cls="dms.r6i.large", bounds="max_database_gb = 500\nmax_writes_per_second = 1000\n")
            + _ROW.format(id="a", cls="dms.t3.medium", bounds="max_database_gb = 10\nmax_writes_per_second = 50\n")
            + _ROW.format(id="c", cls="dms.r6i.2xlarge", bounds=""))  # fmt: skip
    with pytest.raises(CatalogError, match="increase"):
        load_dms_sizing(_write(tmp_path, rows))


def test_an_unknown_field_is_refused(tmp_path):
    rows = _ROW.format(id="c", cls="dms.r6i.2xlarge", bounds="memory_gib = 8\n")
    with pytest.raises(CatalogError):
        load_dms_sizing(_write(tmp_path, rows))


def test_the_table_type_is_frozen():
    assert DmsSizingTable.model_config["frozen"] is True


def test_lob_type_lists_follow_the_guide():
    types = dms_sizing().lob.types
    mysql, pg = set(types["mysql"]), set(types["postgresql"])
    assert {"text", "tinytext", "blob", "tinyblob"}.isdisjoint(mysql)
    assert {"mediumtext", "longtext", "mediumblob", "longblob", "json"} <= mysql
    assert {"geometry", "point", "linestring", "polygon", "multipoint", "multilinestring",
            "multipolygon", "geometrycollection"} <= mysql  # fmt: skip
    assert {"text", "citext", "bytea", "json", "jsonb", "xml", "array", "composite", "hstore"} <= pg


_BOUNDED = "max_database_gb = 10\nmax_writes_per_second = 50\n"
_LAST = _ROW.format(id="z", cls="dms.r6i.2xlarge", bounds="")
_FIRST = _ROW.format(id="a", cls="dms.t3.medium", bounds=_BOUNDED)


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        (_FIRST + _ROW.format(id="a", cls="dms.r6i.large", bounds=""), "duplicate"),
        (_ROW.format(id="a", cls="dms.t3.medium", bounds="max_database_gb = 10\n") + _LAST, "both bounds"),
        (_ROW.format(id="a", cls="dms.t3.medium", bounds="max_writes_per_second = 10\n") + _LAST, "both bounds"),
        (_FIRST.replace('why = "w"\n', "") + _LAST, "why"),
        (_FIRST.replace('why = "w"', 'why = "a\\nb"') + _LAST, "why"),
    ],
)  # fmt: skip
def test_each_row_rule_is_enforced(tmp_path, rows, message):
    with pytest.raises(CatalogError, match=message):
        load_dms_sizing(_write(tmp_path, rows))


@pytest.mark.parametrize("version", ["3.6", "latest", "3.6.1\n", ""])
def test_an_engine_version_must_be_three_numbers(tmp_path, version):
    path = _write(tmp_path, _FIRST + _LAST)
    path.write_text(
        path.read_text().replace('version = "3.6.1"', f'version = "{version}"'.replace("\n", "\\n"))
    )
    with pytest.raises(CatalogError):
        load_dms_sizing(path)


def test_the_catalog_names_the_engine_version_with_its_reason():
    engine = dms_sizing().engine
    assert engine.version == "3.6.1" and "3.5.4" in engine.why


def test_default_above_limited_is_refused(tmp_path):
    path = _write(tmp_path, _LAST)
    path.write_text(path.read_text().replace("default_max_kb = 32", "default_max_kb = 2048"))
    with pytest.raises(CatalogError, match="default_max_kb"):
        load_dms_sizing(path)


@pytest.mark.parametrize(
    ("line", "message"),
    [('mysql = ["Text"]', "lower-case"), ('mysql = ["text", "text"]', "twice")],
)
def test_lob_type_names_are_lower_case_and_unique(tmp_path, line, message):
    path = _write(tmp_path, _LAST)
    path.write_text(path.read_text().replace('mysql = ["text"]', line))
    with pytest.raises(CatalogError, match=message):
        load_dms_sizing(path)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('postgresql = ["int4"]', 'postgresql = ["Int4"]', "lower-case"),
        ('postgresql = ["int4"]', 'postgresql = ["int4", "int4"]', "twice"),
        ('postgresql = ["int4"]', 'postgresql = ["text"]', "also listed"),
    ],
)
def test_scalar_type_names_are_lower_case_unique_and_not_lobs(tmp_path, old, new, message):
    path = _write(tmp_path, _LAST)
    path.write_text(path.read_text().replace(old, new))
    with pytest.raises(CatalogError, match=message):
        load_dms_sizing(path)


def test_postgresql_scalars_cover_the_plain_types_and_leave_lobs_out():
    lob = dms_sizing().lob
    scalars = set(lob.scalars["postgresql"])
    assert {"int4", "bigint", "integer", "varchar", "character", "timestamp", "uuid", "numeric", "double",
            "int4range", "point", "money", "oid", "inet"} <= scalars  # fmt: skip
    assert not [t for t in scalars if "multirange" in t]  # unbounded strings in DMS: full mode
    assert scalars.isdisjoint(lob.types["postgresql"]) and "user-defined" in lob.types["postgresql"]
    assert lob.unbounded_varchar_max_bytes == 10485760 and lob.scalars_why


def test_included_storage_is_required_and_within_the_allocation(tmp_path):
    last = _ROW.format(id="z", cls="dms.r6i.2xlarge", bounds="")
    with pytest.raises(CatalogError):
        load_dms_sizing(_write(tmp_path, last.replace("included_storage_gb = 50\n", "")))
    with pytest.raises(CatalogError, match="included_storage_gb"):
        load_dms_sizing(
            _write(tmp_path, last.replace("included_storage_gb = 50", "included_storage_gb = 51"))
        )
    with pytest.raises(CatalogError):
        load_dms_sizing(
            _write(tmp_path, last.replace("included_storage_gb = 50", "included_storage_gb = -1"))
        )


def test_the_committed_rows_include_the_storage_the_pricing_page_lists():
    # AWS DMS pricing: T3 includes 50 GB; R5 (assumed for r6i) includes 100 GB.
    included = {r.instance_class: r.included_storage_gb for r in dms_sizing().rows}
    assert included["dms.t3.medium"] == 50
    assert {v for k, v in included.items() if "r6i" in k} == {100}


@pytest.mark.passes_on_main  # main refuses the whole file: it has no [gate] field
@pytest.mark.parametrize("value", ["0", "-1", "2.5", '"30"'])
def test_a_bad_gate_lag_limit_is_refused(tmp_path, value):
    path = _write(tmp_path, _ROW.format(id="a", cls="dms.t3.medium", bounds=""))
    path.write_text(path.read_text().replace("max_lag_seconds = 30", f"max_lag_seconds = {value}"))
    with pytest.raises(CatalogError):
        load_dms_sizing(path)


def test_a_missing_gate_section_is_refused(tmp_path):
    path = _write(tmp_path, _ROW.format(id="a", cls="dms.t3.medium", bounds=""))
    path.write_text(path.read_text().replace('[gate]\nmax_lag_seconds = 30\nwhy = "w"\n', ""))
    with pytest.raises(CatalogError):
        load_dms_sizing(path)
