"""Unit tests for offline_parser.py.

Focused on hardening changes from the Oracle production JSON test run:
- Whitespace-tolerant sentinel-object regex (handles both inline and
  DBMS_OUTPUT-style multi-line emission).
"""

import json
from unittest.mock import MagicMock, patch

import pytest


class TestSentinelRegex:
    """The two `.replace()` calls previously used could not handle
    Oracle's DBMS_OUTPUT.PUT_LINE emission style, which puts the sentinel
    object on its own line (,\\n{"_sentinel": true}\\n). The new
    whitespace-tolerant regex must handle both forms.
    """

    @staticmethod
    def _run_fetch(body_text: str) -> dict:
        """Invoke fetch_offline_json with a mocked S3 body."""
        from src.tools.database.offline_parser import fetch_offline_json

        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {
            "Body": MagicMock(read=lambda: body_text.encode("utf-8"))
        }
        with patch("src.tools.database.offline_parser.boto3.client", return_value=mock_s3):
            return fetch_offline_json("bucket", "key")

    def test_inline_sentinel_stripped(self):
        content = '{"tables":[{"table_name":"t1"},{"_sentinel":true}]}'
        result = self._run_fetch(content)
        assert result["tables"] == [{"table_name": "t1"}]

    def test_inline_sentinel_with_space_stripped(self):
        # Legacy form the old code handled
        content = '{"tables":[{"table_name":"t1"},{"_sentinel": true}]}'
        result = self._run_fetch(content)
        assert result["tables"] == [{"table_name": "t1"}]

    def test_multiline_sentinel_stripped(self):
        """Oracle DBMS_OUTPUT emits this multi-line pattern."""
        content = '{"tables":[\n' '{"table_name":"t1"},\n' '{"_sentinel": true}\n' "]}"
        result = self._run_fetch(content)
        assert result["tables"] == [{"table_name": "t1"}]

    def test_sentinel_only_element_stripped(self):
        """Real Oracle emission pattern for an empty result set: the
        sentinel is the ONLY element in the array. The regex must
        handle this too (no leading comma).
        """
        content = '{"triggers":[\n{"_sentinel": true}\n]}'
        result = self._run_fetch(content)
        assert result["triggers"] == []

    def test_nested_object_with_sentinel_field_preserved(self):
        """Guard against false-positive: a legitimate nested object that
        happens to have a `_sentinel: true` field (not at end of array)
        must NOT be stripped.
        """
        content = '{"config":{"_sentinel":true}}'
        result = self._run_fetch(content)
        # Nested object preserved — lookahead requires `]` after sentinel
        assert result["config"] == {"_sentinel": True}

    def test_control_chars_stripped(self):
        """Oracle 19c JSON_OBJECT does not escape control chars — the
        parser strips them (except \\n) before json.loads. Unchanged
        behavior, but sanity-check the ordering: control-char strip must
        happen before the sentinel regex.
        """
        # \x00 embedded in a string value would break json.loads
        content = '{"tables":[{"table_name":"t\x001"}]}'
        result = self._run_fetch(content)
        # Control char became space, JSON still parses
        assert result["tables"] == [{"table_name": "t 1"}]

    def test_multiple_sentinels_across_sections(self):
        """Real Oracle output has one sentinel per array (tables, columns,
        indexes, ...). All should be stripped in a single pass.
        """
        content = (
            "{"
            '"tables":[{"table_name":"t1"},\n{"_sentinel": true}\n],'
            '"columns":[{"table_name":"t1","column_name":"c1"},\n{"_sentinel": true}\n],'
            '"triggers":[\n{"_sentinel": true}\n]'
            "}"
        )
        result = self._run_fetch(content)
        assert result["tables"] == [{"table_name": "t1"}]
        assert result["columns"] == [{"table_name": "t1", "column_name": "c1"}]
        assert result["triggers"] == []

    def test_produces_valid_json(self):
        """After sentinel stripping, the content must be valid JSON."""
        content = '{"tables":[\n' '{"table_name":"t1"},\n' '{"_sentinel": true}\n' "]}"
        result = self._run_fetch(content)
        # Round-trip through json to prove it's a real dict
        assert json.loads(json.dumps(result))["tables"] == [{"table_name": "t1"}]


