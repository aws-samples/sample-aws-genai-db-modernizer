"""Each designer is told how its engine serves the write-group members it fits poorly (#1362).

A table has one writer (ADR-031 Decision 1 Amendment 9), so its write group can carry a
statement the engine serves poorly. The schema-design step reads them once per call from
the assignment version (``carried_writes.carried_write_records``, the function the risk
register reads too) and every design path keeps only its own queries': the external
request, the Bedrock projection, each design group's input and the Aurora design view.
The records are stubbed here (their computation is ``test_carried_writes.py``'s); no test
calls a model.
"""

from __future__ import annotations

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

DB, JOB = "ecommerce", "job-1362"
ENGINE = "dynamodb"
ON_ENGINE = {"q02-user-by-id", "q08-best-sellers"}


def _record(qid: str, engine: str = ENGINE) -> dict:
    return {
        "query_id": qid,
        "table_id": "ecommerce.users",
        "engine": engine,
        "score": 14.0,
        "floor": 50.0,
        "group_size": 3,
        "statement": "DELETE",
        "intent": "retention-delete",
        "intent_name": "Retention delete",
        "intent_summary": "Removes rows once they pass an age or expiry cut-off.",
        "approach": "Use Time to Live.",
        "docs": "Amazon DynamoDB Developer Guide, Using time to live (TTL) in DynamoDB",
    }


CARRIED = [_record("q02-user-by-id"), _record("q08-best-sellers")]


def _analysis(collector: dict) -> dict:
    contract, _trace, _ = analyze_for_dynamodb_deterministic(
        AnalysisInput(
            job_id=JOB, collector_output=collector, target_database=TargetDatabase.DYNAMODB
        )
    )
    return contract.model_dump(mode="json")


@pytest.fixture
def store(tmp_path: Path, monkeypatch) -> LocalArtifactStore:
    collector = get_ecommerce_collector_output()
    store = LocalArtifactStore(base_dir=str(tmp_path / "store"))
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector)
    store.write_json(f"{DB}/{JOB}/analysis-{ENGINE}/analysis.json", _analysis(collector))
    store.write_json(
        f"{DB}/{JOB}/assignment/v1/assignment.json",
        {
            "write_groups": {"ecommerce.users": ["q02-user-by-id", "q08-best-sellers", "x"]},
            "query_assignments": [
                {
                    "query_id": q["query_id"],
                    "assigned_engine": ENGINE if q["query_id"] in ON_ENGINE else "aurora_mysql",
                    "in_scope": True,
                    "source_tables": q["tables_accessed"],
                }
                for q in collector["queries"]["query_patterns"]
            ],
        },
    )
    seen: dict = {}

    def records(assignment, collector, analyses, triage, engine=None):
        seen["engine"] = engine
        return [r for r in CARRIED if engine in (None, r["engine"])]

    monkeypatch.setattr(handler, "carried_write_records", records)
    store.seen = seen  # type: ignore[attr-defined]
    return store


def test_the_external_request_carries_the_engines_own_carried_writes(store):
    request = handler.prepare_schema_design_input(JOB, DB, ENGINE, store, assignment_version=1)
    assert request["analysis_output"]["carried_writes"] == CARRIED
    assert store.seen["engine"] == ENGINE
    # The analysis on disk is unchanged: the design step adds them.
    on_disk = store.read_json(f"{DB}/{JOB}/analysis-{ENGINE}/analysis.json")
    assert on_disk.get("carried_writes") is None


def test_the_bedrock_projection_carries_the_intent_and_approach(store, monkeypatch):
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
    projected = seen["projected"]["analysis"]["carried_writes"]
    assert [c["query_id"] for c in projected] == sorted(ON_ENGINE)
    assert projected[0]["intent_name"] == "Retention delete"
    assert projected[0]["approach"] == "Use Time to Live."


def test_each_design_group_carries_only_its_own(store):
    handler.run_schema_split(JOB, DB, ENGINE, store, assignment_version=1)
    groups = [
        store.read_json(k)
        for k in store.list_prefix(f"{DB}/{JOB}/schema-{ENGINE}/v1/")
        if k.rsplit("/", 1)[-1].startswith("input_group_")
    ]
    assert groups
    found = []
    for group in groups:
        own = {q["query_id"] for q in group["collector_output"]["queries"]["query_patterns"]}
        carried = group["analysis_output"].get("carried_writes") or []
        assert {c["query_id"] for c in carried} <= own
        found += carried
    assert sorted(c["query_id"] for c in found) == sorted(ON_ENGINE)


