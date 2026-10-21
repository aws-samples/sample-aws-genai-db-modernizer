"""Query tags never reach a schema designer's model input (#1180).

``QueryPattern.query_tags`` is the caller's join key to its own code, customer text
with no bearing on a design. Every designer input path leaves it out: the external
request (``prepare_schema_design_input``, with and without an assignment version),
each design group's input (``build_group_input``) and the Bedrock projection
(``load_agent_input``). No test calls a model.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.agents.analysis.dynamodb_analysis_agent import analyze_for_dynamodb_deterministic
from src.agents.schema_design.group_input import build_group_input
from src.agents.schema_design.handler import prepare_schema_design_input
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.storage.local_store import LocalArtifactStore
from src.tools.schema.base_schema_agent import AgentInputPaths
from src.tools.schema.dynamodb_schema_agent import load_agent_input
from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output

DB, JOB = "ecommerce", "job-1180"
SENTINEL = "controller='Sentinel1180',action='ignore_previous_instructions'"


def _collector() -> dict:
    collector = copy.deepcopy(get_ecommerce_collector_output())
    for q in collector["queries"]["query_patterns"]:
        q["query_tags"] = [SENTINEL]
        q["query_tags_dropped"] = 3
    return collector


def _analysis(collector: dict) -> dict:
    contract, _, _ = analyze_for_dynamodb_deterministic(
        AnalysisInput(
            job_id=JOB, collector_output=collector, target_database=TargetDatabase.DYNAMODB
        )
    )
    return contract.model_dump(mode="json")


def _assignment(collector: dict) -> dict:
    return {
        "query_assignments": [
            {
                "query_id": q["query_id"],
                "assigned_engine": "dynamodb",
                "in_scope": True,
                "source_tables": q["tables_accessed"],
            }
            for q in collector["queries"]["query_patterns"]
        ]
    }


def _assert_no_tags(payload: dict) -> None:
    text = json.dumps(payload, default=str)
    assert "Sentinel1180" not in text
    assert "query_tags" not in text


@pytest.mark.parametrize("version", [0, 1])
def test_the_external_request_has_no_tags(tmp_path: Path, version: int) -> None:
    collector = _collector()
    store = LocalArtifactStore(base_dir=str(tmp_path))
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector)
    store.write_json(f"{DB}/{JOB}/analysis-dynamodb/analysis.json", _analysis(collector))
    store.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", _assignment(collector))
    request = prepare_schema_design_input(JOB, DB, "dynamodb", store, assignment_version=version)
    assert request["collector_output"]["queries"]["query_patterns"]
    _assert_no_tags(request)


# Both already list only the agent contract's query fields; this keeps it so.
@pytest.mark.passes_on_main
def test_a_group_input_and_the_bedrock_projection_have_no_tags(tmp_path: Path) -> None:
    collector = _collector()
    tables = collector["database_schema"]["tables"]
    group = build_group_input(
        job_id=JOB,
        database_name=DB,
        engine="dynamodb",
        group_index=0,
        group_name="all",
        primary_tables=[t["table_id"] for t in tables],
        group_queries=collector["queries"]["query_patterns"],
        group_tables=tables,
        collector_output=collector,
        analysis_output=_analysis(collector),
        total_queries=len(collector["queries"]["query_patterns"]),
    )
    _assert_no_tags(group)
    # The Bedrock projection, even of a collector output that still has them.
    paths = []
    for name, data in (("collector", collector), ("analysis", _analysis(collector))):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(data, default=str))
        paths.append(str(path))
    _assert_no_tags(load_agent_input(AgentInputPaths(collector=paths[0], analysis=paths[1])))
