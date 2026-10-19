"""Schema-design arithmetic computed in code, not by the model (#626).

Every expected value below is worked out by hand in the comment next to it, so
a change to a rule shows up as a changed number here rather than a silently
different figure in a design. The second half checks that a design whose
numbers disagree is rejected on every path: the comparison itself, the
DynamoDB cost check, external-mode ``--finalize``, and the Bedrock runner's
correction turns.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from src.contracts.design_metrics import DesignMetricsContract
from src.tools.schema.design_metrics import (
    avg_row_size_bytes,
    build_design_metrics,
    check_design_numbers,
    check_hot_partition_entry,
    design_metrics_from_collector,
    design_metrics_from_projection,
    design_rps,
    schema_item_size_bytes,
    shard_count,
)

# ---------------------------------------------------------------------------
# A tiny workload, hand-checked
# ---------------------------------------------------------------------------

# users: 1 MB over 512 rows = 1,048,576 / 512 = 2048 bytes = 2.0 KB a row.
# posts: 2 MB over 2048 rows = 2,097,152 / 2048 = 1024 bytes = 1.0 KB a row,
#        2048 / 512 = 4.0 posts per user, so embedding adds 4 × 1.0 = 4.0 KB.
# tags:  size unknown, 10 rows: 0.01 MB stands in, 10,485.76 / 10 = 1048.576 -> 1049 bytes.
# events: 45 GB = 46,080 MB.
TABLES: list[dict[str, Any]] = [
    {
        "table_id": "db.users",
        "table_name": "users",
        "row_count": 512,
        "size_mb": 1.0,
        "columns": [
            {"column_name": "id", "normalized_data_type": "integer", "nullable": False},
            {
                "column_name": "email",
                "normalized_data_type": "string",
                "max_length": 255,
                "nullable": False,
            },
            {"column_name": "active", "normalized_data_type": "boolean", "nullable": False},
            {"column_name": "bio", "normalized_data_type": "text", "nullable": True},
        ],
    },
    {
        "table_id": "db.posts",
        "table_name": "posts",
        "row_count": 2048,
        "size_mb": 2.0,
        "columns": [{"column_name": "id", "normalized_data_type": "integer", "nullable": False}],
        "foreign_keys": [
            {
                "constraint_name": "fk_user",
                "columns": ["user_id"],
                "referenced_table": "users",
                "referenced_columns": ["id"],
            }
        ],
    },
    {"table_id": "db.tags", "table_name": "tags", "row_count": 10, "size_mb": None, "columns": []},
    {
        "table_id": "db.events",
        "table_name": "events",
        "row_count": 1000,
        "size_mb": 46080.0,
        "columns": [],
    },
]

# q1: 0.5 calls/s × 3 × 10 = 15.0. q2: no calls_per_second, 7200/h = 2/s -> 60.0.
# q3: OTHER, 1 call an hour: 1/3600 × 30 = 0.008333... -> 0.0083.
QUERIES: list[dict[str, Any]] = [
    {"query_id": "q1", "query_type": "SELECT", "calls_per_second": 0.5, "frequency_per_hour": 1800},
    {
        "query_id": "q2",
        "query_type": "INSERT",
        "calls_per_second": None,
        "frequency_per_hour": 7200,
    },
    {"query_id": "q3", "query_type": "OTHER", "calls_per_second": None, "frequency_per_hour": 1},
]


def _collector(total: int | None = 4) -> dict:
    return {
        "database_schema": {"tables": TABLES},
        "queries": {"query_patterns": QUERIES, "total_queries_analyzed": total},
    }


def _metrics(engine: str, total: int | None = 4) -> dict:
    return design_metrics_from_collector(engine, _collector(total))


# ---------------------------------------------------------------------------
# The computations
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cps,per_hour,expected",
    [
        (0.5, 1800, 15.0),  # calls_per_second wins over the hourly figure
        (None, 7200, 60.0),  # 7200 / 3600 = 2/s × 30
        (None, 1, 0.0083),  # 1/3600 × 30 = 0.00833
        (0.0, 7200, 0.0),  # a measured zero is a zero, not a fallback to the hourly count
        (None, 0, 0.0),
    ],
)
def test_design_rps(cps, per_hour, expected):
    assert design_rps(cps, per_hour) == expected


def test_design_rps_uses_the_context_multipliers():
    assert design_rps(2.0, None, peak_to_avg_ratio=2.0, growth_multiplier=1.5) == 6.0


@pytest.mark.parametrize(
    "size_mb,rows,expected",
    [
        (1.0, 512, (2048, False)),
        (2.0, 2048, (1024, False)),
        (None, 10, (1049, True)),  # 0.01 MB stand-in: 10,485.76 / 10
        (1.0, 0, (1048576, False)),  # an empty table divides by 1
    ],
)
def test_avg_row_size_bytes(size_mb, rows, expected):
    assert avg_row_size_bytes(size_mb, rows) == expected


def test_schema_item_size_bytes():
    # id: 2 + 8, email: 5 + 255, active: 6 + 1, bio (no max_length): 3 + 50 -> 330
    assert schema_item_size_bytes(TABLES[0]["columns"]) == 330


def test_long_text_columns_count_at_most_one_kilobyte():
    # LONGTEXT reports max_length 4,294,967,295; it counts as 1024: 7 + 1024 = 1031
    column = {"column_name": "content", "normalized_data_type": "text", "max_length": 4294967295}
    assert schema_item_size_bytes([column]) == 1031


@pytest.mark.parametrize(
    "data_gb,max_gb,nodes,expected",
    [
        (0.1, 30, 3, 1),  # small dataset: one shard
        (30, 30, 3, 1),  # exactly the target is still one shard
        (45, 30, 3, 3),  # ceil(1.5) = 2, rounded up to 3 nodes
        (100, 30, 3, 6),  # ceil(3.33) = 4 -> 6
        (100, 30, 2, 4),  # 4 is already a multiple of 2
        (120, 50, 3, 3),  # time series: ceil(2.4) = 3
        (200, 50, 3, 6),  # ceil(4) = 4 -> 6
    ],
)
def test_shard_count(data_gb, max_gb, nodes, expected):
    assert shard_count(data_gb, max_gb, nodes) == expected


def test_table_and_query_metrics():
    metrics = DesignMetricsContract.model_validate(_metrics("dynamodb"))
    assert [(q.query_id, q.base_rps, q.design_rps) for q in metrics.queries] == [
        ("q1", 0.5, 15.0),
        ("q2", 2.0, 60.0),
        ("q3", 0.000278, 0.0083),
    ]
    tables = {t.table_id: t for t in metrics.tables}
    assert tables["db.users"].avg_row_size_bytes == 2048
    assert tables["db.users"].avg_row_size_kb == 2.0
    assert tables["db.users"].schema_item_size_bytes == 330
    assert tables["db.posts"].avg_row_size_kb == 1.0
    assert tables["db.tags"].size_estimated is True
    assert tables["db.tags"].data_gb == 0.0
    assert tables["db.events"].data_gb == 45.0  # 46,080 / 1024


def test_coverage_ratio():
    # q1 and q2 are in scope, q3 is OTHER: 2 / 4 analysed = 0.5
    coverage = _metrics("dynamodb")["coverage"]
    assert coverage == {"in_scope_patterns": 2, "total_queries_analyzed": 4, "coverage_ratio": 0.5}
    # A design group has no whole-database count, so no ratio.
    assert _metrics("dynamodb", total=None)["coverage"] is None


def test_engine_sections():
    ddb = _metrics("dynamodb")
    assert ddb["dynamodb_partition_limits"] == {
        "read_units_per_second": 3000.0,
        "write_units_per_second": 1000.0,
        "at_risk_threshold_pct": 80.0,
    }
    assert ddb["documentdb_embeddings"] is None and ddb["opensearch_shards"] is None

    (embedding,) = _metrics("documentdb")["documentdb_embeddings"]
    assert embedding == {
        "parent_table": "db.users",
        "child_table": "db.posts",
        "avg_children_per_parent": 4.0,
        "parent_row_size_kb": 2.0,
        "child_row_size_kb": 1.0,
        "embedded_avg_kb": 4.0,
        "doc_avg_kb_if_embedded": 6.0,
    }

    shards = {t["table_id"]: t for t in _metrics("opensearch")["opensearch_shards"]["tables"]}
    assert shards["db.events"] == {
        "table_id": "db.events",
        "data_gb": 45.0,
        "search_shards": 3,  # ceil(45 / 30) = 2 -> 3 nodes
        "timeseries_shards": 1,  # 45 <= 50
    }
    assert shards["db.users"]["search_shards"] == 1


def test_projection_and_collector_shapes_agree():
    """The Bedrock path (Agent* projection) and external path (collector dict) agree."""
    projected = {
        "collector": {
            "tables": TABLES,
            "queries": {"query_patterns": QUERIES, "total_queries_analyzed": 4},
        },
        "context": {"growth_multiplier": 10.0, "peak_to_avg_ratio": 3.0},
    }
    for engine in ("dynamodb", "documentdb", "opensearch"):
        assert design_metrics_from_projection(engine, projected) == _metrics(engine)


def test_metrics_are_deterministic():
    shuffled = build_design_metrics("opensearch", list(reversed(TABLES)), list(reversed(QUERIES)))
    assert shuffled.model_dump(mode="json")["queries"] == _metrics("opensearch")["queries"]
    assert shuffled.model_dump(mode="json")["tables"] == _metrics("opensearch")["tables"]


# ---------------------------------------------------------------------------
# Checking a design against them
# ---------------------------------------------------------------------------


def _hot(units: float, op: str = "read", limit: float = 3000, pct: float = 10.0, risk=False):
    return {
        "table_name": "Users",
        "gsi_name": None,
        "operation": op,
        "rcu_or_wcu_per_second": units,
        "partition_limit": limit,
        "utilization_pct": pct,
        "at_risk": risk,
        "contributing_patterns": ["q1"],
        "mitigation": "shard suffix" if risk else None,
    }


@pytest.mark.parametrize(
    "rps,ok",
    [(15.0, True), (15.1, True), (15.2, False), (30.0, False)],  # 1% of 15 = 0.15
)
def test_dynamodb_design_rps(rps, ok):
    design = {
        "access_patterns": [{"pattern_id": "DDB-AP-1", "query_ids": ["q1"], "design_rps": rps}]
    }
    errors = check_design_numbers("dynamodb", design, _metrics("dynamodb"))
    assert (errors == []) is ok
    if not ok:
        assert "DDB-AP-1" in errors[0] and "15" in errors[0]


def test_a_pattern_over_several_queries_sums_them():
    pattern = {"pattern_id": "DDB-AP-1", "query_ids": ["q1", "q2"], "design_rps": 75.0}
    assert (
        check_design_numbers("dynamodb", {"access_patterns": [pattern]}, _metrics("dynamodb")) == []
    )
    pattern["design_rps"] = 15.0
    assert check_design_numbers("dynamodb", {"access_patterns": [pattern]}, _metrics("dynamodb"))


def test_unknown_query_ids_are_left_to_the_scope_check():
    pattern = {"pattern_id": "DDB-AP-1", "query_ids": ["q-elsewhere"], "design_rps": 1.0}
    assert (
        check_design_numbers("dynamodb", {"access_patterns": [pattern]}, _metrics("dynamodb")) == []
    )


@pytest.mark.parametrize(
    "entry,messages",
    [
        (_hot(300), []),  # 300 / 3000 = 10.0%
        (_hot(300, pct=12.0), ["utilization_pct 12 disagrees", "= 10"]),
        (_hot(300, limit=1000, pct=30.0), ["partition_limit 1000 for a read must be 3000"]),
        (_hot(900, op="write", limit=1000, pct=90.0, risk=True), []),  # 90% is at risk
        (_hot(900, op="write", limit=1000, pct=90.0), ["at_risk must be true"]),
        (_hot(4000, pct=100.0, risk=True), []),  # capped at 100, as the merge does
        (_hot(2400, pct=80.0), []),  # exactly 80% is not above the threshold
    ],
)
def test_hot_partition_arithmetic(entry, messages):
    errors = check_hot_partition_entry(entry)
    assert len(errors) == len([m for m in messages if "must be" in m or "disagrees" in m])
    for message in messages:
        assert any(message in e for e in errors), (message, errors)


def _collection(avg, max_, strategy="embed", sources=("db.users", "db.posts")):
    return {
        "collection_name": "users",
        "source_tables": list(sources),
        "embedded_entities": [
            {
                "source_table": "db.posts",
                "strategy": strategy,
                "avg_array_length": 4.0,
                "max_array_length_estimate": 50.0,
            }
        ],
        "estimated_avg_doc_size_kb": avg,
        "estimated_max_doc_size_kb": max_,
    }


@pytest.mark.parametrize(
    "collection,failing_fields",
    [
        # 2.0 + 1.0 × 4 = 6.0 KB average; 2.0 + 1.0 × 50 = 52.0 KB max.
        (_collection(6.0, 52.0), []),
        (_collection(6.9, 55.0), []),  # within 1 KB / within 10%
        (_collection(20.0, 52.0), ["estimated_avg_doc_size_kb"]),
        (_collection(6.0, 5.0), ["estimated_max_doc_size_kb"]),
        (_collection(20.0, 5.0, strategy="hybrid"), []),  # partial embed: not checked
        (_collection(2.0, 2.0, strategy="reference"), []),  # a reference adds nothing
        (_collection(20.0, 5.0, sources=("db.users", "db.tags", "db.posts")), []),  # 2 parents
    ],
)
def test_documentdb_doc_sizes(collection, failing_fields):
    errors = check_design_numbers(
        "documentdb", {"collections": [collection]}, _metrics("documentdb")
    )
    assert [f for f in failing_fields if any(f in e for e in errors)] == failing_fields
    assert len(errors) == len(failing_fields)


def _nested_metrics() -> dict:
    # users 2 KB, posts 1 KB, comments 0.5 KB; comments -> posts -> users.
    return {
        "tables": [
            {"table_id": "db.users", "avg_row_size_kb": 2.0},
            {"table_id": "db.posts", "avg_row_size_kb": 1.0},
            {"table_id": "db.comments", "avg_row_size_kb": 0.5},
        ],
        "documentdb_embeddings": [
            {"parent_table": "db.users", "child_table": "db.posts"},
            {"parent_table": "db.posts", "child_table": "db.comments"},
        ],
    }


def _nested_collection(avg: float, comments_path: str = "posts.comments") -> dict:
    return {
        "collection_name": "users",
        "source_tables": ["db.users", "db.posts", "db.comments"],
        "embedded_entities": [
            {
                "source_table": "db.posts",
                "embed_path": "posts",
                "strategy": "embed",
                "avg_array_length": 4.0,
                "max_array_length_estimate": 4.0,
            },
            {
                "source_table": "db.comments",
                "embed_path": comments_path,
                "strategy": "embed",
                "avg_array_length": 10.0,
                "max_array_length_estimate": 10.0,
            },
        ],
        "estimated_avg_doc_size_kb": avg,
        "estimated_max_doc_size_kb": avg,
    }


@pytest.mark.parametrize("comments_path", ["posts.comments", "posts[].comments", "comments"])
def test_nested_embeds_count_per_enclosing_element(comments_path):
    """The review's example: post = 1 + 10 × 0.5 = 6 KB, document = 2 + 4 × 6 = 26 KB.

    ``comments`` (no dotted path) is placed inside ``posts`` through its foreign key.
    """
    design = {"collections": [_nested_collection(26.0, comments_path)]}
    assert check_design_numbers("documentdb", design, _nested_metrics()) == []
    # The flat formula's 2 + 4 × 1 + 10 × 0.5 = 11 KB understates it and is rejected.
    design = {"collections": [_nested_collection(11.0, comments_path)]}
    errors = check_design_numbers("documentdb", design, _nested_metrics())
    assert len(errors) == 2 and all("26 KB" in e for e in errors)


def test_nothing_nested_under_a_referenced_entity_counts():
    collection = _nested_collection(2.0)
    collection["embedded_entities"][0]["strategy"] = "reference"
    assert (
        check_design_numbers("documentdb", {"collections": [collection]}, _nested_metrics()) == []
    )


def test_capped_text_columns_are_listed():
    columns = [
        {"column_name": "content", "normalized_data_type": "text", "max_length": 4294967295},
        {"column_name": "title", "normalized_data_type": "string", "max_length": 255},
        {"column_name": "big", "normalized_data_type": "integer", "max_length": 99999},
    ]
    (table,) = build_design_metrics(
        "dynamodb", [{"table_id": "t", "row_count": 1, "size_mb": 1.0, "columns": columns}], []
    ).tables
    assert table.capped_text_columns == ["content"]
    assert table.schema_item_size_bytes == (7 + 1024) + (5 + 255) + (3 + 8)


def test_documentdb_design_rps_uses_source_query_ids():
    design = {
        "access_patterns": [
            {"pattern_id": "DOC-AP-1", "source_query_ids": ["q2"], "design_rps": 2.0}
        ]
    }
    (error,) = check_design_numbers("documentdb", design, _metrics("documentdb"))
    assert "60" in error


def _settings(shards: int, nodes: int = 3) -> dict:
    return {"number_of_shards": shards, "assumed_node_count": nodes}


@pytest.mark.parametrize(
    "design,errors_expected",
    [
        (
            {
                "index_designs": [
                    {
                        "index_name": "events",
                        "source_tables": ["db.events"],
                        "settings": _settings(3),
                    }
                ]
            },
            0,
        ),
        (
            {
                "index_designs": [
                    {
                        "index_name": "events",
                        "source_tables": ["db.events"],
                        "settings": _settings(2),
                    }
                ]
            },
            1,
        ),
        # 45 GB on 2 nodes: ceil(1.5) = 2 -> 2
        (
            {
                "index_designs": [
                    {
                        "index_name": "events",
                        "source_tables": ["db.events"],
                        "settings": _settings(2, 2),
                    }
                ]
            },
            0,
        ),
        # As a data stream 45 GB is one shard (<= 50 GB)
        (
            {
                "data_stream_designs": [
                    {
                        "data_stream_name": "events",
                        "source_tables": ["db.events"],
                        "index_template": {"settings": _settings(1)},
                    }
                ]
            },
            0,
        ),
        # users + posts: 3 MB in all, one shard
        (
            {
                "index_designs": [
                    {
                        "index_name": "u",
                        "source_tables": ["db.users", "db.posts"],
                        "settings": _settings(3),
                    }
                ]
            },
            1,
        ),
        (
            {
                "index_designs": [
                    {"index_name": "x", "source_tables": ["db.missing"], "settings": _settings(9)}
                ]
            },
            0,
        ),
    ],
)
def test_opensearch_shards(design, errors_expected):
    assert (
        len(check_design_numbers("opensearch", design, _metrics("opensearch"))) == errors_expected
    )


def test_engines_without_metrics_are_not_checked():
    assert check_design_numbers("elasticache", {"access_patterns": [{"design_rps": 9}]}, {}) == []


# ---------------------------------------------------------------------------
# Every input path carries the metrics
# ---------------------------------------------------------------------------


def _full_collector() -> dict:
    tables = [
        {**t, "schema_name": "db", "columns": [{**c, "data_type": "int"} for c in t["columns"]]}
        for t in TABLES
    ]
    return {
        "contract_version": "3.0",
        "job_id": "job-1",
        "database_schema": {"tables": tables},
        "queries": {
            "query_patterns": [
                {**q, "query_text": "SELECT 1", "tables_accessed": ["db.users"]} for q in QUERIES
            ],
            "total_queries_analyzed": 4,
        },
    }


def test_external_request_carries_design_metrics():
    from src.agents.schema_design.handler import prepare_schema_design_input

    store = MagicMock()
    store.read_json.side_effect = lambda key: (
        _full_collector()
        if key.endswith("collector/output.json")
        else {"table_recommendations": []}
    )
    for engine in ("dynamodb", "documentdb", "opensearch"):
        request = prepare_schema_design_input("job-1", "db", engine, store)
        assert request["design_metrics"] == _metrics(engine)
    assert "design_metrics" not in prepare_schema_design_input("job-1", "db", "elasticache", store)


def test_group_input_carries_design_metrics_without_coverage():
    from src.agents.schema_design.group_input import build_group_input

    collector = _full_collector()
    group = build_group_input(
        job_id="job-1",
        database_name="db",
        engine="dynamodb",
        group_index=0,
        group_name="g",
        primary_tables=["db.users"],
        group_queries=collector["queries"]["query_patterns"][:2],
        group_tables=collector["database_schema"]["tables"][:2],
        collector_output=collector,
        analysis_output={},
        total_queries=3,
    )
    metrics = group["design_metrics"]
    assert metrics["coverage"] is None
    assert [q["design_rps"] for q in metrics["queries"]] == [15.0, 60.0]
    assert [t["table_id"] for t in metrics["tables"]] == ["db.posts", "db.users"]


# ---------------------------------------------------------------------------
# Rejection on each path
# ---------------------------------------------------------------------------


def test_cost_check_computes_the_figures_and_flags_the_drafts_own():
    from src.tools.schema.dynamodb_cost_check import (
        check_draft_costs,
        compute_performances_and_costs_entries,
    )

    raw = {
        k: v
        for k, v in _hot(900, op="write").items()
        if k not in ("partition_limit", "utilization_pct", "at_risk")
    }
    raw["mitigation"] = "shard suffix"
    (computed,) = compute_performances_and_costs_entries([raw])
    assert (computed["partition_limit"], computed["utilization_pct"], computed["at_risk"]) == (
        1000.0,
        90.0,
        True,
    )

    draft = {
        "hot_partition_analysis": [
            {**raw, "partition_limit": 1000, "utilization_pct": 9.0, "at_risk": False}
        ],
        "access_patterns": [{"pattern_id": "DDB-AP-1", "query_ids": ["q1"], "design_rps": 0.5}],
    }
    result = check_draft_costs(draft, _metrics("dynamodb", total=None))
    assert result["passed"] is False
    assert result["results"][0]["utilization_pct"] == 90.0  # the figure to copy
    messages = [e["error"] for e in result["errors"]]
    assert any("utilization_pct 9 disagrees" in m for m in messages)
    assert any("design_rps 0.5 disagrees" in m for m in messages)

    fixed = {
        "hot_partition_analysis": [computed],
        "access_patterns": [{"pattern_id": "DDB-AP-1", "query_ids": ["q1"], "design_rps": 15.0}],
    }
    assert check_draft_costs(fixed, _metrics("dynamodb", total=None))["passed"] is True


def test_check_costs_script_reads_the_group_metrics(tmp_path):
    from scripts.run_schema_design import _group_design_metrics

    (tmp_path / "input_group_2.json").write_text(json.dumps({"design_metrics": {"queries": []}}))
    assert _group_design_metrics(tmp_path / "schema_draft_group_2.json") == {"queries": []}
    assert _group_design_metrics(tmp_path / "schema_draft_group_3.json") is None
    assert _group_design_metrics(tmp_path / "draft.json") is None


@pytest.mark.usefixtures("schema_contract_off")
def test_finalize_keeps_a_design_whose_numbers_disagree_but_fails_it(tmp_path: Path, monkeypatch):
    """Headless matches Bedrock: the design is written, marked failed, the phase goes on."""
    from src.agents.schema_design import handler
    from src.storage.local_store import LocalArtifactStore

    monkeypatch.setattr(handler, "validate_schema_design_output", lambda *_: {"valid": True})
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(
        "db/job-1/llm_requests/schema_design_documentdb.json",
        {"design_metrics": _metrics("documentdb")},
    )
    design: dict[str, Any] = {
        "access_patterns": [
            {"pattern_id": "DOC-AP-1", "source_query_ids": ["q1"], "design_rps": 1.0}
        ],
        "collections": [],
        "trade_offs": [],
        "validation_passed": True,
        "validation_failures": ["an unrelated failure"],
    }
    response = "db/job-1/llm_responses/schema_design_documentdb.json"
    written = "db/job-1/schema-documentdb/v1/schema_output.json"
    store.write_json(response, design)
    result = handler.finalize_schema_design("job-1", "db", "documentdb", store)
    assert result["status"] == "complete"
    (warning,) = result["warnings"]
    assert warning.startswith("design_metrics: ") and "design_rps 1 disagrees" in warning
    output = store.read_json(written)
    assert output["validation_passed"] is False
    assert output["validation_failures"] == ["an unrelated failure", warning]

    # Re-finalizing the written design after the fix drops the stale mismatch.
    fixed = {**output, "access_patterns": [{**design["access_patterns"][0], "design_rps": 15.0}]}
    fixed["validation_failures"] = [warning]
    store.write_json(response, fixed)
    result = handler.finalize_schema_design("job-1", "db", "documentdb", store)
    assert result["status"] == "complete" and "warnings" not in result
    output = store.read_json(written)
    assert output["validation_passed"] is True and output["validation_failures"] == []


class _Out(BaseModel):
    rps: float
    validation_passed: bool = True
    validation_failures: list[str] = []


def _runner(outputs: list[_Out]):
    from src.tools.schema.base_schema_agent import SchemaDesignRunner

    prompts: list[str] = []

    def designer(prompt):
        prompts.append(prompt)
        return MagicMock(structured_output=outputs.pop(0))

    runner = SchemaDesignRunner(
        target_type="dynamodb",
        output_model=_Out,
        model=MagicMock(),
        designer_agent=designer,
        output_check_fn=lambda out: (
            [] if out.rps == 15.0 else [f"design_rps {out.rps:g} disagrees"]
        ),
    )
    return runner, prompts


def test_runner_asks_for_a_correction():
    runner, prompts = _runner([_Out(rps=1.0), _Out(rps=15.0)])
    output, _ = runner.run("design")
    assert output.rps == 15.0 and output.validation_passed is True
    assert len(prompts) == 2
    assert "design_rps 1 disagrees" in prompts[1] and "design_metrics" in prompts[1]


def test_runner_keeps_a_design_that_never_agrees_but_fails_it():
    runner, prompts = _runner([_Out(rps=1.0), _Out(rps=2.0), _Out(rps=3.0)])
    output, _ = runner.run("design")
    assert len(prompts) == 3  # the design plus two correction turns
    assert output.rps == 3.0
    assert output.validation_passed is False
    assert output.validation_failures == ["design_metrics: design_rps 3 disagrees"]


# ---------------------------------------------------------------------------
# The skills read the numbers instead of computing them
# ---------------------------------------------------------------------------

SKILLS = Path(__file__).resolve().parents[4] / "src" / "skills"


@pytest.mark.parametrize(
    "skill",
    ["dynamodb-data-modeling.md", "documentdb-data-modeling.md", "opensearch-index-modeling.md"],
)
def test_skills_read_design_metrics(skill):
    text = (SKILLS / skill).read_text(encoding="utf-8")
    assert "design_metrics" in text
    assert "Chunking Threshold" not in text and "80 KB" not in text
    assert "compute as `(calls_per_second" not in text
    assert "`design_rps`: `(calls_per_second" not in text


def test_opensearch_skill_no_longer_derives_shards():
    text = (SKILLS / "opensearch-index-modeling.md").read_text(encoding="utf-8")
    assert "raw_shards = ceil" not in text
    assert "design_metrics.opensearch_shards" in text


def test_coverage_counts_the_whole_log_after_the_assignment_filter():
    """An engine-scoped input still reports the collector's coverage, not its share."""
    from src.agents.schema_design.handler import filter_collector_for_assignment

    assignment = {
        "query_assignments": [
            {"query_id": "q1", "assigned_engine": "documentdb", "in_scope": True},
            {"query_id": "q2", "assigned_engine": "dynamodb", "in_scope": True},
        ]
    }
    filtered = filter_collector_for_assignment(_full_collector(), assignment, "documentdb")
    assert [q["query_id"] for q in filtered["queries"]["query_patterns"]] == ["q1"]
    # q1 and q2 are in the log (q3 is OTHER): still 2 / 4, not 1 / 4.
    coverage = design_metrics_from_collector("documentdb", filtered)["coverage"]
    assert coverage["in_scope_patterns"] == 2 and coverage["coverage_ratio"] == 0.5
