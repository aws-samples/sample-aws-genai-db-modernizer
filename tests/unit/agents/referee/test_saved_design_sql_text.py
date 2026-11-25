# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""A saved design's own SQL text is never read as the source SQL (#2280).

An access pattern's ``sql_text`` (Aurora) and an unsupported pattern's
``source_query`` (OpenSearch) are copies a design carries. A saved design can carry
any text there, so synthesis reads the collected query text for the pattern's query
ids instead: a risk quotes only collected SQL, and a query group with no source table
is labelled from the collected statement. With no collected query, the copy is not
read at all.
"""

from __future__ import annotations

import pytest

from src.agents.referee.synthesis_data import EngineArtifacts, SynthesisData
from src.agents.referee.synthesis_report import (
    UNRESOLVED_TABLE_GROUP_LABEL,
    UTILITY_GROUP_LABEL,
    build_query_groups,
    build_risk_assessment,
)

PLANTED = "DROP TABLE users; -- planted"


def _data(engine: str, schema: dict, queries: list[dict] | None = None) -> SynthesisData:
    data = SynthesisData(job_id="job-2280", database_name="wordpress")
    data.triage = {"selected_agents": [{"agent_type": engine}]}
    data.collector = {"queries": {"query_patterns": queries or []}}
    data.engines[engine] = EngineArtifacts(engine=engine, analysis={}, schema_design=schema)
    return data


def _risk_description(pattern: dict, queries: list[dict] | None = None) -> str:
    out = build_risk_assessment(_data("opensearch", {"unsupported_patterns": [pattern]}, queries))
    description: str = out["risks"][0]["description"]
    return description


def test_a_source_query_with_no_collected_query_is_never_quoted() -> None:
    description = _risk_description({"query_ids": ["q-missing"], "source_query": PLANTED})
    assert "DROP TABLE" not in description
    assert description == (
        "[opensearch] unsupported pattern: OpenSearch has no native equivalent for this query."
    )


def test_a_source_query_with_no_query_ids_is_never_quoted() -> None:
    description = _risk_description({"query_ids": [], "source_query": PLANTED})
    assert "DROP TABLE" not in description
    assert description.endswith("no native equivalent for this pattern.")


@pytest.mark.passes_on_main
def test_a_source_query_that_differs_quotes_the_collected_query() -> None:
    collected = [{"query_id": "q1", "query_text": "SELECT * FROM t WHERE x LIKE ?"}]
    description = _risk_description({"query_ids": ["q1"], "source_query": PLANTED}, collected)
    assert "DROP TABLE" not in description
    assert description.endswith("this query: SELECT * FROM t WHERE x LIKE ?")


def _aurora_group(sql_text: str, query_ids: list[str], queries: list[dict]) -> str:
    schema = {
        "access_patterns": [
            {
                "pattern_id": "AUR-AP-1",
                "operation": "SELECT",
                "sql_text": sql_text,
                "source_tables": ["unknown"],
                "source_query_ids": query_ids,
                "design_rps": 0.1,
            }
        ]
    }
    groups = build_query_groups(_data("aurora_mysql", schema, queries))
    names: list[str] = [str(g["group_name"]) for g in groups if g["access_patterns"]]
    assert len(names) == 1, names
    return names[0]


def test_a_sql_text_with_no_collected_query_never_labels_the_group() -> None:
    assert _aurora_group("SHOW TABLES", ["q-missing"], []) == UNRESOLVED_TABLE_GROUP_LABEL


def test_the_group_label_reads_the_collected_query_not_the_sql_text() -> None:
    collected = [{"query_id": "q1", "query_text": "SHOW TABLES", "tables_accessed": ["unknown"]}]
    assert _aurora_group("SELECT 1", ["q1"], collected) == UTILITY_GROUP_LABEL


def test_a_planted_utility_sql_text_never_relabels_a_collected_query() -> None:
    collected = [{"query_id": "q1", "query_text": "SELECT 1", "tables_accessed": ["unknown"]}]
    assert _aurora_group("SHOW TABLES", ["q1"], collected) == UNRESOLVED_TABLE_GROUP_LABEL