class TestMergeQueryVariants:
    """Regression for #386: offline collections can hold multiple rows for
    one query shape (SQL Server literal variants in particular, but this is
    a safety net applied on every engine). Rows sharing a ``digest`` must be
    merged into one row before patterns are built.
    """

    @staticmethod
    def _variant(**overrides: object) -> dict:
        base = {
            "digest": "shape-1",
            "query_text": "SELECT * FROM Person.StateProvince WHERE CountryRegionCode = 'FR'",
            "execution_count": 10,
            "total_time_ms": 100.0,
            "avg_time_ms": 10.0,
            "min_time_ms": 8.0,
            "max_time_ms": 12.0,
            "total_rows_sent": 100,
            "total_rows_examined": 200,
            "total_rows_affected": 0,
            "first_seen": "2026-01-01 00:00:00",
            "last_seen": "2026-01-02 00:00:00",
        }
        base.update(overrides)
        return base

    def test_literal_variants_merge_into_one_pattern(self) -> None:
        from src.tools.database.offline_parser import _transform_queries

        raw = [
            self._variant(query_text="... CountryRegionCode = 'FR'"),
            self._variant(query_text="... CountryRegionCode = 'US'"),
            self._variant(query_text="... CountryRegionCode = 'DE'"),
        ]
        patterns = _transform_queries(raw, "adventureworks", set())
        assert len(patterns) == 1
        assert patterns[0]["query_id"] == "shape-1"

    def test_execution_count_and_total_time_are_summed(self) -> None:
        from src.tools.database.offline_parser import _transform_queries

        raw = [
            self._variant(execution_count=10, total_time_ms=100.0),
            self._variant(execution_count=20, total_time_ms=300.0),
        ]
        patterns = _transform_queries(raw, "db", set())
        assert patterns[0]["execution_count"] == 30
        assert patterns[0]["total_time_ms"] == pytest.approx(400.0)

    def test_average_is_recomputed_from_merged_totals(self) -> None:
        from src.tools.database.offline_parser import _transform_queries

        raw = [
            self._variant(execution_count=10, total_time_ms=100.0, avg_time_ms=10.0),
            self._variant(execution_count=20, total_time_ms=300.0, avg_time_ms=15.0),
        ]
        patterns = _transform_queries(raw, "db", set())
        # 400ms total / 30 executions = 13.33ms, NOT a simple average of the
        # per-variant avg_time_ms values (10 and 15).
        assert patterns[0]["execution_time_ms_avg"] == pytest.approx(400.0 / 30)

    def test_min_and_max_take_extremes_across_variants(self) -> None:
        from src.tools.database.offline_parser import _transform_queries

        raw = [
            self._variant(min_time_ms=8.0, max_time_ms=12.0),
            self._variant(min_time_ms=3.0, max_time_ms=50.0),
        ]
        patterns = _transform_queries(raw, "db", set())
        assert patterns[0]["execution_time_ms_min"] == pytest.approx(3.0)
        assert patterns[0]["execution_time_ms_max"] == pytest.approx(50.0)

    def test_rows_sent_examined_affected_are_summed(self) -> None:
        from src.tools.database.offline_parser import _transform_queries

        raw = [
            self._variant(
                execution_count=10,
                total_rows_sent=100,
                total_rows_examined=200,
                total_rows_affected=5,
            ),
            self._variant(
                execution_count=10,
                total_rows_sent=50,
                total_rows_examined=80,
                total_rows_affected=2,
            ),
        ]
        patterns = _transform_queries(raw, "db", set())
        p = patterns[0]
        assert p["rows_returned_avg"] == pytest.approx(150 / 20)
        assert p["rows_examined_avg"] == pytest.approx(280 / 20)
        assert p["rows_affected_avg"] == pytest.approx(7 / 20)

    def test_first_seen_min_and_last_seen_max(self) -> None:
        from src.tools.database.offline_parser import _transform_queries

        raw = [
            self._variant(first_seen="2026-03-01 00:00:00", last_seen="2026-03-05 00:00:00"),
            self._variant(first_seen="2026-01-15 00:00:00", last_seen="2026-06-01 00:00:00"),
        ]
        patterns = _transform_queries(raw, "db", set())
        assert patterns[0]["first_seen"] == "2026-01-15 00:00:00"
        assert patterns[0]["last_seen"] == "2026-06-01 00:00:00"

    def test_keeps_representative_text_from_costliest_variant(self) -> None:
        from src.tools.database.offline_parser import _transform_queries

        raw = [
            self._variant(query_text="... CountryRegionCode = 'FR'", total_time_ms=50.0),
            self._variant(query_text="... CountryRegionCode = 'US'", total_time_ms=900.0),
            self._variant(query_text="... CountryRegionCode = 'DE'", total_time_ms=10.0),
        ]
        patterns = _transform_queries(raw, "db", set())
        assert patterns[0]["query_text"] == "... CountryRegionCode = 'US'"

    def test_pattern_order_is_deterministic_by_first_appearance(self) -> None:
        from src.tools.database.offline_parser import _transform_queries

        raw = [
            self._variant(digest="shape-b", query_text="SELECT b"),
            self._variant(digest="shape-a", query_text="SELECT a v1"),
            self._variant(digest="shape-b", query_text="SELECT b v2"),
            self._variant(digest="shape-a", query_text="SELECT a v2"),
            self._variant(digest="shape-c", query_text="SELECT c"),
        ]
        patterns = _transform_queries(raw, "db", set())
        assert [p["query_id"] for p in patterns] == ["shape-b", "shape-a", "shape-c"]

    def test_rows_without_shared_digest_are_not_merged(self) -> None:
        from src.tools.database.offline_parser import _transform_queries

        raw = [
            self._variant(digest="shape-a"),
            self._variant(digest="shape-b"),
        ]
        patterns = _transform_queries(raw, "db", set())
        assert len(patterns) == 2
        assert {p["query_id"] for p in patterns} == {"shape-a", "shape-b"}

    def test_logs_how_many_rows_were_merged(self, caplog) -> None:
        from src.tools.database.offline_parser import _transform_queries

        raw = [
            self._variant(query_text="... 'FR'"),
            self._variant(query_text="... 'US'"),
            self._variant(query_text="... 'DE'"),
        ]
        with caplog.at_level("INFO", logger="src.tools.database.offline_parser"):
            _transform_queries(raw, "db", set())
        assert any("merged" in record.message for record in caplog.records)
        assert any("2" in record.message for record in caplog.records)


