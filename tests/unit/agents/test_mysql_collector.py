"""Unit tests for shared mysql_collector helper functions.

These helpers (`_build_tables`, `_build_triggers`) are used by MySQL LIVE
mode AND all other engines' offline mode via `_collect_offline`. Focused
on hardening changes from the Oracle production JSON test run.
"""

import logging

import pytest

from src.agents.collector.mysql_collector import (
    _TYPE_MAP,
    _build_columns,
    _build_tables,
    _build_triggers,
    _is_truthy,
    _normalize_datetime_str,
    _normalize_index_type,
)
from src.contracts.collector_output import NormalizedDataType

# ---------------------------------------------------------------------------
# _build_tables: skip tables with zero columns
# ---------------------------------------------------------------------------


class TestTypeMapExactVsApproximateNumerics:
    """DECIMAL carries real precision/scale, so it stays an exact numeric.

    FLOAT/DOUBLE are approximate and must never resolve through the
    exact-numeric path, which would reach a target DECIMAL(n,0) and truncate
    every fractional value (#156). Inherited by the MariaDB collector, which
    reuses this type map.
    """

    def test_decimal_is_exact(self):
        assert _TYPE_MAP["decimal"] == NormalizedDataType.decimal

    def test_float_and_double_are_approximate(self):
        assert _TYPE_MAP["float"] == NormalizedDataType.floating_point
        assert _TYPE_MAP["double"] == NormalizedDataType.floating_point

    def test_oracle_offline_native_types(self):
        """Oracle's offline collection (collect-oracle.sql) feeds the same
        shared _build_columns as every other engine, unlike live mode (which
        has oracle_collector._ORACLE_TYPE_MAP) - these keys would otherwise
        resolve to normalized_data_type=None (#156)."""
        assert _TYPE_MAP["number"] == NormalizedDataType.decimal
        assert _TYPE_MAP["binary_float"] == NormalizedDataType.floating_point
        assert _TYPE_MAP["binary_double"] == NormalizedDataType.floating_point

    def test_sqlserver_offline_native_types(self):
        """SQL Server's offline collection (collect-sqlserver.sql) feeds the
        same shared _build_columns as every other engine, unlike live mode
        (which has sqlserver_collector._SQLSERVER_TYPE_MAP) (#156)."""
        assert _TYPE_MAP["numeric"] == NormalizedDataType.decimal
        assert _TYPE_MAP["money"] == NormalizedDataType.decimal
        assert _TYPE_MAP["smallmoney"] == NormalizedDataType.decimal
        assert _TYPE_MAP["real"] == NormalizedDataType.floating_point


