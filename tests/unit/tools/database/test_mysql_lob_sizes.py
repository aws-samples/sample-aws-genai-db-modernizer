# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The MySQL collection measures each LOB column's longest value (#2128).

Step 1's DMS task runs MySQL LOB tables in limited LOB mode, and a value above its LobMaxSize
is truncated, so the plan sizes LobMaxSize from ``max_value_bytes``. Both the collection script
and the live collector measure ``MAX(LENGTH(column))`` of each LOB column, bounded like the
PostgreSQL script's ``lob_scan_max_mb``. The script on a real server: tests/integration/
test_collect_mysql_lob_lengths.py.
"""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.agents.collector import mysql_collector
from src.agents.collector.offline_ingest import build_offline_collector_output
from src.contracts.collector_output import CollectorOutputContract
from src.shared.catalog.dms_sizing import dms_sizing
from src.tools.database.mariadb_tools import MariaDBRemoteCollector
from src.tools.database.mysql_tools import MySQLRemoteCollector
from src.tools.database.offline_parser import parse_offline_collection

SCRIPT = Path(__file__).resolve().parents[4] / "scripts" / "collect-mysql.sql"


def _script() -> str:
    return SCRIPT.read_text()


def _columns_section() -> str:
    """The LOB measurement: section 13, after the collection is printed (#2128)."""
    text = _script()
    return text[text.index("-- ===================== 13. LOB value lengths") :]


# ---------------------------------------------------------------- the script's text


def test_the_script_measures_exactly_the_lob_types_the_catalog_and_collector_name():
    section = _columns_section()
    match = re.search(r"c\.data_type IN \(([^)]*)\)", section, re.S)
    assert match, "the columns section lists the LOB types"
    from src.tools.database.mysql_tools import LOB_TYPES

    names = re.findall(r"'([a-z]+)'", match.group(1))
    listed = set(names)
    assert len(names) == len(listed) == 13  # none missing, none repeated
    assert listed == set(dms_sizing().lob.types["mysql"]) == set(LOB_TYPES)
    assert len(LOB_TYPES) == len(dms_sizing().lob.types["mysql"]) == 13
    # Not LOBs to DMS (WSTRING or BYTES): their declared length bounds them, so they are not read.
    assert not listed & {"tinytext", "text", "tinyblob", "blob"}


def test_the_script_emits_each_columns_longest_value_as_max_value_bytes():
    section = _columns_section()
    assert "COALESCE(MAX(LENGTH(`" in section  # 0 for an empty column, not null
    # One entry per measured column, found again by table and position in the ingest.
    assert "''table_name'', t, ''ordinal_position'', p, ''max_value_bytes'', m" in section
    assert "''lob_sizes''" in section


def test_the_script_reads_a_table_once_for_all_its_lob_columns():
    section = _columns_section()
    # One SELECT per table (CONCAT_WS over that table's columns), grouped by table: a table is
    # scanned once, whatever its number of LOB columns, so the bound is per scan.
    assert section.count("CONCAT_WS(") == 1 and "g.cols" in section
    assert "GROUP BY c.table_name" in section


def test_the_script_bounds_the_scan_like_the_postgresql_script():
    section = _columns_section()
    assert "@lob_scan_max_mb > 0" in section  # 0 measures nothing
    assert "<= @lob_scan_max_mb * 1048576" in section
    text = _script()
    assert re.search(
        r"SET @lob_scan_max_mb = IF\(CAST\(@lob_scan_max_mb AS CHAR\) REGEXP '\^\[0-9\]\+\$', "
        r"CAST\(@lob_scan_max_mb AS UNSIGNED\), 1024\);",
        text,
    )
    from src.tools.database.mysql_tools import DEFAULT_LOB_SCAN_MAX_MB

    assert DEFAULT_LOB_SCAN_MAX_MB == 1024  # the live collector's bound is the same
    assert '@lob_scan_max_mb=0"' in text  # documented in the header


def test_the_script_measures_only_tables_the_user_may_select_and_whose_size_is_known():
    section = _columns_section()
    assert section.count("privilege_type = ''SELECT''") == 3  # global, schema, table grants
    # InnoDB keeps a large value off the pages data_length counts: its tablespace file bounds it,
    # read only with PROCESS (the view raises an error otherwise) and never assumed to exist.
    assert "u.privilege_type = 'PROCESS'" in section
    assert "'INNODB_TABLESPACES', 'INNODB_SYS_TABLESPACES'" in section  # MySQL, MariaDB 10.5
    # A size of 0 is as unknown as none; the table is never bounded by data_length alone.
    assert "(t.engine <> ''InnoDB'' OR ts.f > 0)" in section
    assert "ts.f IS NOT NULL" not in section
    # A partitioned table's tablespaces (name#p#partition) are summed.
    assert "SUM(file_size)" in section and "''#p#''" in section
    # A list that group_concat_max_len cut would not parse.
    assert ">= @@SESSION.group_concat_max_len" in section


def test_the_script_measures_without_writing_or_locking():
    section = "\n".join(
        line for line in _columns_section().splitlines() if not line.startswith("--")
    )
    for word in ("INSERT", "UPDATE", "DELETE", "CREATE TEMPORARY", "CREATE TABLE", "LOCK ",
                 "FOR UPDATE", "FOR SHARE", "SET GLOBAL"):  # fmt: skip
        assert word not in section.upper()
    assert section.count("\nPREPARE ") == 3 and "ANALYZE" not in section.upper()


def test_the_measurement_has_a_timeout_in_a_statement_of_its_own():
    text = _script()
    section = _columns_section()
    assert re.search(
        r"SET @lob_scan_timeout_seconds = IF\(CAST\(@lob_scan_timeout_seconds AS CHAR\) "
        r"REGEXP '\^\[0-9\]\+\$', CAST\(@lob_scan_timeout_seconds AS UNSIGNED\), 60\);",
        text,
    )
    assert "@lob_scan_timeout_seconds=" in text or "@lob_scan_timeout_seconds  limit" in text
    # MySQL: the hint on the SELECT (a whole number: a decimal turns the hint off); MariaDB:
    # SET STATEMENT. The wait for a metadata lock is limited through lock_wait_timeout.
    assert "MAX_EXECUTION_TIME(', CAST(@lob_scan_timeout_seconds * 1000 AS UNSIGNED)" in section
    assert "SET STATEMENT max_statement_time=" in section
    assert "SET SESSION lock_wait_timeout = CAST(IF(@lob_scan_timeout_seconds > 0" in section
    assert "INTO @lob_values" in section
    assert "No\n-- --force is needed" in text  # the header: the collection survives a timeout


def test_a_failed_measurement_cannot_lose_the_collection():
    """The LOB step comes after the collection is printed, and the collection holds no size."""
    text = _script()
    printed = text.index("-- ===================== 12. Assemble Final JSON")
    lob = text.index("-- ===================== 13. LOB value lengths")
    assert printed < lob
    # Nothing before section 13 reads the measurement; the columns carry no max_value_bytes.
    before = text[:lob]
    assert "max_value_bytes" not in re.sub(r"--[^\n]*", "", before)
    assert "@lob_values" not in re.sub(r"--[^\n]*", "", before)
    assert "'collection_output'" not in text[lob:] and "AS collection_output" in text[printed:lob]
    # The second object is the last statement; it is its own SELECT.
    assert text.rstrip().endswith("DEALLOCATE PREPARE lob_sizes_stmt;")


@pytest.mark.passes_on_main  # the grantee was already defined once; this guards it
def test_the_script_reuses_one_definition_of_the_grantee():
    text = _script()
    assert text.count("SET @grantee = CONCAT(") == 1
    assert text.index("SET @grantee = CONCAT(") < text.index("@privileges_not_seen")


# ---------------------------------------------------------------- the contract keeps its shape


def _raw(measured: object = None, *, with_key: bool = True) -> dict:
    column: dict[str, object] = {
        "table_name": "t",
        "column_name": "body",
        "data_type": "longtext",
        "is_nullable": "YES",
    }
    if with_key:
        column["max_value_bytes"] = measured
    return {
        "metadata": {"version": "8.0.36", "database_name": "app"},
        "tables": [{"table_name": "t", "row_count": 10}],
        "columns": [column],
        "queries": [],
    }


def _column(raw: dict):
    output = build_offline_collector_output(
        parse_offline_collection(raw), job_id="j2128", database_name="app", s3_key="k"
    )
    return CollectorOutputContract.model_validate(output).database_schema.tables[0].columns[0]


@pytest.mark.passes_on_main  # the field and its mapping predate #2128; the script now fills it
def test_a_mysql_collection_keeps_the_measured_size_and_an_unmeasured_one_stays_null():
    assert _column(_raw(1_866_112)).max_value_bytes == 1_866_112
    assert _column(_raw(0)).max_value_bytes == 0
    assert _column(_raw(None)).max_value_bytes is None
    assert _column(_raw(with_key=False)).max_value_bytes is None


# ---------------------------------------------------------------- the live collector


class _Server(MySQLRemoteCollector):
    """A collector whose SQL goes to a recorder, with one canned answer per query."""

    def __init__(self, engine="InnoDB", size=1000, file_size=2000, longest=None, fail=None):
        self.sent: list[str] = []
        self.engine, self.size, self.file_size = engine, size, file_size
        self.longest = longest if longest is not None else {}
        self.fail = fail

    def _query(self, sql: str) -> list[dict]:
        self.sent.append(sql)
        if self.fail and self.fail in sql:
            raise RuntimeError("denied")
        if "information_schema.tables" in sql:
            return [{"engine": self.engine, "bytes": str(self.size)}]
        if "tablespaces" in sql.lower():
            return [{"file_size": None if self.file_size is None else str(self.file_size)}]
        return [{f"m{i}": v for i, v in enumerate(self.longest.get("values", []))}]


def _cols():
    return [
        {"column_name": "id", "data_type": "int"},
        {"column_name": "body", "data_type": "longtext"},
        {"column_name": "note", "data_type": "text"},
        {"column_name": "pic`x", "data_type": "mediumblob"},
    ]


def test_the_live_collector_measures_each_lob_column_in_one_select():
    server, cols = _Server(longest={"values": ["1866112", "0"]}), _cols()
    server.measure_lob_columns("posts", cols, lob_scan_timeout_seconds=0)
    select = server.sent[-1]
    assert select == (
        "SELECT COALESCE(MAX(LENGTH(`body`)), 0) AS m0, COALESCE(MAX(LENGTH(`pic``x`)), 0) AS m1 "
        "FROM `posts`"
    )
    assert [c.get("max_value_bytes") for c in cols] == [None, 1_866_112, None, 0]


def test_the_live_collector_skips_a_table_above_the_bound_or_a_bound_of_zero():
    big = _Server(size=3 * 1048576)
    cols = _cols()
    big.measure_lob_columns("posts", cols, lob_scan_max_mb=2)
    assert all("max_value_bytes" not in c for c in cols)
    assert not any("MAX(LENGTH" in sql for sql in big.sent)
    off = _Server(longest={"values": ["5", "5"]})
    off.measure_lob_columns("posts", cols, lob_scan_max_mb=0)
    assert off.sent == [] and all("max_value_bytes" not in c for c in cols)


def test_an_innodb_tablespace_larger_than_data_length_counts_toward_the_bound():
    # data_length leaves out InnoDB's off-page LOB pages; the file size holds them.
    server = _Server(size=16384, file_size=5 * 1048576, longest={"values": ["9", "9"]})
    cols = _cols()
    server.measure_lob_columns("posts", cols, lob_scan_max_mb=2)
    assert all("max_value_bytes" not in c for c in cols)


def test_an_innodb_table_with_no_known_file_size_is_not_measured():
    # No row, a file size of 0 (never fall back to data_length, which leaves out LOB pages),
    # and a view the user may not read all mean an unknown size.
    for server in (_Server(file_size=None), _Server(file_size=0), _Server(fail="tablespaces")):
        cols = _cols()
        server.measure_lob_columns("posts", cols)
        assert all("max_value_bytes" not in c for c in cols)
        assert not any("MAX(LENGTH" in sql for sql in server.sent)


def test_a_table_of_another_engine_is_bounded_by_data_and_index_length():
    server = _Server(engine="MyISAM", file_size=None, longest={"values": ["7", "8"]})
    cols = _cols()
    server.measure_lob_columns("posts", cols)
    assert [c.get("max_value_bytes") for c in cols] == [None, 7, None, 8]
    assert not any("tablespaces" in sql.lower() for sql in server.sent)


def test_a_table_the_user_may_not_select_is_left_unmeasured_not_failed():
    server = _Server(fail="MAX(LENGTH")
    cols = _cols()
    server.measure_lob_columns("posts", cols)
    assert all("max_value_bytes" not in c for c in cols)


def test_a_table_name_with_a_quote_cannot_break_out_of_the_size_query():
    server = _Server()
    server.measure_lob_columns("o'brien\\", _cols())
    assert "table_name = 'o''brien\\\\'" in server.sent[0]


def test_a_table_without_lob_columns_costs_no_query():
    server = _Server()
    server.measure_lob_columns("t", [{"column_name": "id", "data_type": "int"}])
    assert server.sent == []


def test_the_mariadb_collector_measures_too():
    assert MariaDBRemoteCollector.measure_lob_columns is MySQLRemoteCollector.measure_lob_columns


def test_the_schema_read_measures_each_tables_columns():
    db = MagicMock()
    db.collect_tables.return_value = [{"table_name": "posts"}]
    db.collect_columns.return_value = [{"column_name": "body", "data_type": "longtext"}]
    db.collect_primary_key.return_value = []
    db.collect_routine_capture.return_value = {}
    db.collect_routine_stats.return_value = ([], {})
    mysql_collector._collect_schema_raw(db)
    db.measure_lob_columns.assert_called_once_with("posts", db.collect_columns.return_value)


def test_the_file_sizes_of_a_partitioned_table_are_summed_by_one_query():
    server = _Server(longest={"values": ["1", "1"]})
    server.measure_lob_columns("posts", _cols())
    size_query = next(sql for sql in server.sent if "tablespaces" in sql.lower())
    assert "SUM(file_size)" in size_query
    assert "SUBSTRING_INDEX(LOWER(name), '#p#', 1)" in size_query  # name#p#partition


def test_mysql_limits_the_read_with_a_hint_and_the_lock_wait_with_its_session_timeout():
    server, cols = _Server(longest={"values": ["4", "5"]}), _cols()
    server.measure_lob_columns("posts", cols, lob_scan_timeout_seconds=30)
    sql = server.sent[-1]
    assert sql.startswith(
        "SET SESSION lock_wait_timeout = 30; SELECT /*+ MAX_EXECUTION_TIME(30000) */ "
    )
    assert sql.count("FROM `posts`") == 1  # all the LOB columns in one read
    assert [c.get("max_value_bytes") for c in cols] == [None, 4, None, 5]


def test_a_timeout_of_zero_sets_no_limit_and_the_default_is_conservative():
    from src.tools.database.mysql_tools import DEFAULT_LOB_SCAN_TIMEOUT_SECONDS

    assert 0 < DEFAULT_LOB_SCAN_TIMEOUT_SECONDS <= 60
    server = _Server(longest={"values": ["4", "5"]})
    server.measure_lob_columns("posts", _cols(), lob_scan_timeout_seconds=0)
    assert "MAX_EXECUTION_TIME" not in server.sent[-1] and "lock_wait" not in server.sent[-1]


def test_a_read_that_times_out_leaves_the_columns_unmeasured_and_the_collection_goes_on():
    server, cols = _Server(fail="MAX_EXECUTION_TIME"), _cols()
    server.measure_lob_columns("posts", cols)  # the server's timeout error is swallowed
    assert all("max_value_bytes" not in c for c in cols)


class _MariaServer(MariaDBRemoteCollector):
    def __init__(self):
        self.sent: list[str] = []

    def _query(self, sql: str) -> list[dict]:
        self.sent.append(sql)
        if "information_schema.tables" in sql:
            return [{"engine": "InnoDB", "bytes": "100"}]
        if "tablespaces" in sql.lower():
            return [{"file_size": "65536"}]
        return [{"m0": "7", "m1": "9"}]


def test_mariadb_sizes_innodb_tables_from_its_own_view_and_limits_with_set_statement():
    server, cols = _MariaServer(), _cols()
    server.measure_lob_columns("posts", cols, lob_scan_timeout_seconds=30)
    assert "information_schema.INNODB_SYS_TABLESPACES" in server.sent[1]
    assert server.sent[-1].startswith("SET STATEMENT max_statement_time=30 FOR SELECT COALESCE(")
    assert [c.get("max_value_bytes") for c in cols] == [None, 7, None, 9]
    assert "innodb_tablespaces" in MySQLRemoteCollector._TABLESPACE_VIEW