class TestUpsertTableExtraction:
    """ON DUPLICATE KEY UPDATE / DO UPDATE must not be treated as table refs."""

    def test_duplicate_key_update_column_is_not_a_table(self):
        from src.tools.database.offline_parser import _transform_queries

        sql = (
            "INSERT INTO `wp_options` (`option_name`, `option_value`) VALUES (...)"
            " ON DUPLICATE KEY UPDATE `option_name` = VALUES(`option_name`)"
        )
        result = _transform_queries([{"query_text": sql}], "wordpress", {"wp_options"})
        assert result[0]["tables_accessed"] == ["wordpress.wp_options"]

    def test_real_update_still_extracts_table(self):
        from src.tools.database.offline_parser import _transform_queries

        sql = "UPDATE `wp_options` SET `option_value` = 'x' WHERE `option_name` = 'siteurl'"
        result = _transform_queries([{"query_text": sql}], "wordpress", {"wp_options"})
        assert result[0]["tables_accessed"] == ["wordpress.wp_options"]

    def test_conflict_do_update_does_not_capture_set(self):
        from src.tools.database.offline_parser import _transform_queries

        sql = (
            'INSERT INTO "posts" ("id", "title") VALUES (1, \'a\')'
            ' ON CONFLICT ("id") DO UPDATE SET "title" = EXCLUDED."title"'
        )
        result = _transform_queries([{"query_text": sql}], "blog", {"posts"})
        assert result[0]["tables_accessed"] == ["blog.posts"]
