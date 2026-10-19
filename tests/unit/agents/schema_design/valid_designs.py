"""Small DynamoDB designs that pass ``DynamoDBModelOutputContract`` (#1186).

For merge and reconcile tests: drafts built here are valid, so a merger change
that turns valid drafts into an invalid merged design fails those tests at the
output-contract gate instead of passing unnoticed.
"""

from __future__ import annotations


def ddb_attr(name: str, source: str, column: str | None = None, type_: str = "S") -> dict:
    return {"name": name, "type": type_, "source_table": source, "source_column": column or name}


def ddb_key(name: str, type_: str = "S") -> dict:
    return {"attribute_name": name, "attribute_type": type_}


def ddb_table(name: str, source: str, pk: str = "id", **overrides) -> dict:
    table = {
        "table_name": name,
        "aggregate_pattern": "separate",
        "source_tables": [source],
        "partition_key": ddb_key(pk),
        "attributes": [ddb_attr(pk, source)],
        "gsis": [],
        "item_count": 10,
        "item_size_bytes": 100,
    }
    return {**table, **overrides}


def ddb_pattern(pid: str, table: str, source: str, query: str, **overrides) -> dict:
    pattern = {
        "pattern_id": pid,
        "pattern_group": "reads",
        "query_ids": [query],
        "source_tables": [source],
        "description": "read by key",
        "operation": "GetItem",
        "table_name": table,
        "key_condition": "PK = id",
        "design_rps": 1.0,
        "item_size_bytes": 100,
    }
    return {**pattern, **overrides}


def ddb_design(
    tables: list[dict],
    patterns: list[dict],
    *,
    trade_offs: list[dict] | None = None,
    job_id: str = "job-001",
    database: str = "mydb",
) -> dict:
    return {
        "job_id": job_id,
        "source_database": database,
        "target_engine": "dynamodb",
        "access_patterns": patterns,
        "table_definitions": tables,
        "hot_partition_analysis": [],
        "trade_offs": (
            trade_offs if trade_offs is not None else [{"description": "d", "impact": "i"}]
        ),
        "validation_passed": True,
        "validation_failures": [],
    }
