"""Every model that designs or reviews a context group gets the context-table rule (#1243).

A Bedrock smoke run on trailgear (PR #1385) showed why. Group 0 read ``Shipment`` as
context; the table's two writes were in its owner's group. Its PE review asked for the
writes a sparse GSI depended on, so the designer added a ``Create shipment`` pattern
with an invented query id (``DERIVED-SHIPMENT-INSERT``), and the merge failed on scope.
The rule says a context group never writes a context table, never invents a query
id, and never designs an index whose correctness depends on writes it does not serve:
the owner designs the table's writes. The designer's input and prompt, and both PE
review paths (Bedrock and the external request), carry it word for word.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from src.agents.prompt_framing import is_framed
from src.agents.schema_design.group_input import CONTEXT_TABLE_RULE, build_group_input
from src.agents.schema_design.pe_review_external import prepare_pe_review_llm_input
from src.storage.local_store import LocalArtifactStore

CONTEXT = [{"table": "trailgear.Shipment", "owner_group": 1}]


def test_the_rule_says_the_owner_designs_the_writes() -> None:
    rule = CONTEXT_TABLE_RULE
    assert "never adds an access pattern that writes a context table" in rule
    assert "never puts a query id in `query_ids` that is not in its own input" in rule
    assert "never designs an index or attribute whose correctness depends on writes" in rule
    assert "the owner designs the table's writes" in rule


def _input(context_owners: dict[str, int] | None) -> dict:
    return build_group_input(
        job_id="j",
        database_name="trailgear",
        engine="dynamodb",
        group_index=0,
        group_name="g",
        primary_tables=["trailgear.SalesOrder"],
        group_queries=[{"query_id": "q1", "tables_accessed": ["trailgear.SalesOrder"]}],
        group_tables=[],
        collector_output={},
        analysis_output={},
        total_queries=1,
        context_owners=context_owners,
    )


def test_a_context_groups_input_carries_the_rule() -> None:
    data = _input({"trailgear.Shipment": 1})
    assert data["group_context_tables"] == CONTEXT
    assert data["group_context_rule"] == CONTEXT_TABLE_RULE


def test_a_group_with_no_context_tables_has_no_rule() -> None:
    data = _input(None)
    assert "group_context_tables" not in data and "group_context_rule" not in data


def test_the_bedrock_pe_reviewer_is_given_the_rule_and_the_context_tables() -> None:
    from src.tools.schema import dynamodb_schema_agent as agent

    prompts: list[str] = []

    class _Reviewer:
        def __init__(self, **_: Any) -> None:
            pass

        def __call__(self, prompt: str) -> SimpleNamespace:
            prompts.append(prompt)
            return SimpleNamespace(structured_output=None)

    model: Any = object()
    design: Any = SimpleNamespace(model_dump=lambda mode="json": {"table_definitions": []})
    with (
        patch.object(agent, "Agent", _Reviewer),
        patch.object(agent, "_build_pe_input", lambda d: d),
        patch.object(agent, "load_skill", lambda _p=None: "skill"),
    ):
        try:
            agent._invoke_pe_reviewer(model, design, {"context_tables": CONTEXT})
        except Exception:  # noqa: BLE001 -- no structured output from the stand-in
            pass
    assert prompts, "the reviewer was called"
    assert CONTEXT_TABLE_RULE in prompts[0]
    assert is_framed(prompts[0], json.dumps(CONTEXT, indent=2))
    prompts.clear()
    with (
        patch.object(agent, "Agent", _Reviewer),
        patch.object(agent, "_build_pe_input", lambda d: d),
        patch.object(agent, "load_skill", lambda _p=None: "skill"),
    ):
        try:
            agent._invoke_pe_reviewer(model, design, {})
        except Exception:  # noqa: BLE001
            pass
    assert prompts and CONTEXT_TABLE_RULE not in prompts[0]


def test_the_bedrock_designer_hands_its_context_tables_to_the_pe_review(tmp_path: Path) -> None:
    from src.tools.schema import dynamodb_schema_agent as agent

    path = tmp_path / "context.json"
    path.write_text(json.dumps(CONTEXT), encoding="utf-8")
    seen: dict[str, Any] = {}

    def run(self, designer_prompt: str, input_summary: dict | None = None):
        seen["summary"] = input_summary
        raise RuntimeError("stop")

    with (
        patch.object(agent, "_build_model", return_value=object()),
        patch.object(agent, "Agent", lambda **_: object()),
        patch.object(agent, "_project_input", lambda _p: {"design_metrics": None}),
        patch.object(agent, "_collector_for_columns", lambda _p: {}),
        patch.object(agent.SchemaDesignRunner, "run", run),
    ):
        try:
            agent.run_dynamodb_schema_agent(
                collector_path=str(path), analysis_path=str(path), context_tables_path=str(path)
            )
        except RuntimeError:
            pass
    assert seen["summary"]["context_tables"] == CONTEXT


def test_the_external_pe_request_carries_the_rule(tmp_path: Path) -> None:
    store = LocalArtifactStore(base_dir=str(tmp_path))
    prefix, v = "trailgear/j", 2
    base = f"{prefix}/schema-dynamodb/v{v}"
    store.write_json(f"{prefix}/collector/output.json", {"queries": {"query_patterns": []}})
    store.write_json(
        f"{base}/groups_manifest.json",
        {"groups": [{"group_index": 0}, {"group_index": 1}], "total_groups": 2},
    )
    for idx, ctx in ((0, CONTEXT), (1, None)):
        group_input: dict[str, Any] = {
            "collector_output": {"queries": {"query_patterns": [{"query_id": f"q{idx}"}]}}
        }
        if ctx:
            group_input["group_context_tables"] = ctx
            group_input["group_context_rule"] = CONTEXT_TABLE_RULE
        store.write_json(f"{base}/input_group_{idx}.json", group_input)
        store.write_json(
            f"{base}/schema_draft_group_{idx}.json",
            {"table_definitions": [], "access_patterns": [{"query_ids": [f"q{idx}"]}]},
        )
    prepare_pe_review_llm_input(store, "trailgear", "j", v, engines=["dynamodb"])
    requests = {
        n: store.read_json(f"{prefix}/llm_requests/pe_review_dynamodb_group_{n}.json")
        for n in (0, 1)
    }
    assert requests[0]["context_tables"] == CONTEXT
    assert requests[0]["context_table_rule"] == CONTEXT_TABLE_RULE
    assert "context_tables" not in requests[1] and "context_table_rule" not in requests[1]


def test_the_review_command_tells_the_reviewer_to_apply_the_rule() -> None:
    text = (Path(__file__).resolve().parents[4] / ".claude/commands/review-schema.md").read_text(
        encoding="utf-8"
    )
    assert "`context_table_rule`" in text and "`context_tables`" in text
