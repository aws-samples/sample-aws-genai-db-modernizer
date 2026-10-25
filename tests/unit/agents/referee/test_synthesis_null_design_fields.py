"""Synthesis schema summaries read a null design field as no design (#757).

``dict.get(key, [])`` returns ``None`` when the key is present with a null value,
so a schema output with ``"index_designs": null`` next to a non-empty
``data_stream_designs`` (or the reverse) raised ``TypeError`` in the OpenSearch
summary. ``schema_table_defs`` already treats these fields as empty lists (#402);
the summaries must agree with it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.agents.referee.synthesis_handler import (
    _build_aurora_schema_summary,
    _build_documentdb_schema_summary,
    _build_elasticache_schema_summary,
    _build_opensearch_schema_summary,
    _build_schema_summaries,
    _build_standard_schema_summary,
)
from src.agents.referee.synthesis_report import schema_table_defs

INDEX = {
    "index_name": "posts",
    "source_tables": ["wordpress.wp_posts"],
    "settings": {"number_of_shards": 2, "number_of_replicas": 1},
    "field_mappings": [{"name": "title"}],
}
STREAM = {
    "data_stream_name": "logs",
    "source_tables": ["wordpress.wp_actionscheduler_logs"],
    "ism_policy": {"hot_phase_days": 7, "delete_after_days": 30},
    "index_template": {"settings": {"number_of_shards": 1, "number_of_replicas": 1}},
}


def _names(summary: dict) -> list[str]:
    return [t["table_name"] for t in summary["tables"]]


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"index_designs": None, "data_stream_designs": [STREAM]}, ["logs"]),
        ({"index_designs": [INDEX], "data_stream_designs": None}, ["posts"]),
        ({"index_designs": [INDEX], "data_stream_designs": [STREAM]}, ["posts", "logs"]),
    ],
)
def test_opensearch_summary_reads_a_null_list_as_empty(schema: dict, expected: list[str]) -> None:
    summary = _build_opensearch_schema_summary(schema)
    assert summary["status"] == "completed"
    assert _names(summary) == expected
    # The summary and schema_table_defs name the same designs.
    assert _names(summary) == [t["table_name"] for t in schema_table_defs("opensearch", schema)]


@pytest.mark.parametrize(
    "schema",
    [
        {"index_designs": None, "data_stream_designs": None},
        {"index_designs": [], "data_stream_designs": None},
        {},
    ],
)
def test_opensearch_summary_with_no_design_is_not_available(schema: dict) -> None:
    assert _build_opensearch_schema_summary(schema) == {"status": "not_available"}


def test_opensearch_summary_reads_null_nested_settings_as_defaults() -> None:
    index = {**INDEX, "settings": None, "field_mappings": None}
    stream = {**STREAM, "ism_policy": None, "index_template": None}
    summary = _build_opensearch_schema_summary(
        {"index_designs": [index], "data_stream_designs": [stream], "access_patterns": None}
    )
    posts, logs = summary["tables"]
    assert (posts["shards"], posts["replicas"], posts["field_count"]) == (0, 1, 0)
    assert (logs["shards"], logs["ism_hot_days"]) == (0, None)
    assert summary["access_pattern_count"] == 0


def test_elasticache_summary_reads_null_lists_as_empty() -> None:
    summary = _build_elasticache_schema_summary({"key_designs": None, "access_patterns": None})
    assert summary["tables"] == []
    assert summary["access_pattern_count"] == 0


def test_documentdb_summary_reads_null_lists_as_empty() -> None:
    summary = _build_documentdb_schema_summary({"collections": None, "access_patterns": None})
    assert summary["tables"] == []
    assert summary["access_pattern_count"] == 0


def test_schema_summaries_survive_null_design_fields_for_every_engine() -> None:
    def engine(schema: dict) -> SimpleNamespace:
        return SimpleNamespace(schema_design=schema)

    data = SimpleNamespace(
        engines={
            "opensearch": engine({"index_designs": None, "data_stream_designs": [STREAM]}),
            "elasticache": engine({"key_designs": None}),
            "documentdb": engine({"collections": None}),
        }
    )
    summaries = _build_schema_summaries(data)
    assert _names(summaries["opensearch"]) == ["logs"]
    assert summaries["elasticache"] == {"status": "not_available"}
    assert summaries["documentdb"] == {"status": "not_available"}


def test_documentdb_summary_reads_a_null_collection_index_list_as_empty() -> None:
    summary = _build_documentdb_schema_summary(
        {"collections": [{"collection_name": "c", "indexes": None}]}
    )
    assert summary["tables"][0]["gsi_count"] == 0


@pytest.mark.parametrize("engine", ["aurora_mysql", "aurora_postgresql"])
def test_aurora_summary_reads_null_lists_as_empty(engine: str) -> None:
    """#1094: Aurora's top-level and per-table lists."""
    table = {
        "table_name": "t",
        "columns": None,
        "indexes": None,
        "foreign_keys": None,
        "primary_key": None,
    }
    summary = _build_aurora_schema_summary(
        engine, {"table_definitions": [table], "access_patterns": None}
    )
    (row,) = summary["tables"]
    assert (row["column_count"], row["index_count"], row["foreign_key_count"]) == (0, 0, 0)
    assert row["primary_key"] == []
    assert (summary["index_count"], summary["foreign_key_count"]) == (0, 0)
    assert summary["access_pattern_count"] == 0


