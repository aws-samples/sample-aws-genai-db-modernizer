# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1991: a peak-to-average ratio the customer sets (the assumptions file) is the peak
every designer's ``design_metrics`` take, on each path: the external request, the
DynamoDB group inputs and the Bedrock designers' input tool. The gate states the same
peak, so what it says is what the designs were written at."""

from __future__ import annotations

import json

import pytest

from src.shared.assumptions import record_assumptions, record_growth
from src.storage.local_store import LocalArtifactStore
from tests.fixtures.ecommerce_collector_output import get_ecommerce_collector_output
from tests.unit.agents.test_group_input import _analysis

DB, JOB = "ecommerce", "j1"


@pytest.fixture
def store(tmp_path) -> LocalArtifactStore:
    store = LocalArtifactStore(str(tmp_path))
    collector = get_ecommerce_collector_output()
    store.write_json(f"{DB}/{JOB}/collector/output.json", collector)
    for engine in ("dynamodb", "documentdb", "opensearch"):
        store.write_json(f"{DB}/{JOB}/analysis-{engine}/analysis.json", _analysis(collector))
    return store


def _peaks(metrics: dict) -> tuple[str, float]:
    return metrics["peak_basis"], metrics["peak_to_avg_ratio"]


def _scaled(metrics: dict) -> bool:
    return all(q["design_rps"] == round(q["base_rps"] * 2.5, 4) for q in metrics["queries"])


@pytest.mark.parametrize("engine", ["dynamodb", "documentdb", "opensearch"])
def test_the_external_request_takes_the_customers_peak(store, engine) -> None:
    from src.agents.schema_design.handler import prepare_schema_design_input

    assert _peaks(prepare_schema_design_input(JOB, DB, engine, store)["design_metrics"]) == (
        "not_measured",
        1.0,
    )
    record_assumptions(store, DB, JOB, {"peak_to_avg_ratio": 2.5}, via="file")
    metrics = prepare_schema_design_input(JOB, DB, engine, store)["design_metrics"]
    assert _peaks(metrics) == ("customer", 2.5)
    assert _scaled(metrics)


def test_growth_alone_never_reaches_a_design_rate(store) -> None:
    from src.agents.schema_design.handler import prepare_schema_design_input

    record_growth(store, DB, JOB, 10, via="cli")
    metrics = prepare_schema_design_input(JOB, DB, "dynamodb", store)["design_metrics"]
    assert _peaks(metrics) == ("not_measured", 1.0)
    assert all(q["design_rps"] == round(q["base_rps"], 4) for q in metrics["queries"])


def test_the_dynamodb_group_inputs_take_the_customers_peak(store, tmp_path) -> None:
    from src.agents.schema_design.group_splitter import split_schema_input

    record_assumptions(store, DB, JOB, {"peak_to_avg_ratio": 2.5}, via="file")
    collector = store.read_json(f"{DB}/{JOB}/collector/output.json")
    manifest = split_schema_input(
        job_id=JOB,
        database_name=DB,
        engine="dynamodb",
        collector_output=collector,
        analysis_output=_analysis(collector),
        queries=collector["queries"]["query_patterns"],
        store=store,
    )
    assert manifest.groups
    for group in manifest.groups:
        text = (tmp_path / DB / JOB / "schema-dynamodb/v1" / group.input_file).read_text()
        metrics = json.loads(text)["design_metrics"]
        assert _peaks(metrics) == ("customer", 2.5)
        assert _scaled(metrics)


@pytest.mark.parametrize("engine", ["dynamodb", "documentdb", "opensearch"])
def test_the_bedrock_input_tool_takes_the_customers_peak(store, tmp_path, engine) -> None:
    """The handler writes the call's collector with the job's design context; the
    designer's input tool reads it from there, never from another job's decision."""
    from src.agents.schema_design.handler import with_design_context
    from src.tools.schema import (
        documentdb_schema_agent,
        dynamodb_schema_agent,
        opensearch_schema_agent,
    )
    from src.tools.schema.base_schema_agent import AgentInputPaths

    record_assumptions(store, DB, JOB, {"peak_to_avg_ratio": 2.5}, via="file")
    from src.storage.collector_for_models import read_collector_for_models

    # As the handler writes it: the collector the models read, with the design context.
    collector = with_design_context(read_collector_for_models(store, DB, JOB), store, DB, JOB)
    collector_path, analysis_path = tmp_path / "c.json", tmp_path / "a.json"
    collector_path.write_text(json.dumps(collector))
    analysis_path.write_text(json.dumps(_analysis(collector)))
    mod = {
        "dynamodb": dynamodb_schema_agent,
        "documentdb": documentdb_schema_agent,
        "opensearch": opensearch_schema_agent,
    }[engine]
    load = getattr(mod, "_project_input", None) or mod.load_agent_input
    projected = load(AgentInputPaths(collector=str(collector_path), analysis=str(analysis_path)))
    assert _peaks(projected["design_metrics"]) == ("customer", 2.5)
    # The model reads the projection: the design context is no table, query or key.
    assert "_design_context" not in json.dumps(projected["collector"])


def test_a_job_without_a_customer_peak_writes_no_design_context(store) -> None:
    from src.agents.schema_design.handler import with_design_context

    collector = store.read_json(f"{DB}/{JOB}/collector/output.json")
    assert with_design_context(collector, store, DB, JOB) is collector


class _Captured(Exception):
    """Raised by the fake designer once it has read its collector file."""


def _capture(monkeypatch) -> list[dict]:
    from pathlib import Path

    from src.agents.schema_design import handler

    seen: list[dict] = []

    def dispatch(target_type, collector_path=None, analysis_path=None, **_kw):
        seen.append(json.loads(Path(collector_path).read_text()))
        raise _Captured

    monkeypatch.setattr(handler, "_dispatch_schema_agent", dispatch)
    monkeypatch.setattr(handler, "require_valid_guardrail_config", lambda: None)
    return seen


def test_every_bedrock_designer_file_carries_the_customers_peak(store, monkeypatch, tmp_path):
    """The one-call design, the cascade pass, a group design and a revision each write the
    collector file their designer reads with the job's design context (#1991)."""
    from src.agents.schema_design import handler, revision_handler
    from src.contracts.schema_revision_models import NewPattern, SchemaRevisionRequest
    from tests.unit.agents.schema_design import test_group_decision_trace as groups

    seen = _capture(monkeypatch)
    record_assumptions(store, DB, JOB, {"peak_to_avg_ratio": 2.5}, via="file")
    with pytest.raises(_Captured):
        handler.run_schema_design(JOB, DB, "opensearch", store)
    # The cascade pass (rerouted queries injected into an engine's design, #1991 review).
    with pytest.raises(_Captured):
        handler.run_schema_design_with_injected(JOB, DB, "opensearch", store, set())
    request = SchemaRevisionRequest(
        base_version=1,
        pattern_modifications=[],
        table_modifications=[],
        new_patterns=[NewPattern(description="d", target_engine="dynamodb", source_tables=[])],
    )
    with pytest.raises(_Captured):
        revision_handler._dispatch_llm_redesign(JOB, DB, "dynamodb", {}, request, store)
    group_store = groups._seed(tmp_path / "groups", "documentdb", None)
    record_assumptions(group_store, groups.DB, groups.JOB, {"peak_to_avg_ratio": 2.5}, via="file")
    monkeypatch.setattr(handler, "run_schema_split", groups._fake_split({0: groups.GROUP_A}))
    with pytest.raises(_Captured):
        handler.run_schema_design_auto(
            groups.JOB, groups.DB, "documentdb", group_store, assignment_version=1
        )
    assert [c.get("_design_context") for c in seen] == [
        {"peak_to_avg_ratio": 2.5, "peak_origin": "file"}
    ] * 4


SKILLS = (
    "src/skills/dynamodb-data-modeling.md",
    "src/skills/documentdb-data-modeling.md",
    "src/skills/opensearch-index-modeling.md",
)


def test_every_metric_designer_prompt_says_a_customer_peak_scales_design_rps() -> None:
    """#1991 review: a customer peak scales design_rps, and the DynamoDB and OpenSearch
    prompts said only a CloudWatch peak did. The three prompts state the rule alike."""
    from pathlib import Path

    root = Path(__file__).resolve().parents[4]
    rules = []
    for skill in SKILLS:
        line = next(x for x in (root / skill).read_text().splitlines() if "`design_rps` = " in x)
        rule = line.split("`design_rps` = ", 1)[1].split(" A row with", 1)[0]
        assert "`customer` (the customer's own ratio, set in the assumptions file)" in rule
        assert "`cloudwatch`" in rule and "else 1" in rule
        rules.append(rule)
    assert len(set(rules)) == 1, rules
