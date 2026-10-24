"""Every in-scope query is in a query group, or the report says which are not (#226).

``facts.query_group_counts.ungrouped_queries`` counts, per owner engine, the in-scope
queries no group of its own serves; a cache-layer group serves none. The engineering
report states them under "Query groups", and labels a cache-layer group's queries as
cached reads.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from src.contracts.synthesis_output import QueryGroupFacts
from src.report.facts import report_facts
from src.report.renderers import render_engineering_report_md
from src.shared.report_facts import query_group_facts
from tests.report_concision import section

FIXTURE = Path(__file__).parent / "fixtures" / "wordpress_model_run_1_60_report.json"


def _ap(engine: str, qids: list[str], pid: str) -> dict[str, Any]:
    return {"engine": engine, "pattern_id": pid, "query_ids": qids, "design_rps": 1.0}


def _sq(qid: str, cached: bool = False) -> dict[str, Any]:
    return {
        "query_id": qid,
        "query_text": f"SELECT {qid}",
        **({"cached_read": True} if cached else {}),
    }


def _report(ddb_designed: bool = True) -> dict[str, Any]:
    members = [
        {"query_id": "q1", "engine": "dynamodb"},
        {"query_id": "q2", "engine": "dynamodb"},
        {"query_id": "q3", "engine": "aurora_mysql"},
        {"query_id": "q4", "engine": None},  # no engine in scope (#1329): no owner
        {"query_id": "q5", "engine": "documentdb"},
        {"query_id": "q6", "engine": "documentdb"},
    ]
    return {
        "ranking": [
            {"target": "dynamodb", "schema_design_available": ddb_designed},
            {"target": "aurora_mysql", "schema_design_available": True},
            {"target": "documentdb", "schema_design_available": False},
        ],
        "query_pattern_groups": [{"pattern_id": "p", "members": members}],
        "query_groups": [
            {
                "group_name": "Option reads",
                "engines": ["dynamodb"],
                "access_patterns": [_ap("dynamodb", ["q1"], "DDB-AP-1")],
                "source_queries": [_sq("q1")],
                "total_design_rps": 1.0,
            },
            {
                # Fronts q1 and q2: neither is served here.
                "group_name": "wp.wp_options",
                "engines": ["elasticache"],
                "cache_layer": True,
                "access_patterns": [
                    _ap("elasticache", ["q1"], "EC-AP-1"),
                    _ap("elasticache", ["q2"], "EC-AP-2"),
                ],
                "source_queries": [_sq("q1", True), _sq("q2", True)],
                "total_design_rps": 2.0,
            },
            {
                "group_name": "wp.wp_posts",
                "engines": ["aurora_mysql"],
                "access_patterns": [
                    _ap("aurora_mysql", ["q3"], "relational-aurora_mysql-q3"),
                    # Another engine's pattern on a query does not group it for its owner.
                    _ap("dynamodb", ["q5"], "DDB-AP-9"),
                ],
                "source_queries": [_sq("q3")],
                "total_design_rps": 1.0,
            },
        ],
    }


def test_ungrouped_queries_per_owner_engine() -> None:
    facts = query_group_facts(_report())
    assert facts is not None
    assert facts["ungrouped_queries"] == [
        {"engine": "documentdb", "query_count": 2, "schema_design_available": False},
        # q2 is only in the cache layer's group, which serves no query.
        {"engine": "dynamodb", "query_count": 1, "schema_design_available": True},
    ]
    QueryGroupFacts.model_validate(facts)


def test_every_query_grouped_leaves_the_list_empty() -> None:
    report = _report()
    report["query_pattern_groups"][0]["members"] = [
        {"query_id": "q1", "engine": "dynamodb"},
        {"query_id": "q3", "engine": "aurora_mysql"},
    ]
    assert query_group_facts(report)["ungrouped_queries"] == []  # type: ignore[index]


def test_no_in_scope_query_list_states_no_gap() -> None:
    report = _report()
    del report["query_pattern_groups"]
    assert query_group_facts(report)["ungrouped_queries"] == []  # type: ignore[index]


def test_a_facts_block_from_before_the_field_still_loads() -> None:
    """A 1.63 block has no ``ungrouped_queries``: it loads, and the report states none."""
    block = query_group_facts(_report())
    assert block is not None
    block.pop("ungrouped_queries")
    assert QueryGroupFacts.model_validate(block).ungrouped_queries == []


def _md(report: dict[str, Any]) -> str:
    report.pop("facts", None)
    return render_engineering_report_md(report)


def test_the_report_states_the_ungrouped_queries() -> None:
    groups = section(_md(_report()), "Query groups")
    assert (
        "No group yet for 2 DocumentDB queries: DocumentDB's schema design has not run, "
        "and its access patterns make the groups." in groups
    )
    assert (
        "No group serves 1 DynamoDB query: no access pattern in DynamoDB's schema design "
        "lists it." in groups
    )


def test_the_cache_layer_row_counts_cached_reads() -> None:
    groups = section(_md(_report()), "Query groups")
    row = next(ln for ln in groups.splitlines() if ln.startswith("| wp.wp_options |"))
    assert "| 2 cached reads |" in row
    assert "A cache layer group lists the reads it fronts as cached reads" in groups
    owner = next(ln for ln in groups.splitlines() if ln.startswith("| Option reads |"))
    assert "cached" not in owner


def test_with_no_listed_group_the_section_still_states_the_gap() -> None:
    """The deterministic samples: only Aurora groups, the other engines undesigned."""
    report = _report(ddb_designed=False)
    report["query_groups"] = [report["query_groups"][2]]
    md = _md(report)
    assert "## Query groups (0 of 1)" in md.splitlines()
    groups = section(md, "Query groups")
    assert "All 1 groups" not in groups
    assert (
        "The only group runs only on Aurora MySQL; Queries on Aurora by table lists its" in groups
    )
    assert "No group yet for 2 DynamoDB queries" in groups
    assert "| Group | Engines |" not in groups
    assert "\n\n\n" not in md


@pytest.mark.passes_on_main  # guards the saved model run's rendering, which has no flags
def test_a_saved_report_from_before_the_split_renders_no_cached_label() -> None:
    saved = json.loads(FIXTURE.read_text())
    groups = section(render_engineering_report_md(copy.deepcopy(saved)), "Query groups")
    assert "cached read" not in groups
    assert "No group" not in groups
    assert report_facts(copy.deepcopy(saved))["query_group_counts"]["listed_group_count"] == 33
