"""Bedrock Aurora agents design a delta against the deterministic draft (issue #273).

The designer is prompted with the compact design view (never the full draft)
and returns an ``AuroraDesignDeltaContract``; the agent merges it into the
draft and returns the full engine contract. No model is called: the runner and
the Strands ``Agent`` are stubbed.
"""

from __future__ import annotations

import logging

import pytest

import src.tools.schema.aurora_mysql_schema_agent as mysql_mod
import src.tools.schema.aurora_postgresql_schema_agent as pg_mod
from src.contracts.aurora_design_delta import AuroraDesignDeltaContract
from src.contracts.aurora_mysql_model_output import AuroraMySQLModelOutputContract
from src.contracts.aurora_postgresql_model_output import AuroraPostgresqlModelOutputContract

_ENGINES = {
    "aurora_postgresql": (
        pg_mod,
        pg_mod.run_aurora_postgresql_schema_agent,
        AuroraPostgresqlModelOutputContract,
        "oracle",
        "translate",
    ),
    "aurora_mysql": (
        mysql_mod,
        mysql_mod.run_aurora_mysql_schema_agent,
        AuroraMySQLModelOutputContract,
        "mysql",
        "carry_over",
    ),
}


def _agent_input(source_engine: str, tables: list[dict] | None = None) -> dict:
    if tables is None:
        tables = [
            {
                "table_id": "t1",
                "table_name": "users",
                "row_count": 5,
                "primary_key": ["id"],
                "columns": [
                    {
                        "column_name": "id",
                        "normalized_data_type": "integer",
                        "nullable": False,
                        "is_auto_increment": True,
                    },
                    {"column_name": "nick", "nullable": True},
                ],
            }
        ]
    return {
        "collector": {
            "contract_version": "3.0",
            "job_id": "job-1",
            "source_database_name": "sales",
            "source_database_engine": source_engine,
            "collection_timestamp": "2026-01-01T00:00:00Z",
            "tables": tables,
            "queries": {"query_patterns": []},
        },
        "analysis": {},
        "context": {},
        "decision_trace": {},
        "raw_collector": {
            "database_schema": {
                "tables": [
                    {
                        "table_name": "users",
                        "columns": [{"column_name": "nick", "data_type": "varchar2"}],
                    }
                ]
            }
        },
    }


def _stub(monkeypatch, mod, agent_input: dict, deltas: list[dict], captured: dict) -> None:
    def fake_run(self, designer_prompt, input_summary):
        captured["prompt"] = designer_prompt
        captured["summary"] = input_summary
        captured["output_model"] = self.output_model
        return AuroraDesignDeltaContract.model_validate(deltas[0]), {"stubbed": True}

    def fake_invoke(self, prompt, previous_output=None):
        captured["correction_prompt"] = prompt
        return AuroraDesignDeltaContract.model_validate(deltas[1])

    monkeypatch.setattr(mod.SchemaDesignRunner, "run", fake_run)
    monkeypatch.setattr(mod.SchemaDesignRunner, "_invoke_designer", fake_invoke)
    monkeypatch.setattr(mod, "load_agent_input", lambda: agent_input)
    monkeypatch.setattr(mod, "_build_model", lambda: object())

    def fake_agent(**kwargs):
        captured.setdefault("agents", []).append(kwargs)
        return object()

    monkeypatch.setattr(mod, "Agent", fake_agent)


@pytest.mark.parametrize("engine", sorted(_ENGINES))
def test_agent_designs_a_delta_from_the_view_and_returns_the_merged_contract(engine, monkeypatch):
    mod, run, contract, source_engine, strategy = _ENGINES[engine]
    captured: dict = {}
    delta = {
        "delta_version": "1.0",
        "type_rules": [{"source_data_type": "VARCHAR2", "aurora_type": "VARCHAR(64)"}],
        "trade_offs": [{"description": "d", "impact": "i"}],
    }
    _stub(monkeypatch, mod, _agent_input(source_engine), [delta], captured)

    result, trace = run(collector_path="unused", analysis_path="unused")

    assert isinstance(result, contract)
    assert result.migration_strategy == strategy
    nick = next(c for c in result.table_definitions[0].columns if c.name == "nick")
    assert nick.aurora_type == "VARCHAR(64)"
    assert "VARCHAR(64)" in result.generated_ddl
    assert trace["stubbed"] is True
    assert trace["delta_merge"]["errors"] == []
    # The designer returns the delta contract and is prompted with the view only.
    assert captured["output_model"] is AuroraDesignDeltaContract
    assert captured["agents"][0]["structured_output_model"] is AuroraDesignDeltaContract
    assert strategy in captured["prompt"]
    assert "id BIGINT" in captured["prompt"]
    assert "residual_types" in captured["prompt"]
    assert "full_ddl" not in captured["prompt"]
    assert captured["summary"]["table_count"] == 1