def test_aurora_summary_counts_non_null_lists() -> None:
    table = {"table_name": "t", "columns": [{}, {}], "indexes": [{}], "foreign_keys": [{}]}
    summary = _build_aurora_schema_summary(
        "aurora_mysql", {"table_definitions": [table], "access_patterns": [{}]}
    )
    (row,) = summary["tables"]
    assert (row["column_count"], row["index_count"], row["foreign_key_count"]) == (2, 1, 1)
    assert summary["access_pattern_count"] == 1


def test_dynamodb_summary_reads_null_lists_as_empty() -> None:
    """#1094: the DynamoDB (standard) summary's lists."""
    summary = _build_standard_schema_summary(
        {
            "table_definitions": [{"table_name": "t", "gsis": None, "source_tables": None}],
            "access_patterns": None,
            "hot_partition_analysis": None,
        }
    )
    (row,) = summary["tables"]
    assert row["gsi_count"] == 0
    assert row["source_tables"] == []
    assert (summary["access_pattern_count"], summary["hot_partitions_at_risk"]) == (0, 0)


def test_dynamodb_summary_counts_non_null_lists() -> None:
    summary = _build_standard_schema_summary(
        {
            "table_definitions": [{"table_name": "t", "gsis": [{}, {}]}],
            "access_patterns": [{}],
            "hot_partition_analysis": [{"at_risk": True}, {"at_risk": False}],
        }
    )
    assert summary["tables"][0]["gsi_count"] == 2
    assert (summary["access_pattern_count"], summary["hot_partitions_at_risk"]) == (1, 1)


def test_field_helpers_read_missing_and_null_as_empty_and_keep_values() -> None:
    from src.agents.referee.schema_fields import field_dict, field_list

    assert field_list({"k": None}, "k") == []
    assert field_list({}, "k") == []
    assert field_list(None, "k") == []
    value = [1]
    assert field_list({"k": value}, "k") is value
    assert field_list({"k": []}, "k") == []
    assert field_dict({"k": None}, "k") == {}
    assert field_dict(None, "k") == {}
    assert field_dict({"k": {"a": 1}}, "k") == {"a": 1}


def test_effective_architecture_reads_a_null_group_pattern_list_as_empty() -> None:
    from src.agents.referee.synthesis_grounding import build_effective_architecture

    out = build_effective_architecture(
        {"dynamodb": ["wordpress.wp_posts"]},
        [],
        [{"target": "dynamodb"}],
        [{"group_name": "g", "access_patterns": None}],
        "wordpress",
        {},
    )
    assert [e["engine"] for e in out["engines"]] == ["dynamodb"]
    assert out["engines"][0]["top_query_groups"] == []


def test_standard_summary_reports_the_keys_only_fetch_the_check_priced() -> None:
    """#1503: synthesis carries the base-table fetch behind KEYS_ONLY GSI reads."""
    summary = _build_standard_schema_summary(
        {
            "table_definitions": [
                {
                    "table_name": "Posts",
                    "item_size_bytes": 1000,
                    "gsis": [{"gsi_name": "ByTag", "projection": "KEYS_ONLY"}],
                }
            ],
            "access_patterns": [
                {
                    "pattern_id": "DDB-AP-1",
                    "operation": "Query",
                    "table_name": "Posts",
                    "gsi_name": "ByTag",
                    "design_rps": 10.0,
                    "avg_items_returned": 20,
                }
            ],
            "hot_partition_analysis": [],
        }
    )

    assert summary["keys_only_fetch"] == [
        {
            "table_name": "Posts",
            "gsi_name": "ByTag",
            "projection": "KEYS_ONLY",
            "pattern_ids": ["DDB-AP-1"],
            "assumed_full_item_pattern_ids": ["DDB-AP-1"],
            "unprojected_attributes": [],
            "extra_reads_per_second": 200.0,
            "extra_read_units_per_second": 100.0,
        }
    ]
