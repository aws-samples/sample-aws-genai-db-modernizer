"""Each designer is told which engines the effective assignment runs (#213).

Without it, a designer's ``unsupported_patterns`` workaround can name an engine the
reality check consolidated away ("OpenSearch would serve it"), and synthesis grounding
(#202) can only drop or annotate that text afterwards. The schema-design step adds the
effective architecture (the engines with an in-scope query, and each eliminated engine
with the one that absorbed it) to every design path's analysis input: the external
request, the Bedrock projection, each design group's input and the Aurora design view.
No test calls a model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.agents.analysis.aurora_mysql_analysis_agent import (
    analyze_for_aurora_mysql_deterministic,
)
from src.agents.analysis.dynamodb_analysis_agent import analyze_for_dynamodb_deterministic
from src.agents.schema_design import handler
from src.agents.schema_design.effective_architecture import (
    effective_architecture,
    effective_engines,
)
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.storage.assignment_versioning import reality_check_output_path
from src.storage.local_store import LocalArtifactStore
from src.tools.schema.base_schema_agent import AgentInputPaths
from src.tools.schema.dynamodb_schema_agent import load_agent_input
from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output

DB, JOB = "ecommerce", "job-213"
ON_DYNAMODB = {"q02-user-by-id", "q08-best-sellers"}
EXPECTED = {
    "engines": ["aurora_mysql", "dynamodb"],
    "eliminated_engines": [{"engine": "opensearch", "absorbed_by": "aurora_mysql"}],
}


def _analysis(collector: dict, engine: str) -> dict:
    analyze = {
        "dynamodb": (analyze_for_dynamodb_deterministic, TargetDatabase.DYNAMODB),
        "aurora_mysql": (analyze_for_aurora_mysql_deterministic, TargetDatabase.AURORA_MYSQL),
    }[engine]
    contract, _trace, _ = analyze[0](
        AnalysisInput(job_id=JOB, collector_output=collector, target_database=analyze[1])
    )
    return contract.model_dump(mode="json")


def _assignment(collector: dict) -> dict:
    return {
        "version": 1,
        "query_assignments": [
            {
                "query_id": q["query_id"],
                "assigned_engine": "dynamodb" if q["query_id"] in ON_DYNAMODB else "aurora_mysql",
                "in_scope": True,
                "source_tables": q["tables_accessed"],
            }
            for q in collector["queries"]["query_patterns"]
        ],
    }


@pytest.fixture
def store(tmp_path: Path) -> LocalArtifactStore:
    collector = get_ecommerce_collector_output()
    store = LocalArtifactStore(base_dir=str(tmp_path / "store"))
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector)
    for engine in ("dynamodb", "aurora_mysql"):
        store.write_json(
            f"{DB}/{JOB}/analysis-{engine}/analysis.json", _analysis(collector, engine)
        )
    store.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", _assignment(collector))
    # The reality check consolidated OpenSearch into Aurora MySQL.
    store.write_json(
        reality_check_output_path(DB, JOB),
        {
            "eliminated_engines": ["opensearch"],
            "before_distribution": {"opensearch": 3, "aurora_mysql": 10, "dynamodb": 2},
            "consolidations": [
                {"from_engine": "opensearch", "to_engine": "aurora_mysql", "query_count": 3}
            ],
        },
    )
    return store


def test_the_external_request_carries_the_effective_architecture(store):
    request = handler.prepare_schema_design_input(JOB, DB, "dynamodb", store, assignment_version=1)
    assert request["analysis_output"]["effective_architecture"] == EXPECTED
    # The analysis on disk is unchanged: the design step adds it.
    on_disk = store.read_json(f"{DB}/{JOB}/analysis-dynamodb/analysis.json")
    assert "effective_architecture" not in on_disk


def test_the_bedrock_projection_carries_it(store, monkeypatch):
    seen: dict = {}

    def fake_dispatch(target_type, collector_path, analysis_path, **_kw):
        seen["projected"] = load_agent_input(
            AgentInputPaths(collector=collector_path, analysis=analysis_path)
        )
        raise RuntimeError("stop before any model call")

    monkeypatch.setattr(handler, "require_valid_guardrail_config", lambda: None)
    monkeypatch.setattr(handler, "_dispatch_schema_agent", fake_dispatch)
    with pytest.raises(RuntimeError, match="stop before"):
        handler.run_schema_design(JOB, DB, "dynamodb", store, assignment_version=1)
    assert seen["projected"]["analysis"]["effective_architecture"] == EXPECTED


def test_each_design_group_carries_it(store):
    handler.run_schema_split(JOB, DB, "dynamodb", store, assignment_version=1)
    groups = [
        store.read_json(k)
        for k in store.list_prefix(f"{DB}/{JOB}/schema-dynamodb/v1/")
        if k.rsplit("/", 1)[-1].startswith("input_group_")
    ]
    assert groups
    assert all(g["analysis_output"]["effective_architecture"] == EXPECTED for g in groups)


def test_the_aurora_design_view_carries_it(store):
    from scripts.run_schema_design import _aurora_delta_request

    request = handler.prepare_schema_design_input(
        JOB, DB, "aurora_mysql", store, assignment_version=1
    )
    view = _aurora_delta_request(request, "aurora_mysql")["design_view"]
    assert view["analysis"]["effective_architecture"] == EXPECTED


def test_a_legacy_version_or_missing_assignment_adds_none(store):
    legacy = handler.prepare_schema_design_input(JOB, DB, "dynamodb", store, assignment_version=0)
    assert "effective_architecture" not in legacy["analysis_output"]
    assert effective_architecture(store, DB, JOB, 7) is None


def test_without_a_reality_check_nothing_is_eliminated(store):
    Path(store.base_dir, reality_check_output_path(DB, JOB)).unlink()
    assert effective_architecture(store, DB, JOB, 1) == {
        "engines": ["aurora_mysql", "dynamodb"],
        "eliminated_engines": [],
    }


def test_an_eliminated_engine_with_no_absorber_says_so(store):
    path = reality_check_output_path(DB, JOB)
    store.write_json(path, {**store.read_json(path), "consolidations": []})
    assert effective_architecture(store, DB, JOB, 1)["eliminated_engines"] == [
        {"engine": "opensearch", "absorbed_by": None}
    ]


def test_effective_engines_count_cache_engines_and_skip_out_of_scope_queries():
    assignment = {
        "query_assignments": [
            {"query_id": "a", "assigned_engine": "aurora_mysql", "cache_engine": "elasticache"},
            {"query_id": "b", "assigned_engine": "opensearch", "in_scope": False},
            {"query_id": "c", "assigned_engine": None},
            "garbage",
        ]
    }
    assert effective_engines(assignment) == ["aurora_mysql", "elasticache"]
    assert effective_engines({}) == []


DESIGNER_SKILLS = (
    "dynamodb-data-modeling.md",
    "documentdb-data-modeling.md",
    "opensearch-index-modeling.md",
    "elasticache-data-modeling.md",
    "aurora_mysql-data-modeling.md",
    "aurora_postgresql-data-modeling.md",
)


@pytest.mark.parametrize("name", DESIGNER_SKILLS)
def test_every_designer_skill_names_only_remaining_engines(name):
    skills = Path(__file__).resolve().parents[4] / "src" / "skills"
    text = (skills / name).read_text(encoding="utf-8")
    line = next(ln for ln in text.splitlines() if ln.startswith("Name only engines that remain"))
    for needle in ("effective_architecture", "`engines`", "`eliminated_engines`", "`absorbed_by`"):
        assert needle in line, (name, needle)