@pytest.mark.parametrize("engine", sorted(_ENGINES))
def test_merge_errors_get_one_correction_round(engine, monkeypatch):
    mod, run, contract, source_engine, _ = _ENGINES[engine]
    captured: dict = {}
    bad = {"delta_version": "1.0", "tables": [{"table_name": "ghosts"}]}
    good = {"delta_version": "1.0"}
    _stub(monkeypatch, mod, _agent_input(source_engine), [bad, good], captured)

    result, trace = run(collector_path="unused", analysis_path="unused")

    assert "unknown table 'ghosts'" in captured["correction_prompt"]
    assert result.validation_passed is True
    assert trace["delta_merge"]["errors"] == []


@pytest.mark.parametrize("engine", sorted(_ENGINES))
def test_uncorrected_merge_errors_are_recorded(engine, monkeypatch):
    mod, run, contract, source_engine, _ = _ENGINES[engine]
    captured: dict = {}
    bad = {"delta_version": "1.0", "tables": [{"table_name": "ghosts"}]}
    _stub(monkeypatch, mod, _agent_input(source_engine), [bad, bad], captured)

    result, trace = run(collector_path="unused", analysis_path="unused")

    assert result.validation_passed is False
    assert any("ghosts" in f for f in result.validation_failures)
    assert trace["delta_merge"]["errors"]


@pytest.mark.parametrize("engine", sorted(_ENGINES))
def test_zero_tables_logs_warning_and_does_not_call_the_model(engine, monkeypatch, caplog):
    mod, run, _, source_engine, _ = _ENGINES[engine]
    captured: dict = {}
    _stub(monkeypatch, mod, _agent_input(source_engine, tables=[]), [{}], captured)

    with caplog.at_level(logging.WARNING), pytest.raises(ValueError, match="no tables"):
        run(collector_path="x", analysis_path="y")

    assert any("no tables" in r.message.lower() for r in caplog.records)
    assert "prompt" not in captured


@pytest.mark.parametrize("engine", sorted(_ENGINES))
def test_pe_reviewer_reviews_the_delta(engine, monkeypatch):
    mod = _ENGINES[engine][0]
    captured: dict = {}

    class FakePE:
        def __init__(self, **kwargs):
            pass

        def __call__(self, prompt):
            captured["prompt"] = prompt

            class R:
                structured_output = mod.PEReviewResult(verdict="APPROVED")

            return R()

    monkeypatch.setattr(mod, "Agent", FakePE)
    delta = AuroraDesignDeltaContract.model_validate(
        {"delta_version": "1.0", "tables": [{"table_name": "users", "remove_indexes": ["i1"]}]}
    )

    view = {
        "migration_strategy": "carry_over",
        "residual_types": [{"source_data_type": "bigint", "count": 2}],
        "hot_queries": [{"query_id": "hot-q-1", "tables_accessed": ["users"]}],
        "tables": [
            {"table_name": "users", "columns": ["id BIGINT"], "indexes": ["i1 (id)"]},
            {"table_name": "untouched_table", "columns": ["x TEXT"]},
        ],
    }

    review = mod._invoke_pe_reviewer(object(), delta, {"table_count": 3, "design_view": view})

    prompt = captured["prompt"]
    assert review.verdict.value == "APPROVED"
    assert "delta against the" in prompt
    assert '"remove_indexes"' in prompt
    assert "Tables: 3" in prompt
    # I5: the evidence the delta is judged against
    assert "hot-q-1" in prompt
    assert '"i1 (id)"' in prompt  # view entry of the touched table
    assert "untouched_table" not in prompt
    assert '"source_data_type": "bigint"' in prompt


def test_revision_prompt_lists_the_tables_the_delta_touches():
    from src.tools.schema.base_schema_agent import SchemaDesignRunner

    delta = AuroraDesignDeltaContract.model_validate(
        {"delta_version": "1.0", "tables": [{"table_name": "users"}, {"table_name": "orders"}]}
    )
    assert SchemaDesignRunner._get_table_names(delta) == ["users", "orders"]
