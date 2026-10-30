# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Engine display names are the one shared, AWS-branded source of truth (#221).

The interactive HTML report's engine label table and its hand-kept JS mirror (both
retired in #987) had drifted into non-brand casing ("Elasticache",
"AuroraPostgresql", "AuroraMySQL"); every renderer now reads this module instead of
keeping its own dict (#239).
"""

from __future__ import annotations

from src.shared.engine_names import ENGINE_DISPLAY_NAMES, display_engine


def test_names_every_engine_with_its_brand_casing() -> None:
    for engine, name in (
        ("neptune", "Neptune"),
        ("keyspaces", "Keyspaces"),
        ("aurora", "Aurora"),
        ("aurora_mysql", "Aurora MySQL"),
        ("elasticache", "ElastiCache"),
    ):
        assert ENGINE_DISPLAY_NAMES[engine] == name


def test_display_engine_known_ids() -> None:
    assert display_engine("aurora_mysql") == "Aurora MySQL"
    assert display_engine("aurora_postgresql") == "Aurora PostgreSQL"
    assert display_engine("elasticache") == "ElastiCache"
    assert display_engine("documentdb") == "DocumentDB"
    assert display_engine("dynamodb") == "DynamoDB"
    assert display_engine("opensearch") == "OpenSearch"


def test_display_engine_falls_back_to_a_titlecased_id() -> None:
    assert display_engine("some_future_engine") == "Some Future Engine"


def test_display_engine_passes_through_empty_and_falsy() -> None:
    assert display_engine("") == ""


def test_heterogeneous_source_clause_is_one_wording_for_wave_and_summary() -> None:
    from src.shared.engine_names import heterogeneous_source_clause

    clause = heterogeneous_source_clause("sqlserver")
    assert clause == (
        "The source SQL Server database has no Aurora engine of its own dialect: "
        "Aurora MySQL and Aurora PostgreSQL both competed on the collected workload"
    )
    assert heterogeneous_source_clause("oracle", "19.3").startswith(
        "The source Oracle database (19.3) has no Aurora engine"
    )