class TestOfflineColumnsPerEngine:
    """One representative row per engine, shaped exactly as that engine's
    offline collection script (scripts/collect-<engine>.sql) emits it, run
    through the shared offline builder (_build_columns) every engine uses.
    Guards against the keys drifting apart again (#156): a SQL script can
    emit a column under the wrong key with no import-time signal that
    _build_columns silently ignores it.
    """

    def test_oracle_number_row(self):
        """collect-oracle.sql: NUMBER's own precision/scale, no max_length."""
        row = {
            "column_name": "amount",
            "data_type": "number",
            "max_length": None,
            "numeric_precision": 10,
            "numeric_scale": 2,
            "is_nullable": "Y",
            "column_default": None,
            "is_identity": "NO",
        }
        col = _build_columns([row])[0]
        assert col.normalized_data_type == NormalizedDataType.decimal
        assert col.numeric_precision == 10
        assert col.numeric_scale == 2
        assert col.max_length is None

    def test_oracle_number_row_old_offline_file_falls_back_to_data_precision(self):
        """A collection captured before #156 fixed collect-oracle.sql has
        precision/scale under the old keys (Oracle's own catalog column
        names), not the generic ones _build_columns otherwise reads -- it
        must still get an exact type, not revert to always-unknown."""
        row = {
            "column_name": "amount",
            "data_type": "number",
            "max_length": None,
            "data_precision": 10,
            "data_scale": 2,
            "is_nullable": "Y",
            "column_default": None,
            "is_identity": "NO",
        }
        col = _build_columns([row])[0]
        assert col.numeric_precision == 10
        assert col.numeric_scale == 2

    def test_new_keys_take_priority_over_old_keys_when_both_present(self):
        row = {
            "column_name": "amount",
            "data_type": "number",
            "numeric_precision": 10,
            "numeric_scale": 2,
            "data_precision": 99,
            "data_scale": 9,
            "is_nullable": "Y",
            "column_default": None,
        }
        col = _build_columns([row])[0]
        assert col.numeric_precision == 10
        assert col.numeric_scale == 2

    def test_oracle_binary_double_row(self):
        """collect-oracle.sql: BINARY_DOUBLE has no decimal precision/scale."""
        row = {
            "column_name": "measurement",
            "data_type": "binary_double",
            "max_length": None,
            "numeric_precision": None,
            "numeric_scale": None,
            "is_nullable": "Y",
            "column_default": None,
            "is_identity": "NO",
        }
        col = _build_columns([row])[0]
        assert col.normalized_data_type == NormalizedDataType.floating_point
        assert col.numeric_precision is None

    @pytest.mark.parametrize("data_type", ["varchar2", "nvarchar2", "nchar"])
    def test_oracle_string_row_keeps_its_char_length(self, data_type):
        """collect-oracle.sql: VARCHAR2/NVARCHAR2/NCHAR emit CHAR_LENGTH as
        max_length; a name the shared map lacks reaches the draft as TEXT (#543)."""
        row = {
            "column_name": "name",
            "data_type": data_type,
            "max_length": 100,
            "numeric_precision": None,
            "numeric_scale": None,
            "is_nullable": "Y",
            "column_default": None,
            "is_identity": "NO",
        }
        col = _build_columns([row])[0]
        assert col.normalized_data_type == NormalizedDataType.string
        assert col.max_length == 100

    def test_sqlserver_real_row(self):
        """collect-sqlserver.sql: sys.columns.precision/scale, aliased."""
        row = {
            "column_name": "ratio",
            "data_type": "real",
            "max_length": None,
            "numeric_precision": 24,
            "numeric_scale": 0,
            "is_nullable": "YES",
            "column_default": None,
        }
        col = _build_columns([row])[0]
        assert col.normalized_data_type == NormalizedDataType.floating_point
        assert col.numeric_precision == 24

    def test_sqlserver_numeric_row(self):
        row = {
            "column_name": "amount",
            "data_type": "numeric",
            "max_length": None,
            "numeric_precision": 10,
            "numeric_scale": 2,
            "is_nullable": "YES",
            "column_default": None,
        }
        col = _build_columns([row])[0]
        assert col.normalized_data_type == NormalizedDataType.decimal
        assert col.numeric_precision == 10
        assert col.numeric_scale == 2

    @pytest.mark.parametrize("data_type", ["nvarchar", "nchar"])
    def test_sqlserver_string_row_keeps_its_char_length(self, data_type):
        """collect-sqlserver.sql: the n-types' max_length is already bytes / 2 (#543)."""
        row = {
            "column_name": "name",
            "data_type": data_type,
            "max_length": 100,
            "numeric_precision": None,
            "numeric_scale": None,
            "is_nullable": "YES",
            "column_default": None,
        }
        col = _build_columns([row])[0]
        assert col.normalized_data_type == NormalizedDataType.string
        assert col.max_length == 100

    def test_postgresql_numeric_row(self):
        """collect-postgresql.sql: information_schema.columns.numeric_precision
        / numeric_scale, aliased; data_type is the bare udt_name."""
        row = {
            "column_name": "amount",
            "data_type": "numeric",
            "max_length": None,
            "numeric_precision": 10,
            "numeric_scale": 2,
            "is_nullable": "YES",
            "column_default": None,
        }
        col = _build_columns([row])[0]
        assert col.normalized_data_type == NormalizedDataType.decimal
        assert col.numeric_precision == 10
        assert col.numeric_scale == 2

    def test_postgresql_real_row(self):
        """collect-postgresql.sql emits udt_name ("float4"), not the
        information_schema.columns.data_type spelling ("real") live mode's
        own _PG_TYPE_MAP uses."""
        row = {
            "column_name": "ratio",
            "data_type": "float4",
            "max_length": None,
            "numeric_precision": None,
            "numeric_scale": None,
            "is_nullable": "YES",
            "column_default": None,
        }
        col = _build_columns([row])[0]
        assert col.normalized_data_type == NormalizedDataType.floating_point


class TestBuildTablesSkipsEmptyColumns:
    """Oracle SYS_IOT_OVER_* and HS_PARTITION_COL_* internal tables appear
    in ALL_TABLES but have zero rows in ALL_TAB_COLUMNS. After the parser's
    table_name-based join, they arrive here with columns=[], which
    previously crashed Table's min_length=1 validator.
    """

    @staticmethod
    def _table_raw(name: str, columns: list[dict] | None = None) -> dict:
        return {
            "table_name": name,
            "row_count": 0,
            "data_size_mb": 0,
            "columns": columns if columns is not None else [],
            "indexes": [],
            "foreign_keys": [],
        }

    @staticmethod
    def _column_raw(name: str) -> dict:
        return {
            "column_name": name,
            "ordinal_position": 1,
            "data_type": "varchar",
            "max_length": 100,
            "is_nullable": "YES",
            "column_default": None,
        }

    def test_empty_columns_table_is_skipped(self):
        raw = [self._table_raw("sys_iot_over_19087")]
        tables = _build_tables(raw, "db")
        assert tables == []

    def test_populated_table_is_kept(self):
        raw = [self._table_raw("users", [self._column_raw("id")])]
        tables = _build_tables(raw, "db")
        assert len(tables) == 1
        assert tables[0].table_name == "users"

    def test_mix_of_empty_and_populated(self):
        raw = [
            self._table_raw("users", [self._column_raw("id")]),
            self._table_raw("sys_iot_over_19087"),
            self._table_raw("orders", [self._column_raw("id")]),
            self._table_raw("hs_partition_col_name"),
        ]
        tables = _build_tables(raw, "db")
        # Only the two real tables survive
        names = {t.table_name for t in tables}
        assert names == {"users", "orders"}

    def test_skipped_tables_are_logged(self, caplog):
        raw = [
            self._table_raw("sys_iot_over_19087"),
            self._table_raw("hs_partition_col_name"),
        ]
        with caplog.at_level(logging.WARNING, logger="src.agents.collector.mysql_collector"):
            _build_tables(raw, "db")
        # Both empty-column tables are counted in the warning
        assert any("Skipped 2 table" in rec.message for rec in caplog.records)

    def test_no_warning_when_none_skipped(self, caplog):
        raw = [self._table_raw("users", [self._column_raw("id")])]
        with caplog.at_level(logging.WARNING, logger="src.agents.collector.mysql_collector"):
            _build_tables(raw, "db")
        assert not any("Skipped" in rec.message for rec in caplog.records)

    def test_returns_empty_list_when_all_skipped(self):
        raw = [self._table_raw(f"sys_iot_over_{n}") for n in range(5)]
        tables = _build_tables(raw, "db")
        assert tables == []


