"""The schema designers see each access pattern once (#1146).

Statements collected several ways (another projection, other literals, another IN-list
length) are one access pattern. Every designer input path lists the patterns that more
than one of its queries makes, each once with its member query ids, combined frequency
and the union of the columns they return (``queries.query_pattern_groups``): the
external request (``prepare_schema_design_input``), each design group's input
(``build_group_input``) and the Bedrock projection (``load_agent_input``). The query
patterns themselves are unchanged, so every query is still designed and covered. No test
calls a model.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from src.agents.analysis.dynamodb_analysis_agent import analyze_for_dynamodb_deterministic
from src.agents.schema_design.group_input import build_group_input
from src.agents.schema_design.handler import (
    filter_collector_for_assignment,
    prepare_schema_design_input,
)
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.contracts.schema_design_input import project_schema_design_input
from src.storage.local_store import LocalArtifactStore
from src.tools.schema.base_schema_agent import AgentInputPaths
from src.tools.schema.dynamodb_schema_agent import load_agent_input
from src.tools.schema.opensearch_schema_agent import load_agent_input as opensearch_input
from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output

DB, JOB = "ecommerce", "job-1146"


def _read(qid: str, text: str, freq: float) -> dict:
    return {
        "query_id": qid,
        "query_text": text,
        "query_type": "SELECT",
        "frequency_per_hour": freq,
        "calls_per_second": freq / 3600,
        "tables_accessed": ["ecommerce.users"],
    }


# One lookup collected three ways, and a COUNT of the same rows (another pattern).
COPIES = [
    _read("u-by-id-all", "SELECT * FROM users WHERE user_id = ?", 360.0),
    _read("u-by-id-name", "SELECT username FROM users WHERE user_id IN (?, ?)", 720.0),
    _read("u-by-id-mail", "SELECT email FROM users WHERE user_id = 7", 0.0),
]
COUNT = _read("u-count", "SELECT COUNT(*) FROM users WHERE user_id = ?", 36.0)
IDS = ["u-by-id-all", "u-by-id-name", "u-by-id-mail"]


def _collector() -> dict:
    collector = copy.deepcopy(get_ecommerce_collector_output())
    collector["queries"]["query_patterns"] += [*COPIES, COUNT]
    return collector


def _analysis() -> dict:
    contract, _, _ = analyze_for_dynamodb_deterministic(
        AnalysisInput(
            job_id=JOB, collector_output=_collector(), target_database=TargetDatabase.DYNAMODB
        )
    )
    return contract.model_dump(mode="json")


def _assignment(collector: dict, dynamodb: set[str]) -> dict:
    return {
        "query_assignments": [
            {
                "query_id": q["query_id"],
                "assigned_engine": "dynamodb" if q["query_id"] in dynamodb else "aurora_mysql",
                "in_scope": True,
                "source_tables": q["tables_accessed"],
            }
            for q in collector["queries"]["query_patterns"]
        ]
    }


def _listed(collector: dict) -> list[dict]:
    groups: list[dict] = collector["queries"].get("query_pattern_groups") or []
    return groups


def test_the_filtered_collection_lists_the_repeated_pattern_once() -> None:
    collector = _collector()
    filtered = filter_collector_for_assignment(
        collector, _assignment(collector, {*IDS, "u-count"}), "dynamodb"
    )
    (group,) = _listed(filtered)
    assert group["query_ids"] == IDS
    assert group["frequency_per_hour"] == 1080.0
    assert group["calls_per_second"] == 0.3
    assert group["returned_columns"] == ["*", "email", "username"]
    assert group["operation"] == "read" and group["tables"] == ["users"]
    # Every query is still there, the COUNT among them as its own pattern.
    ids = [q["query_id"] for q in filtered["queries"]["query_patterns"]]
    assert ids == [*IDS, "u-count"]


def test_only_this_engines_queries_are_grouped() -> None:
    collector = _collector()
    filtered = filter_collector_for_assignment(
        collector, _assignment(collector, {"u-by-id-all", "u-by-id-name"}), "dynamodb"
    )
    (group,) = _listed(filtered)
    assert group["query_ids"] == ["u-by-id-all", "u-by-id-name"]
    alone = filter_collector_for_assignment(
        collector, _assignment(collector, {"u-by-id-all"}), "dynamodb"
    )
    assert "query_pattern_groups" not in alone["queries"]


def test_the_external_request_carries_it(tmp_path: Path) -> None:
    collector = _collector()
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector)
    store.write_json(f"{DB}/{JOB}/analysis-dynamodb/analysis.json", _analysis())
    store.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json", _assignment(collector, {*IDS, "u-count"})
    )
    request = prepare_schema_design_input(JOB, DB, "dynamodb", store, assignment_version=1)
    (group,) = _listed(request["collector_output"])
    assert group["query_ids"] == IDS


def _group_input(collector: dict, ids: set[str]) -> dict:
    queries = [q for q in collector["queries"]["query_patterns"] if q["query_id"] in ids]
    tables = [t for t in collector["database_schema"]["tables"] if t["table_name"] == "users"]
    return build_group_input(
        job_id=JOB,
        database_name=DB,
        engine="dynamodb",
        group_index=0,
        group_name="users",
        primary_tables=[t["table_id"] for t in tables],
        group_queries=queries,
        group_tables=tables,
        collector_output=collector,
        analysis_output=_analysis(),
        total_queries=len(collector["queries"]["query_patterns"]),
    )


def test_a_design_group_lists_its_own_repeated_patterns(tmp_path: Path) -> None:
    collector = _collector()
    # The whole collection's list never leaks into a group.
    collector["queries"]["query_pattern_groups"] = [{"pattern_id": "PAT-9", "query_ids": []}]
    group = _group_input(collector, {"u-by-id-all", "u-by-id-mail", "u-count"})
    (listed,) = _listed(group["collector_output"])
    assert listed["query_ids"] == ["u-by-id-all", "u-by-id-mail"]
    none = _group_input(collector, {"u-by-id-all", "u-count"})
    assert "query_pattern_groups" not in none["collector_output"]["queries"]


def _paths(tmp_path: Path, collector: dict, analysis: dict) -> AgentInputPaths:
    files = []
    for name, data in (("collector", collector), ("analysis", analysis)):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(data, default=str))
        files.append(str(path))
    return AgentInputPaths(collector=files[0], analysis=files[1])


def test_the_bedrock_projection_carries_the_group_inputs_list(tmp_path: Path) -> None:
    collector = _collector()
    group = _group_input(collector, {*IDS, "u-count"})
    paths = _paths(tmp_path, group["collector_output"], group["analysis_output"])
    for loader in (load_agent_input, opensearch_input):
        (listed,) = loader(paths)["collector"]["queries"]["query_pattern_groups"]
        assert listed["query_ids"] == IDS
        assert listed["frequency_per_hour"] == 1080.0


def test_the_projection_drops_a_group_naming_a_query_it_does_not_hold(tmp_path: Path) -> None:
    from src.contracts.analysis_output import AnalysisOutputContract
    from src.contracts.collector_output import CollectorOutputContract

    collector = _collector()
    collector["queries"]["query_patterns"] = [
        q for q in collector["queries"]["query_patterns"] if q["query_id"] != "u-by-id-mail"
    ]
    contract = CollectorOutputContract.model_validate(collector)
    analysis = AnalysisOutputContract.model_validate(_analysis())
    groups = [
        {"pattern_id": "PAT-1", "query_ids": IDS, "operation": "read"},
        {"pattern_id": "PAT-2", "query_ids": IDS[:2], "operation": "read"},
    ]
    agent, _, _ = project_schema_design_input(contract, analysis, query_pattern_groups=groups)
    assert [g.pattern_id for g in agent.queries.query_pattern_groups or []] == ["PAT-2"]
    bare, _, _ = project_schema_design_input(contract, analysis)
    assert bare.queries.query_pattern_groups is None
