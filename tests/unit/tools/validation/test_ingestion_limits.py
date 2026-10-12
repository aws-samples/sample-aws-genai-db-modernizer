"""Unit tests for ingestion size/row caps (#142).

A pathological or oversized offline collection can drive excessive memory,
processing time, and LLM/compute cost before the pipeline ever reaches
analysis. ``src/tools/validation/ingestion_limits.py`` is the one place every
ingestion path (the local API upload, ``run_assessment.py`` /
``run_collect.py --file``, and the AWS Transform upload path) checks a
collection against configurable limits.
"""

from __future__ import annotations

import pytest

from src.tools.validation.ingestion_limits import (
    DEFAULT_MAX_FILE_BYTES,
    DEFAULT_MAX_QUERIES,
    DEFAULT_MAX_TABLES,
    IngestionLimitError,
    IngestionLimits,
    check_collection_counts,
    check_file_size,
)


class TestIngestionLimitsFromEnv:
    def test_defaults_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODERNIZER_MAX_UPLOAD_BYTES", raising=False)
        monkeypatch.delenv("MODERNIZER_MAX_UPLOAD_TABLES", raising=False)
        monkeypatch.delenv("MODERNIZER_MAX_UPLOAD_QUERIES", raising=False)

        limits = IngestionLimits.from_env()

        assert limits.max_file_bytes == DEFAULT_MAX_FILE_BYTES
        assert limits.max_tables == DEFAULT_MAX_TABLES
        assert limits.max_queries == DEFAULT_MAX_QUERIES

    def test_defaults_are_generous_enough_for_10000_query_collections(self) -> None:
        # #384 proposes raising every collector's query cap to 10,000 by default;
        # the ingestion ceiling must not reject that collection.
        limits = IngestionLimits()
        assert limits.max_queries >= 10_000

    def test_default_table_cap_is_generous(self) -> None:
        # Round-1 review (#142, I3): the byte cap is the real memory guard, so
        # the table-count ceiling stays generous rather than tight.
        assert DEFAULT_MAX_TABLES == 50_000

    def test_env_vars_override_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_BYTES", "123")
        monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_TABLES", "7")
        monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_QUERIES", "9")

        limits = IngestionLimits.from_env()

        assert limits.max_file_bytes == 123
        assert limits.max_tables == 7
        assert limits.max_queries == 9

    def test_invalid_env_value_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_BYTES", "not-a-number")

        limits = IngestionLimits.from_env()

        assert limits.max_file_bytes == DEFAULT_MAX_FILE_BYTES

    def test_non_positive_env_value_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_TABLES", "0")

        limits = IngestionLimits.from_env()

        assert limits.max_tables == DEFAULT_MAX_TABLES

    def test_invalid_env_value_logs_a_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_BYTES", "not-a-number")

        with caplog.at_level("WARNING", logger="src.tools.validation.ingestion_limits"):
            IngestionLimits.from_env()

        assert any("MODERNIZER_MAX_UPLOAD_BYTES" in r.message for r in caplog.records)

    def test_non_positive_env_value_logs_a_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_TABLES", "-5")

        with caplog.at_level("WARNING", logger="src.tools.validation.ingestion_limits"):
            IngestionLimits.from_env()

        assert any("MODERNIZER_MAX_UPLOAD_TABLES" in r.message for r in caplog.records)


class TestCheckFileSize:
    def test_passes_under_limit(self) -> None:
        check_file_size(10, source="test.json", limits=IngestionLimits(max_file_bytes=100))

    def test_passes_at_exact_limit(self) -> None:
        check_file_size(100, source="test.json", limits=IngestionLimits(max_file_bytes=100))

    def test_raises_clear_message_over_limit(self) -> None:
        with pytest.raises(IngestionLimitError) as exc_info:
            check_file_size(101, source="test.json", limits=IngestionLimits(max_file_bytes=100))

        message = str(exc_info.value)
        assert "test.json" in message
        assert "101" in message
        assert "100" in message
        assert "MODERNIZER_MAX_UPLOAD_BYTES" in message


class TestCheckCollectionCounts:
    def test_passes_under_both_limits(self) -> None:
        check_collection_counts(
            table_count=1,
            query_count=1,
            source="test.json",
            limits=IngestionLimits(max_tables=10, max_queries=10),
        )

    def test_raises_clear_message_when_tables_exceed_limit(self) -> None:
        with pytest.raises(IngestionLimitError) as exc_info:
            check_collection_counts(
                table_count=11,
                query_count=1,
                source="test.json",
                limits=IngestionLimits(max_tables=10, max_queries=10),
            )

        message = str(exc_info.value)
        assert "test.json" in message
        assert "11" in message
        assert "table" in message
        assert "MODERNIZER_MAX_UPLOAD_TABLES" in message

    def test_raises_clear_message_when_queries_exceed_limit(self) -> None:
        with pytest.raises(IngestionLimitError) as exc_info:
            check_collection_counts(
                table_count=1,
                query_count=11,
                source="test.json",
                limits=IngestionLimits(max_tables=10, max_queries=10),
            )

        message = str(exc_info.value)
        assert "test.json" in message
        assert "11" in message
        assert "quer" in message
        assert "MODERNIZER_MAX_UPLOAD_QUERIES" in message
