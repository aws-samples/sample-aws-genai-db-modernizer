# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Unit tests for the ingestion table and query caps (#142, #384).

A pathological offline collection can drive excessive memory, processing time,
and LLM/compute cost before the pipeline ever reaches analysis. There is no file
size cap (#384, 2026-11-03: "it takes what it takes"); the query cap is 250,000. ``src/tools/validation/ingestion_limits.py`` is the one place every
ingestion path (the local API upload, ``run_assessment.py`` /
``run_collect.py --file``, and the AWS Transform upload path) checks a
collection against configurable limits.
"""

from __future__ import annotations

import pytest

from src.tools.validation import ingestion_limits
from src.tools.validation.ingestion_limits import (
    DEFAULT_MAX_QUERIES,
    DEFAULT_MAX_TABLES,
    IngestionLimitError,
    IngestionLimits,
    check_collection_counts,
)


class TestIngestionLimitsFromEnv:
    @pytest.mark.passes_on_main
    def test_defaults_when_env_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODERNIZER_MAX_UPLOAD_BYTES", raising=False)
        monkeypatch.delenv("MODERNIZER_MAX_UPLOAD_TABLES", raising=False)
        monkeypatch.delenv("MODERNIZER_MAX_UPLOAD_QUERIES", raising=False)

        limits = IngestionLimits.from_env()

        assert limits.max_tables == DEFAULT_MAX_TABLES
        assert limits.max_queries == DEFAULT_MAX_QUERIES

    def test_the_default_query_cap_is_250000(self) -> None:
        # The maintainer's decision on #384 (2026-11-03).
        assert DEFAULT_MAX_QUERIES == 250_000
        assert IngestionLimits().max_queries == 250_000

    @pytest.mark.passes_on_main
    def test_default_table_cap_is_generous(self) -> None:
        assert DEFAULT_MAX_TABLES == 50_000

    def test_there_is_no_file_size_limit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The variable that used to set it is not read any more (#384).
        monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_BYTES", "10")
        assert not hasattr(ingestion_limits, "check_file_size")
        assert not hasattr(IngestionLimits.from_env(), "max_file_bytes")

    @pytest.mark.passes_on_main
    def test_env_vars_override_defaults(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_TABLES", "7")
        monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_QUERIES", "9")

        limits = IngestionLimits.from_env()

        assert limits.max_tables == 7
        assert limits.max_queries == 9

    @pytest.mark.passes_on_main
    def test_invalid_env_value_falls_back_to_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_QUERIES", "not-a-number")

        limits = IngestionLimits.from_env()

        assert limits.max_queries == DEFAULT_MAX_QUERIES

    def test_non_positive_env_value_falls_back_to_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_TABLES", "0")

        limits = IngestionLimits.from_env()

        assert limits.max_tables == DEFAULT_MAX_TABLES

    @pytest.mark.passes_on_main
    def test_invalid_env_value_logs_a_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_QUERIES", "not-a-number")

        with caplog.at_level("WARNING", logger="src.tools.validation.ingestion_limits"):
            IngestionLimits.from_env()

        assert any("MODERNIZER_MAX_UPLOAD_QUERIES" in r.message for r in caplog.records)

    def test_non_positive_env_value_logs_a_warning(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_TABLES", "-5")

        with caplog.at_level("WARNING", logger="src.tools.validation.ingestion_limits"):
            IngestionLimits.from_env()

        assert any("MODERNIZER_MAX_UPLOAD_TABLES" in r.message for r in caplog.records)


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


class TestTheQueryCap:
    def test_20001_queries_are_accepted_by_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MODERNIZER_MAX_UPLOAD_QUERIES", raising=False)
        check_collection_counts(table_count=1, query_count=20_001, source="c.json")

    def test_250000_queries_are_accepted_and_250001_refused_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MODERNIZER_MAX_UPLOAD_QUERIES", raising=False)
        check_collection_counts(table_count=1, query_count=250_000, source="c.json")
        with pytest.raises(IngestionLimitError, match="250,000-query ingestion limit"):
            check_collection_counts(table_count=1, query_count=250_001, source="c.json")

    @pytest.mark.passes_on_main
    def test_the_environment_variable_raises_and_lowers_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_QUERIES", "300000")
        check_collection_counts(table_count=1, query_count=250_001, source="c.json")
        monkeypatch.setenv("MODERNIZER_MAX_UPLOAD_QUERIES", "100")
        with pytest.raises(IngestionLimitError, match="MODERNIZER_MAX_UPLOAD_QUERIES"):
            check_collection_counts(table_count=1, query_count=101, source="c.json")
