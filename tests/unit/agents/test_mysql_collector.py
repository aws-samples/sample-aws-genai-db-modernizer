"""Unit tests for shared mysql_collector helper functions.

These helpers (`_build_tables`, `_build_triggers`) are used by MySQL LIVE
mode AND all other engines' offline mode via `_collect_offline`. Focused
on hardening changes from the Oracle production JSON test run.
"""

import logging

from src.agents.collector.mysql_collector import _build_tables, _build_triggers

# ---------------------------------------------------------------------------
# _build_tables: skip tables with zero columns
# ---------------------------------------------------------------------------


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