def test_build_group_input_keeps_the_groups_own_only():
    collector = get_ecommerce_collector_output()
    analysis = {**_analysis(collector), "carried_writes": CARRIED}
    queries = [
        q for q in collector["queries"]["query_patterns"] if q["query_id"] == "q02-user-by-id"
    ]
    tables = [
        t
        for t in collector["database_schema"]["tables"]
        if any(t["table_id"] in q["tables_accessed"] for q in queries)
    ]
    group = build_group_input(
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
    assert group["analysis_output"]["carried_writes"] == CARRIED[:1]


def test_a_job_with_no_carried_write_adds_none(store, monkeypatch):
    monkeypatch.setattr(handler, "carried_write_records", lambda *a, **k: [])
    request = handler.prepare_schema_design_input(JOB, DB, ENGINE, store, assignment_version=1)
    assert request["analysis_output"].get("carried_writes") is None
    legacy = handler.prepare_schema_design_input(JOB, DB, ENGINE, store, assignment_version=0)
    assert legacy["analysis_output"].get("carried_writes") is None


def test_an_assignment_before_write_groups_adds_none(store, monkeypatch):
    path = f"{DB}/{JOB}/assignment/v1/assignment.json"
    assignment = store.read_json(path)
    assignment.pop("write_groups")
    store.write_json(path, assignment)
    monkeypatch.setattr(handler, "carried_write_records", lambda *a, **k: pytest.fail("called"))
    request = handler.prepare_schema_design_input(JOB, DB, ENGINE, store, assignment_version=1)
    assert request["analysis_output"].get("carried_writes") is None


def test_a_saved_analysis_without_the_field_still_loads():
    analysis = _analysis(get_ecommerce_collector_output())
    old = {**analysis, "contract_version": "2.4"}
    assert AnalysisOutputContract.model_validate(old).carried_writes is None


def test_the_aurora_design_view_carries_each_intent_and_approach():
    from src.contracts.schema_design_input import AgentAnalysisInput
    from src.tools.schema.aurora_common.design_view import _analysis_summary

    analysis = AgentAnalysisInput.model_validate(
        {
            "contract_version": "2.4",
            "patterns_detected": [],
            "table_recommendations": [],
            "carried_writes": [_record("q1", "aurora_postgresql")],
        }
    )
    assert _analysis_summary(analysis)["carried_writes"] == [
        {"query_id": "q1", "intent": "Retention delete", "approach": "Use Time to Live."}
    ]


def test_the_request_is_plain_json(store):
    request = handler.prepare_schema_design_input(JOB, DB, ENGINE, store, assignment_version=1)
    json.dumps(request["analysis_output"]["carried_writes"])


SKILLS = (
    "dynamodb-data-modeling.md",
    "documentdb-data-modeling.md",
    "opensearch-index-modeling.md",
    "aurora_postgresql-data-modeling.md",
    "aurora_mysql-data-modeling.md",
)


def test_the_five_designer_skills_say_the_same_thing():
    """#1370 review: one paragraph tells every designer to model the intent, not the
    source shape; each skill adds only its engine's own sentence."""
    import re

    skills = Path(__file__).resolve().parents[4] / "src" / "skills"
    cores = set()
    for name in SKILLS:
        text = (skills / name).read_text(encoding="utf-8")
        line = next(ln for ln in text.splitlines() if "carried_writes" in ln and "(#1362)" in ln)
        core = re.search(
            r"write-group members this engine serves poorly \(#1362\)\..*?`trade_offs`\.", line
        )
        assert core, name
        cores.add(core.group(0))
    assert len(cores) == 1
    (core,) = cores
    assert "Model the statement's intent, not its source shape" in core


def test_the_aurora_skills_never_put_a_constraint_or_trigger_in_the_delta():
    skills = Path(__file__).resolve().parents[4] / "src" / "skills"
    for name in SKILLS[3:]:
        line = next(
            ln
            for ln in (skills / name).read_text(encoding="utf-8").splitlines()
            if "carried_writes" in ln and "(#1362)" in ln
        )
        assert "in the delta" not in line and "`optimizations`" in line and "`add_indexes`" in line


def test_the_aurora_delta_prompt_allows_an_index_a_carried_write_needs():
    from src.tools.schema.aurora_common.bedrock_delta import designer_prompt

    prompt = designer_prompt("aurora_postgresql", {"migration_strategy": "carry_over"})
    assert "a hot query or a carried write's approach" in prompt
