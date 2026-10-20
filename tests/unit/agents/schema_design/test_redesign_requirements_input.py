"""The designer of the engine serving a query routed on a redesign is given it (#1231).

ADR-031 Decision 2 Amendment 13: routing scores a query after redesign, so the plan holds
only if that engine's design adds the keys routing assumed. The assignment's
access-pattern record carries each redesign and each fixable finding; each design path
reads the engine's requirements from it (``query_pattern.redesign_requirements``) and
keeps only its own queries': the external request (``prepare_schema_design_input``), the
Bedrock run (``run_schema_design``, whose projection ``load_agent_input`` keeps the
filtered collector's queries) and each design group's input (``run_schema_split`` and
``build_group_input``). No test calls a model.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from src.agents.analysis.dynamodb_analysis_agent import analyze_for_dynamodb_deterministic
from src.agents.schema_design import handler
from src.agents.schema_design.group_input import build_group_input
from src.contracts.analysis_input import AnalysisInput, TargetDatabase
from src.contracts.analysis_output import AnalysisOutputContract
from src.storage.local_store import LocalArtifactStore
from src.tools.schema.base_schema_agent import AgentInputPaths
from src.tools.schema.dynamodb_schema_agent import load_agent_input
from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output

DB, JOB = "ecommerce", "job-1231"
ENGINE, OTHER = "dynamodb", "documentdb"


def _finding(engine: str, pattern: str, kind: str, column: str) -> dict:
    return {
        "engine": engine,
        "pattern": pattern,
        "penalty": 20,
        "kind": kind,
        "evidence": f"a key or index on {column} narrows the rows the query reads",
        "columns": [column],
        "new_columns": [column],
    }


# q07 and q08 are on DynamoDB, q09 on Aurora MySQL; q13's finding is against DocumentDB.
FINDINGS = {
    "q07-top-rated-products": [_finding(ENGINE, "frequent-full-scan", "intrinsic", "x.y")],
    "q08-best-sellers": [_finding(ENGINE, "complex-aggregation", "fixable", "products.name")],
    "q09-daily-revenue": [_finding(ENGINE, "complex-aggregation", "fixable", "orders.status")],
    "q13-full-catalog-export": [_finding(OTHER, "heavy-cross-collection-joins", "fixable", "p.x")],
}
ON_ENGINE = {"q07-top-rated-products", "q08-best-sellers", "q02-user-by-id"}


# q02 is scored after redesign (a key on users.email it assumes); q09's, on Aurora MySQL,
# is out of DynamoDB's scope.
REDESIGNS = {
    q: {
        "assumed_keys": [
            {
                "column": c,
                "table_id": f"ecommerce.{c.split('.')[0]}",
                "via": "equality",
                "cardinality": "unknown",
            }
        ],
        "class": "key_scoped",
        "reason": "",
        "facts": [],
        "class_scores": {},
    }
    for q, c in (("q02-user-by-id", "users.email"), ("q09-daily-revenue", "orders.total_amount"))
}


def _record(findings: dict[str, list[dict]] | None = None, version: int = 2) -> dict:
    collector = get_ecommerce_collector_output()
    rows = []
    for q in collector["queries"]["query_patterns"]:
        row = {"query_id": q["query_id"], "class": "ad_hoc_filter", "reason": "", "facts": []}
        if version >= 2:
            row["anti_pattern_findings"] = (findings or FINDINGS).get(q["query_id"], [])
            row["redesign"] = REDESIGNS.get(q["query_id"])
        rows.append(row)
    return {"schema_version": version, "queries": rows}


def _analysis(collector: dict) -> dict:
    contract, _trace, _ = analyze_for_dynamodb_deterministic(
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
                "assigned_engine": ENGINE if q["query_id"] in ON_ENGINE else "aurora_mysql",
                "in_scope": True,
                "source_tables": q["tables_accessed"],
            }
            for q in collector["queries"]["query_patterns"]
        ]
    }


@pytest.fixture
def store(tmp_path: Path) -> LocalArtifactStore:
    collector = get_ecommerce_collector_output()
    store = LocalArtifactStore(base_dir=str(tmp_path / "store"))
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector)
    store.write_json(f"{DB}/{JOB}/analysis-{ENGINE}/analysis.json", _analysis(collector))
    store.write_json(f"{DB}/{JOB}/assignment/v1/assignment.json", _assignment(collector))
    store.write_json(f"{DB}/{JOB}/assignment/v1/access_patterns.json", _record())
    return store


EXPECTED = [
    {
        "query_id": "q02-user-by-id",
        "routing_depends": False,
        "assumed_keys": [{"column": "users.email", "via": "equality", "cardinality": "unknown"}],
        "class_as_written": "ad_hoc_filter",
        "class_after_redesign": "key_scoped",
        "lifted_anti_patterns": [],
        "evidence": [],
    },
    {
        "query_id": "q08-best-sellers",
        "routing_depends": True,
        "assumed_keys": [{"column": "products.name", "via": "equality", "cardinality": "unknown"}],
        "class_as_written": "ad_hoc_filter",
        "class_after_redesign": None,
        "lifted_anti_patterns": ["complex-aggregation"],
        "evidence": ["a key or index on products.name narrows the rows the query reads"],
    },
]


def test_the_external_request_carries_the_requirements_of_its_own_queries(store):
    request = handler.prepare_schema_design_input(JOB, DB, ENGINE, store, assignment_version=1)
    # Not q07 (intrinsic), not q09 (another engine serves it), not q13 (another engine's).
    assert request["analysis_output"]["redesign_requirements"] == EXPECTED
    # The analysis on disk is unchanged: the findings come from the assignment's record.
    on_disk = store.read_json(f"{DB}/{JOB}/analysis-{ENGINE}/analysis.json")
    assert on_disk.get("redesign_requirements") is None


def test_the_bedrock_run_gives_the_projection_the_same_list(store, monkeypatch, tmp_path):
    seen: dict = {}

    def fake_dispatch(target_type, collector_path, analysis_path, **_kw):
        seen["projected"] = load_agent_input(
            AgentInputPaths(collector=collector_path, analysis=analysis_path)
        )
        raise RuntimeError("stop before any model call")

    monkeypatch.setattr(handler, "require_valid_guardrail_config", lambda: None)
    monkeypatch.setattr(handler, "_dispatch_schema_agent", fake_dispatch)
    with pytest.raises(RuntimeError, match="stop before"):
        handler.run_schema_design(JOB, DB, ENGINE, store, assignment_version=1)
    assert seen["projected"]["analysis"]["redesign_requirements"] == EXPECTED


def test_each_design_group_carries_only_its_own_queries_requirements(store):
    handler.run_schema_split(JOB, DB, ENGINE, store, assignment_version=1)
    groups = [
        store.read_json(k)
        for k in store.list_prefix(f"{DB}/{JOB}/schema-{ENGINE}/v1/")
        if k.rsplit("/", 1)[-1].startswith("input_group_")
    ]
    assert groups, "the split wrote no group input"
    carried = []
    for group in groups:
        own = {q["query_id"] for q in group["collector_output"]["queries"]["query_patterns"]}
        findings = group["analysis_output"].get("redesign_requirements") or []
        assert {f["query_id"] for f in findings} <= own
        carried += findings
    assert carried == EXPECTED


def _one_group(qids: set[str]) -> dict:
    collector = get_ecommerce_collector_output()
    analysis = {**_analysis(collector), "redesign_requirements": EXPECTED}
    queries = [q for q in collector["queries"]["query_patterns"] if q["query_id"] in qids]
    tables = [
        t
        for t in collector["database_schema"]["tables"]
        if any(t["table_id"] in q["tables_accessed"] for q in queries)
    ]
    return build_group_input(
        job_id=JOB,
        database_name=DB,
        engine=ENGINE,
        group_index=0,
        group_name="g",
        primary_tables=[t["table_id"] for t in tables],
        group_queries=queries,
        group_tables=tables,
        collector_output=collector,
        analysis_output=analysis,
        total_queries=len(collector["queries"]["query_patterns"]),
    )


def test_build_group_input_keeps_the_groups_own_requirements_only():
    assert "redesign_requirements" not in _one_group({"q07-top-rated-products"})["analysis_output"]
    mine = _one_group({"q07-top-rated-products", "q08-best-sellers"})
    assert mine["analysis_output"]["redesign_requirements"] == EXPECTED[1:]


def test_the_projection_keeps_only_the_filtered_collectors_requirements(tmp_path):
    collector = get_ecommerce_collector_output()
    analysis = {**_analysis(collector), "redesign_requirements": EXPECTED}
    filtered = copy.deepcopy(collector)
    filtered["queries"]["query_patterns"] = [
        q
        for q in collector["queries"]["query_patterns"]
        if q["query_id"] not in ("q08-best-sellers", "q02-user-by-id")
    ]
    paths = []
    for name, data in (("collector", filtered), ("analysis", analysis)):
        path = tmp_path / f"{name}.json"
        path.write_text(json.dumps(data, default=str))
        paths.append(str(path))
    projected = load_agent_input(AgentInputPaths(collector=paths[0], analysis=paths[1]))
    assert projected["analysis"]["redesign_requirements"] is None


@pytest.mark.passes_on_main  # main gives no designer a finding either
def test_a_record_written_before_the_redesign_adds_none(store):
    store.write_json(f"{DB}/{JOB}/assignment/v1/access_patterns.json", _record(version=1))
    request = handler.prepare_schema_design_input(JOB, DB, ENGINE, store, assignment_version=1)
    assert request["analysis_output"].get("redesign_requirements") is None


@pytest.mark.passes_on_main  # main gives no designer a finding either
def test_no_record_and_a_legacy_version_add_none(store, tmp_path):
    (tmp_path / "store" / DB / JOB / "assignment" / "v1" / "access_patterns.json").unlink()
    request = handler.prepare_schema_design_input(JOB, DB, ENGINE, store, assignment_version=1)
    assert request["analysis_output"].get("redesign_requirements") is None
    store.write_json(f"{DB}/{JOB}/assignment/v1/access_patterns.json", _record())
    legacy = handler.prepare_schema_design_input(JOB, DB, ENGINE, store, assignment_version=0)
    assert legacy["analysis_output"].get("redesign_requirements") is None


def test_a_saved_analysis_without_the_field_still_loads():
    analysis = _analysis(get_ecommerce_collector_output())
    old = {**analysis, "contract_version": "2.3"}
    assert AnalysisOutputContract.model_validate(old).redesign_requirements is None


def test_the_aurora_design_view_carries_the_index_each_requirement_needs():
    from src.contracts.schema_design_input import AgentAnalysisInput
    from src.tools.schema.aurora_common.design_view import _analysis_summary

    analysis = AgentAnalysisInput.model_validate(
        {
            "contract_version": "2.3",
            "patterns_detected": [],
            "table_recommendations": [],
            "redesign_requirements": EXPECTED,
        }
    )
    assert _analysis_summary(analysis)["redesign_requirements"] == [
        {"query_id": "q02-user-by-id", "routing_depends": False, "index_on": ["users.email"]},
        {"query_id": "q08-best-sellers", "routing_depends": True, "index_on": ["products.name"]},
    ]
