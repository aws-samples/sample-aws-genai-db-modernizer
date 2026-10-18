"""Declaring the engines a source runs today (#963): parsing, merging, validation."""

from __future__ import annotations

import pytest

from src.shared.engines_in_use import merge_engines_in_use, parse_engines_in_use_option


class TestParseOption:
    def test_comma_separated_ids_are_lowercased_and_deduplicated(self) -> None:
        assert parse_engines_in_use_option("OpenSearch, elasticache ,opensearch") == [
            "opensearch",
            "elasticache",
        ]

    def test_empty_and_none_declare_nothing(self) -> None:
        assert parse_engines_in_use_option(None) == []
        assert parse_engines_in_use_option("") == []
        assert parse_engines_in_use_option(" , ") == []

    def test_an_unknown_engine_is_refused_and_the_known_ones_are_named(self) -> None:
        with pytest.raises(ValueError) as exc:
            parse_engines_in_use_option("opensearch,redis")
        assert "redis" in str(exc.value)
        assert "elasticache" in str(exc.value)

    def test_a_placeholder_engine_is_refused(self) -> None:
        # neptune and keyspaces are catalog placeholders: never a plan target.
        with pytest.raises(ValueError):
            parse_engines_in_use_option("neptune")

    def test_a_source_engine_id_is_not_an_aws_target(self) -> None:
        with pytest.raises(ValueError):
            parse_engines_in_use_option("mysql")


class TestMerge:
    def test_keeps_order_and_drops_duplicates(self) -> None:
        assert merge_engines_in_use(["opensearch"], ["elasticache", "opensearch"]) == [
            "opensearch",
            "elasticache",
        ]

    def test_ignores_anything_but_a_list_of_strings(self) -> None:
        assert merge_engines_in_use(None, "opensearch", [3, None, ""], ["documentdb"]) == [
            "documentdb"
        ]

    def test_nothing_in_nothing_out(self) -> None:
        assert merge_engines_in_use() == []


def test_the_legacy_aurora_id_is_refused_with_the_two_real_ids() -> None:
    with pytest.raises(ValueError) as exc:
        parse_engines_in_use_option("opensearch,aurora")
    assert "aurora_mysql" in str(exc.value) and "aurora_postgresql" in str(exc.value)


def test_the_two_real_aurora_ids_are_accepted() -> None:
    assert parse_engines_in_use_option("aurora_mysql,aurora_postgresql") == [
        "aurora_mysql",
        "aurora_postgresql",
    ]