# ---------------------------------------------------------------------------
# _build_triggers: normalize event_type + timing, filter non-DML
# ---------------------------------------------------------------------------


class TestBuildTriggersNormalization:
    """Contract requires event_type ∈ {INSERT,UPDATE,DELETE} and
    timing ∈ {BEFORE,AFTER,INSTEAD OF}. Oracle emits values like:
        event_type: 'LOGON ' (trailing space) — non-DML system trigger
        event_type: 'INSERT OR UPDATE' — compound DML
        timing: 'AFTER EVENT' — non-DML timing prefix
        timing: 'BEFORE EACH ROW' — DML timing with suffix
        table_name: None — DB-level trigger
    All previously produced Pydantic ValidationErrors that crashed
    the whole collector. The new logic filters + normalizes.
    """

    def test_returns_none_for_empty_raw(self):
        assert _build_triggers([]) is None

    def test_valid_single_event_dml_trigger_passes(self):
        raw = [
            {
                "trigger_name": "trg_users_audit",
                "table_name": "users",
                "event_type": "INSERT",
                "timing": "BEFORE",
            }
        ]
        result = _build_triggers(raw)
        assert result is not None
        assert len(result) == 1
        assert result[0].event_type == "INSERT"
        assert result[0].timing == "BEFORE"

    def test_trailing_whitespace_stripped(self):
        """Oracle emits 'LOGON ' with trailing space — but this is a
        non-DML event so it should be filtered. Instead test the trim
        with a DML event that has trailing whitespace.
        """
        raw = [
            {
                "trigger_name": "trg1",
                "table_name": "users",
                "event_type": "UPDATE ",  # trailing space
                "timing": " BEFORE ",  # both sides
            }
        ]
        result = _build_triggers(raw)
        assert result is not None
        assert result[0].event_type == "UPDATE"
        assert result[0].timing == "BEFORE"

    def test_compound_event_normalized_to_first(self):
        """Oracle 'INSERT OR UPDATE OR DELETE' → 'INSERT'."""
        raw = [
            {
                "trigger_name": "trg_multi",
                "table_name": "users",
                "event_type": "INSERT OR UPDATE OR DELETE",
                "timing": "AFTER",
            }
        ]
        result = _build_triggers(raw)
        assert result is not None
        assert result[0].event_type == "INSERT"

    def test_compound_update_delete_matches_update_first(self):
        raw = [
            {
                "trigger_name": "trg1",
                "table_name": "users",
                "event_type": "UPDATE OR DELETE",
                "timing": "AFTER",
            }
        ]
        result = _build_triggers(raw)
        assert result is not None
        assert result[0].event_type == "UPDATE"

    def test_delete_only_compound_normalizes(self):
        raw = [
            {
                "trigger_name": "trg1",
                "table_name": "users",
                "event_type": "DELETE",
                "timing": "AFTER",
            }
        ]
        result = _build_triggers(raw)
        assert result is not None
        assert result[0].event_type == "DELETE"

    def test_timing_with_row_suffix(self):
        """Oracle emits 'BEFORE EACH ROW' — normalize prefix."""
        raw = [
            {
                "trigger_name": "trg1",
                "table_name": "users",
                "event_type": "INSERT",
                "timing": "BEFORE EACH ROW",
            }
        ]
        result = _build_triggers(raw)
        assert result is not None
        assert result[0].timing == "BEFORE"

    def test_timing_with_statement_suffix(self):
        """Oracle emits 'AFTER STATEMENT'."""
        raw = [
            {
                "trigger_name": "trg1",
                "table_name": "users",
                "event_type": "INSERT",
                "timing": "AFTER STATEMENT",
            }
        ]
        result = _build_triggers(raw)
        assert result is not None
        assert result[0].timing == "AFTER"

    def test_instead_of_timing_preserved(self):
        """View triggers use INSTEAD OF."""
        raw = [
            {
                "trigger_name": "trg1",
                "table_name": "users_view",
                "event_type": "INSERT",
                "timing": "INSTEAD OF",
            }
        ]
        result = _build_triggers(raw)
        assert result is not None
        assert result[0].timing == "INSTEAD OF"

    def test_logon_trigger_filtered(self):
        """Oracle LOGON trigger — non-DML, must be filtered."""
        raw = [
            {
                "trigger_name": "dbms_set_pdb",
                "table_name": None,
                "event_type": "LOGON ",
                "timing": "AFTER EVENT",
            }
        ]
        result = _build_triggers(raw)
        assert result is None

    def test_ddl_trigger_filtered(self):
        raw = [
            {
                "trigger_name": "ddl_trigger",
                "table_name": None,
                "event_type": "DDL",
                "timing": "AFTER EVENT",
            }
        ]
        result = _build_triggers(raw)
        assert result is None

    def test_startup_trigger_filtered(self):
        raw = [
            {
                "trigger_name": "startup_trigger",
                "table_name": None,
                "event_type": "STARTUP",
                "timing": "AFTER EVENT",
            }
        ]
        result = _build_triggers(raw)
        assert result is None

    def test_truncate_trigger_filtered(self):
        """PostgreSQL supports TRUNCATE triggers, but our contract
        doesn't include it in the enum. Filter rather than fail.
        """
        raw = [
            {
                "trigger_name": "trunc_trigger",
                "table_name": "users",
                "event_type": "TRUNCATE",
                "timing": "AFTER",
            }
        ]
        result = _build_triggers(raw)
        assert result is None

    def test_missing_table_name_filtered_even_for_dml(self):
        """DML event but no table_name — DB-level trigger, cannot fit
        contract's table_id field."""
        raw = [
            {
                "trigger_name": "orphan",
                "table_name": None,
                "event_type": "INSERT",
                "timing": "BEFORE",
            }
        ]
        result = _build_triggers(raw)
        assert result is None

    def test_mixed_dml_and_non_dml(self):
        """Real-world case: some DML triggers on tables + some
        DB-level system triggers. Only the DML ones survive.
        """
        raw = [
            {
                "trigger_name": "trg_users_audit",
                "table_name": "users",
                "event_type": "INSERT",
                "timing": "AFTER",
            },
            {
                "trigger_name": "dbms_set_pdb",
                "table_name": None,
                "event_type": "LOGON ",
                "timing": "AFTER EVENT",
            },
            {
                "trigger_name": "trg_orders_check",
                "table_name": "orders",
                "event_type": "UPDATE OR DELETE",
                "timing": "BEFORE STATEMENT",
            },
        ]
        result = _build_triggers(raw)
        assert result is not None
        assert len(result) == 2
        assert {t.trigger_name for t in result} == {"trg_users_audit", "trg_orders_check"}
        # Compound event normalized to UPDATE
        orders = next(t for t in result if t.trigger_name == "trg_orders_check")
        assert orders.event_type == "UPDATE"
        assert orders.timing == "BEFORE"

    def test_skipped_triggers_logged(self, caplog):
        raw = [
            {
                "trigger_name": "sys_1",
                "table_name": None,
                "event_type": "LOGON",
                "timing": "AFTER EVENT",
            },
            {
                "trigger_name": "sys_2",
                "table_name": None,
                "event_type": "STARTUP",
                "timing": "AFTER EVENT",
            },
        ]
        with caplog.at_level(logging.WARNING, logger="src.agents.collector.mysql_collector"):
            _build_triggers(raw)
        assert any(
            "Skipped 2 non-DML" in rec.message or "Skipped 2 non-DML or DB-level" in rec.message
            for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# wait_time_percent computation in _collect_offline
# ---------------------------------------------------------------------------


class TestWaitTimePercentComputation:
    """The offline path enriches query patterns with wait_events from
    the SQL script (Oracle V$SYSTEM_EVENT, SQL Server dm_os_wait_stats).

    Previously the parser used `avg_wait_ms` (a raw millisecond value)
    directly as `wait_time_percent`, which fails Pydantic's ≤100 constraint
    for any workload with mean wait latency >100ms — the exact bug we hit
    on the customer's Oracle data (avg_wait_ms = 1,151,685ms).

    The fix computes percent-of-total from `time_waited_ms`, and prefers
    an explicit `wait_time_percent` field when the SQL script provides one.
    """

    @staticmethod
    def _make_parsed(wait_events: list[dict]) -> dict:
        """Build the minimal parsed dict shape that triggers the
        wait_events enrichment branch in _collect_offline.
        """
        return {"wait_events": wait_events}

    def test_percent_computed_from_time_waited_ms(self):
        """No explicit percent field → parser derives from
        time_waited_ms as fraction of total.
        """
        # Import here so we hit the same module the code uses
        from src.contracts.collector_output import WaitEvent

        # Simulate the wait_events section of the parser output
        events = [
            {
                "event": "db file sequential read",
                "time_waited_ms": 693_633_868.8,
                "avg_wait_ms": 0.5,  # this is what the old code used (wrong)
            },
            {
                "event": "enq: TM - contention",
                "time_waited_ms": 462_977_722.22,
                "avg_wait_ms": 1_151_685.876,  # would violate ≤100 in old code
            },
        ]

        # Duplicate the computation from _collect_offline (line 388 onwards)
        # to test in isolation without setting up the full offline pipeline.
        total = sum(float(w.get("time_waited_ms") or 0) for w in events) or 1.0

        def _wait_percent(w: dict) -> float:
            explicit = w.get("wait_time_percent")
            if explicit is not None:
                return float(min(100.0, max(0.0, float(explicit))))
            time_ms = float(w.get("time_waited_ms") or 0)
            return float(min(100.0, max(0.0, (time_ms / total) * 100)))

        wait_list = [
            WaitEvent(
                event_name=w.get("event", ""),
                wait_time_ms=float(w.get("time_waited_ms") or 0),
                wait_time_percent=_wait_percent(w),
            )
            for w in events
        ]

        # Both percents in valid range and sum to 100
        for w in wait_list:
            assert 0.0 <= w.wait_time_percent <= 100.0
        assert abs(sum(w.wait_time_percent for w in wait_list) - 100.0) < 0.01

    def test_explicit_percent_from_script_preferred(self):
        """When SQL script emits `wait_time_percent` (the collect-oracle.sql
        SUM() OVER () pattern), the parser uses that value directly.
        """
        from src.contracts.collector_output import WaitEvent

        events = [
            {
                "event": "db file sequential read",
                "time_waited_ms": 693_633_868.8,
                "wait_time_percent": 37.862,  # from Oracle SUM() OVER ()
            },
        ]
        total = sum(float(w.get("time_waited_ms") or 0) for w in events) or 1.0

        def _wait_percent(w: dict) -> float:
            explicit = w.get("wait_time_percent")
            if explicit is not None:
                return float(min(100.0, max(0.0, float(explicit))))
            time_ms = float(w.get("time_waited_ms") or 0)
            return float(min(100.0, max(0.0, (time_ms / total) * 100)))

        wait = WaitEvent(
            event_name=events[0].get("event", ""),
            wait_time_ms=float(events[0].get("time_waited_ms") or 0),
            wait_time_percent=_wait_percent(events[0]),
        )
        # Uses the explicit script value, not the computed 100.0
        assert wait.wait_time_percent == 37.862

    def test_percent_capped_at_100(self):
        """If for any reason percent > 100 (buggy script output), clamp."""
        # Test the computation directly, not through WaitEvent constructor
        # (which itself has ≤100 validation)
        w = {"time_waited_ms": 200, "wait_time_percent": 150}
        total = 100.0

        def _wait_percent(w: dict) -> float:
            explicit = w.get("wait_time_percent")
            if explicit is not None:
                return float(min(100.0, max(0.0, float(explicit))))
            time_ms = float(w.get("time_waited_ms") or 0)
            return float(min(100.0, max(0.0, (time_ms / total) * 100)))

        assert _wait_percent(w) == 100.0

    def test_percent_floored_at_0(self):
        """Negative script output (shouldn't happen but be defensive)."""
        w = {"time_waited_ms": 100, "wait_time_percent": -5}
        total = 100.0

        def _wait_percent(w: dict) -> float:
            explicit = w.get("wait_time_percent")
            if explicit is not None:
                return float(min(100.0, max(0.0, float(explicit))))
            time_ms = float(w.get("time_waited_ms") or 0)
            return float(min(100.0, max(0.0, (time_ms / total) * 100)))

        assert _wait_percent(w) == 0.0

    def test_zero_total_wait_avoids_div_by_zero(self):
        """When all events have zero time (no wait activity yet),
        the fallback total = 1.0 prevents ZeroDivisionError.
        """
        events = [{"event": "no waits", "time_waited_ms": 0}]
        total = sum(float(w.get("time_waited_ms") or 0) for w in events) or 1.0
        assert total == 1.0

        def _wait_percent(w: dict) -> float:
            explicit = w.get("wait_time_percent")
            if explicit is not None:
                return float(min(100.0, max(0.0, float(explicit))))
            time_ms = float(w.get("time_waited_ms") or 0)
            return float(min(100.0, max(0.0, (time_ms / total) * 100)))

        assert _wait_percent(events[0]) == 0.0


# ---------------------------------------------------------------------------
# _normalize_index_type: Oracle → contract enum mapping
# ---------------------------------------------------------------------------


class TestNormalizeIndexType:
    """Bug caught on production Oracle data — the customer's JSON had 35
    indexes with index_type='functional' (Oracle FUNCTION-BASED NORMAL
    lower-cased by the collect-oracle.sql script). The Index Pydantic
    contract rejects any value not in
    {btree,hash,gist,gin,fulltext,spatial,other}, crashing the collector.

    The parser-side normalizer maps Oracle-specific values into the
    contract enum, providing backward compatibility for JSON captured
    with the pre-fix version of the SQL script.
    """

    def test_none_passes_through(self):
        assert _normalize_index_type(None) is None

    def test_valid_btree_unchanged(self):
        assert _normalize_index_type("btree").value == "btree"

    def test_valid_other_unchanged(self):
        assert _normalize_index_type("other").value == "other"

    @pytest.mark.parametrize("method", ["spgist", "brin"])
    def test_postgres_methods_added_in_293_pass_through(self, method):
        """SP-GiST and BRIN used to fall back to ``other`` and lose the method."""
        assert _normalize_index_type(method).value == method

    def test_case_insensitive(self):
        assert _normalize_index_type("BTREE").value == "btree"
        assert _normalize_index_type("BtRee").value == "btree"

    def test_whitespace_stripped(self):
        assert _normalize_index_type("  btree  ").value == "btree"

    def test_oracle_functional_lowercase(self):
        """The exact value that crashed on the customer's Oracle 19c JSON."""
        assert _normalize_index_type("functional").value == "other"

    def test_oracle_function_based_normal(self):
        """Oracle's ALL_INDEXES.INDEX_TYPE raw value (with prefix match)."""
        assert _normalize_index_type("FUNCTION-BASED NORMAL").value == "other"

    def test_oracle_bitmap(self):
        assert _normalize_index_type("BITMAP").value == "other"

    def test_oracle_iot_top(self):
        assert _normalize_index_type("IOT - TOP").value == "other"

    def test_oracle_cluster(self):
        assert _normalize_index_type("CLUSTER").value == "other"

    def test_oracle_domain(self):
        assert _normalize_index_type("DOMAIN").value == "other"

    def test_oracle_normal_maps_to_btree(self):
        assert _normalize_index_type("NORMAL").value == "btree"

    def test_oracle_normal_rev(self):
        """Oracle reverse-key indexes: 'NORMAL/REV'."""
        assert _normalize_index_type("NORMAL/REV").value == "btree"

    def test_unknown_defaults_to_other(self):
        """Any unrecognized value falls back to 'other' rather than crashing."""
        assert _normalize_index_type("hyperbolic-quantum-index").value == "other"

    def test_empty_string_defaults_to_other(self):
        assert _normalize_index_type("").value == "other"


# ---------------------------------------------------------------------------
# _is_truthy: cross-engine boolean-ish string coercion
# ---------------------------------------------------------------------------


class TestIsTruthy:
    """Bug caught on production Oracle data — 90,414 of the customer's
    110,091 columns had is_nullable='Y' (Oracle single-letter convention),
    but the collector's `nullable=c.get("is_nullable", "YES") == "YES"`
    check only accepted the full-word MySQL/PostgreSQL form. Result:
    every nullable Oracle column silently got marked NOT NULL.

    _is_truthy accepts both spellings plus true/1 for defense-in-depth
    against DDL parser output.
    """

    def test_yes_returns_true(self):
        assert _is_truthy("YES") is True

    def test_y_returns_true(self):
        """Oracle single-letter — the exact case we missed."""
        assert _is_truthy("Y") is True

    def test_lowercase_yes(self):
        assert _is_truthy("yes") is True

    def test_lowercase_y(self):
        assert _is_truthy("y") is True

    def test_true_string(self):
        assert _is_truthy("true") is True

    def test_TRUE_string(self):
        assert _is_truthy("TRUE") is True

    def test_one_string(self):
        assert _is_truthy("1") is True

    def test_whitespace_stripped(self):
        assert _is_truthy(" Y ") is True
        assert _is_truthy(" YES ") is True

    def test_no_returns_false(self):
        assert _is_truthy("NO") is False

    def test_n_returns_false(self):
        """Oracle single-letter negative."""
        assert _is_truthy("N") is False

    def test_lowercase_no(self):
        assert _is_truthy("no") is False

    def test_false_string(self):
        assert _is_truthy("false") is False

    def test_zero_string(self):
        assert _is_truthy("0") is False

    def test_none_returns_false(self):
        assert _is_truthy(None) is False

    def test_empty_string_returns_false(self):
        assert _is_truthy("") is False

    def test_python_true_passthrough(self):
        assert _is_truthy(True) is True

    def test_python_false_passthrough(self):
        assert _is_truthy(False) is False

    def test_unknown_string_returns_false(self):
        """Only strict positive spellings are truthy."""
        assert _is_truthy("maybe") is False
        assert _is_truthy("2") is False


# ---------------------------------------------------------------------------
# _build_columns: Oracle nullability + identity handling
# ---------------------------------------------------------------------------


class TestBuildColumnsOracleFields:
    """The customer's Oracle JSON has is_nullable='Y'/'N' (not YES/NO)
    and is_identity='YES'/'NO' string (not boolean). Verify the builder
    produces correct Pydantic Column objects for both engine conventions.
    """

    @staticmethod
    def _base(**overrides) -> dict:
        base = {
            "column_name": "id",
            "ordinal_position": 1,
            "data_type": "number",
            "is_nullable": "N",
            "column_default": None,
            "is_identity": "NO",
        }
        base.update(overrides)
        return base

    # -- Oracle Y/N convention ------------------------------------------------

    def test_oracle_y_becomes_nullable_true(self):
        columns = _build_columns([self._base(is_nullable="Y")])
        assert columns[0].nullable is True

    def test_oracle_n_becomes_nullable_false(self):
        columns = _build_columns([self._base(is_nullable="N")])
        assert columns[0].nullable is False

    def test_oracle_lowercase_y(self):
        columns = _build_columns([self._base(is_nullable="y")])
        assert columns[0].nullable is True

    # -- MySQL/PG YES/NO convention (backward compat) -------------------------

    def test_mysql_yes_returns_nullable_true(self):
        columns = _build_columns([self._base(is_nullable="YES")])
        assert columns[0].nullable is True

    def test_mysql_no_returns_nullable_false(self):
        columns = _build_columns([self._base(is_nullable="NO")])
        assert columns[0].nullable is False

    # -- Auto-increment detection ---------------------------------------------

    def test_mysql_extra_auto_increment_detected(self):
        c = self._base(is_identity=None, extra="auto_increment")
        columns = _build_columns([c])
        assert (
            columns[0].is_auto_increment is True
        )  # nosemgrep: is-function-without-parentheses -- property, not a method

    def test_oracle_is_identity_yes_detected(self):
        """Oracle 12c+ IDENTITY column: is_identity='YES' should map."""
        columns = _build_columns([self._base(is_identity="YES")])
        assert (
            columns[0].is_auto_increment is True
        )  # nosemgrep: is-function-without-parentheses -- property, not a method

    def test_oracle_is_identity_no_returns_false(self):
        columns = _build_columns([self._base(is_identity="NO")])
        assert (
            columns[0].is_auto_increment is False
        )  # nosemgrep: is-function-without-parentheses -- property, not a method

    def test_missing_is_identity_and_extra_returns_false(self):
        c = self._base(is_identity=None)
        # No 'extra' key at all
        columns = _build_columns([c])
        assert (
            columns[0].is_auto_increment is False
        )  # nosemgrep: is-function-without-parentheses -- property, not a method

    # -- Mixed customer-JSON shape --------------------------------------------

    def test_customer_oracle_row_shape(self):
        """Exact shape observed in the customer's Oracle 19c JSON."""
        raw = [
            {
                "table_name": "access$",
                "column_name": "d_obj#",
                "ordinal_position": 1,
                "data_type": "number",
                "max_length": 22,
                "char_used": None,
                "data_precision": None,
                "data_scale": None,
                "is_nullable": "N",
                "column_default": None,
                "is_identity": "NO",
            }
        ]
        columns = _build_columns(raw)
        c = columns[0]
        assert c.column_name == "d_obj#"
        assert c.nullable is False  # N → not null
        assert (
            c.is_auto_increment is False
        )  # NO  # nosemgrep: is-function-without-parentheses -- property, not a method


# ---------------------------------------------------------------------------
# _build_tables: index_type normalization end-to-end
# ---------------------------------------------------------------------------


class TestBuildTablesIndexTypeNormalization:
    """Verify that Oracle's 'functional' index_type flows through
    _build_tables correctly after normalization.
    """

    @staticmethod
    def _table(indexes: list[dict]) -> dict:
        return {
            "table_name": "orders",
            "columns": [
                {
                    "column_name": "id",
                    "ordinal_position": 1,
                    "data_type": "number",
                    "is_nullable": "N",
                }
            ],
            "indexes": indexes,
            "foreign_keys": [],
        }

    def test_oracle_functional_index_normalized(self):
        table_dict = self._table(
            [
                {
                    "index_name": "idx_upper_email",
                    "columns": ["email"],
                    "is_unique": False,
                    "is_primary": False,
                    "index_type": "functional",
                }
            ]
        )
        tables = _build_tables([table_dict], "db")
        assert len(tables) == 1
        assert tables[0].indexes[0].index_type.value == "other"

    def test_multiple_index_types_all_normalized(self):
        table_dict = self._table(
            [
                {
                    "index_name": "idx1",
                    "columns": ["a"],
                    "is_unique": True,
                    "is_primary": False,
                    "index_type": "btree",
                },
                {
                    "index_name": "idx2",
                    "columns": ["b"],
                    "is_unique": False,
                    "is_primary": False,
                    "index_type": "functional",
                },
                {
                    "index_name": "idx3",
                    "columns": ["c"],
                    "is_unique": False,
                    "is_primary": False,
                    "index_type": "BITMAP",
                },
            ]
        )
        tables = _build_tables([table_dict], "db")
        types = [i.index_type.value for i in tables[0].indexes]
        assert types == ["btree", "other", "other"]


# ---------------------------------------------------------------------------
# _normalize_datetime_str: repair malformed SQL Server offline datetimes
# ---------------------------------------------------------------------------


class TestNormalizeDatetimeStr:
    """Bug caught on production SQL Server data (job 5cd8d882, 2026-07-04).
    The SQL Server offline JSON contained 2 datetime values (out of 1320)
    where the space between date and time was missing:

        '2026-06-1706:58:18.890'   (bad, len 22)
        '2026-06-17 06:58:18.890'  (good, len 23)

    Pydantic's datetime validator rejects the malformed form, crashing the
    collector at _build_queries. The helper detects the specific bad shape
    and inserts the missing space.
    """

    def test_none_passes_through(self):
        assert _normalize_datetime_str(None) is None

    def test_non_string_passes_through(self):
        assert _normalize_datetime_str(1234) == 1234

    def test_well_formed_datetime_unchanged(self):
        assert _normalize_datetime_str("2026-06-18 21:58:27.567") == "2026-06-18 21:58:27.567"

    def test_iso_t_separator_unchanged(self):
        assert _normalize_datetime_str("2026-06-18T21:58:27.567") == "2026-06-18T21:58:27.567"

    def test_malformed_missing_space_repaired(self):
        """The exact value that crashed on the customer's SQL Server JSON."""
        assert _normalize_datetime_str("2026-06-1706:58:18.890") == "2026-06-17 06:58:18.890"

    def test_malformed_at_boundary(self):
        assert _normalize_datetime_str("2026-01-0100:00:00.000") == "2026-01-01 00:00:00.000"

    def test_date_only_unchanged(self):
        """Just a date should pass through — no time portion to repair."""
        assert _normalize_datetime_str("2026-06-18") == "2026-06-18"

    def test_empty_string_unchanged(self):
        assert _normalize_datetime_str("") == ""

    def test_random_string_unchanged(self):
        """Doesn't mangle non-datetime strings."""
        assert _normalize_datetime_str("not-a-datetime") == "not-a-datetime"

    def test_length_22_but_not_datetime_shape(self):
        """Only triggers on the specific YYYY-MM-DDHH:MM:SS shape."""
        # Length 22 but doesn't have '-' at positions 4 and 7 → not touched
        assert _normalize_datetime_str("abcdefghij1234567890zx") == "abcdefghij1234567890zx"


# ---------------------------------------------------------------------------
# _build_columns: PostgreSQL ARRAY / USER-DEFINED keep the real type (#274)
# ---------------------------------------------------------------------------


class TestOpaquePostgresTypes:
    @staticmethod
    def _col(column_type, data_type):
        return {
            "column_name": "c",
            "data_type": data_type,
            "column_type": column_type,
            "is_nullable": "YES",
        }

    def test_array_uses_the_udt_name(self):
        assert _build_columns([self._col("ARRAY", "_int4")])[0].data_type == "_int4"

    def test_user_defined_uses_the_udt_name(self):
        assert _build_columns([self._col("USER-DEFINED", "halfvec")])[0].data_type == "halfvec"

    def test_other_types_keep_the_column_type(self):
        assert _build_columns([self._col("bigint", "int8")])[0].data_type == "bigint"
        assert (
            _build_columns([self._col("bigint unsigned", "bigint")])[0].data_type
            == "bigint unsigned"
        )


# ---------------------------------------------------------------------------
# #471: storage engine, collation and routine language reach the contract
# ---------------------------------------------------------------------------


class TestRelationalGapInputs:
    """The relational-move gap check reads what the collection reports and the
    builders used to drop: a MySQL table's storage engine and collation (from
    information_schema.TABLES) and a routine's language."""

    @staticmethod
    def _raw(**extra) -> dict:
        return {
            "table_name": "wp_posts",
            "row_count": 1,
            "data_size_mb": 0,
            "columns": [
                {
                    "column_name": "id",
                    "ordinal_position": 1,
                    "data_type": "bigint",
                    "is_nullable": "NO",
                }
            ],
            "indexes": [],
            "foreign_keys": [],
            **extra,
        }

    def test_storage_engine_and_collation_are_kept(self):
        (table,) = _build_tables(
            [self._raw(engine="MyISAM", table_collation="utf8mb3_general_ci")], "db"
        )
        assert (table.storage_engine, table.collation) == ("MyISAM", "utf8mb3_general_ci")

    @pytest.mark.parametrize("value", [None, "", "  "])
    def test_missing_values_stay_none(self, value):
        (table,) = _build_tables([self._raw(engine=value, table_collation=value)], "db")
        assert (table.storage_engine, table.collation) == (None, None)

    def test_a_table_without_them_still_builds(self):
        (table,) = _build_tables([self._raw()], "db")
        assert (table.storage_engine, table.collation) == (None, None)

    def test_the_routine_language_is_kept(self):
        from src.agents.collector.mysql_collector import _build_procedures

        procs = _build_procedures(
            [
                {"routine_name": "f", "routine_type": "FUNCTION", "language": "PLPGSQL"},
                {"routine_name": "p", "routine_type": "PROCEDURE"},
            ]
        )
        assert [p.language for p in procs or []] == ["PLPGSQL", None]
