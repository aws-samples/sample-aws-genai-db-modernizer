"""Schema query groups retain source-query provenance for every engine (#237)."""

import pytest

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import build_query_groups


def data_for(engine: str, patterns: list[dict]) -> SynthesisData:
    return SynthesisData(
        job_id="test",
        database_name="sample",
        collector={
            "queries": {
                "query_patterns": [
                    {"query_id": "q1", "query_text": "SELECT * FROM users", "query_type": "SELECT"}
                ]
            }
        },
        engines={
            engine: EngineArtifacts(engine=engine, schema_design={"access_patterns": patterns})
        },
    )


@pytest.mark.parametrize("engine", ["elasticache", "documentdb"])
def test_source_query_ids_are_linked_and_grouped_by_source(engine: str) -> None:
    data = data_for(
        engine,
        [
            {
                "pattern_id": "AP-1",
                "source_query_ids": ["q1"],
                "source_tables": ["public.users"],
                "design_rps": 12.5,
            }
        ],
    )
    groups = build_query_groups(data)
    assert len(groups) == 1
    group = groups[0]
    assert group["group_name"] == "public.users"
    assert group["access_patterns"][0]["query_ids"] == ["q1"]
    assert group["source_queries"][0]["query_id"] == "q1"
    assert group["source_queries"][0]["linked_patterns"] == ["AP-1"]
    assert group["total_design_rps"] == 12.5


def test_empty_legacy_ids_fall_back_and_links_are_deduplicated() -> None:
    data = data_for(
        "documentdb",
        [
            {
                "pattern_id": "AP-1",
                "query_ids": [],
                "source_query_ids": ["q1"],
                "source_tables": ["users", "profiles"],
            },
            {
                "pattern_id": "AP-2",
                "source_query_ids": ["q1"],
                "source_tables": ["profiles", "users"],
            },
        ],
    )
    (group,) = build_query_groups(data)
    assert group["group_name"] == "profiles, users"
    assert len(group["source_queries"]) == 1
    assert group["source_queries"][0]["linked_patterns"] == ["AP-1", "AP-2"]


def test_explicit_groups_and_query_ids_keep_precedence() -> None:
    data = data_for(
        "dynamodb",
        [
            {
                "pattern_id": "AP-1",
                "pattern_group": "User lookups",
                "query_ids": ["q1"],
                "source_query_ids": ["other"],
                "source_tables": ["users"],
            }
        ],
    )
    (group,) = build_query_groups(data)
    assert group["group_name"] == "User lookups"
    assert group["access_patterns"][0]["query_ids"] == ["q1"]
    assert group["source_queries"][0]["query_id"] == "q1"


def test_missing_group_metadata_keeps_legacy_ungrouped_fallback() -> None:
    (group,) = build_query_groups(data_for("dynamodb", [{"pattern_id": "AP-1"}]))
    assert group["group_name"] == "ungrouped"


def test_unknown_source_query_ids_do_not_fabricate_queries() -> None:
    (group,) = build_query_groups(
        data_for("elasticache", [{"source_query_ids": ["missing"], "source_tables": ["users"]}])
    )
    assert group["source_queries"] == []
    assert group["access_patterns"][0]["query_ids"] == ["missing"]
