import src.tools.schema.aurora_postgresql_schema_agent as agent_mod
from src.contracts.aurora_postgresql_model_output import AuroraPostgresqlModelOutputContract


def test_agent_builds_draft_and_passes_strategy(monkeypatch):
    captured = {}

    def fake_run(self, designer_prompt, input_summary):
        captured["prompt"] = designer_prompt
        captured["summary"] = input_summary
        out = AuroraPostgresqlModelOutputContract(
            job_id="job-1",
            source_database="sales",
            migration_strategy="translate",
            table_definitions=[
                {
                    "table_name": "users",
                    "columns": [
                        {
                            "name": "id",
                            "aurora_type": "BIGINT",
                            "source_type": "integer",
                            "script_derived": True,
                            "needs_judgment": False,
                        }
                    ],
                    "primary_key": ["id"],
                }
            ],
            generated_ddl='CREATE TABLE "users" (...);',
            trade_offs=[{"description": "d", "impact": "i"}],
            validation_passed=True,
        )
        return out, {"stubbed": True}

    monkeypatch.setattr(agent_mod.SchemaDesignRunner, "run", fake_run)
    monkeypatch.setattr(
        agent_mod,
        "load_agent_input",
        lambda: {
            "collector": {
                "source_database_engine": "oracle",
                "tables": [
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
                            }
                        ],
                    }
                ],
                "queries": {"query_patterns": []},
            },
            "analysis": {},
            "context": {},
            "decision_trace": {},
        },
    )
    monkeypatch.setattr(agent_mod, "_build_model", lambda: object())
    monkeypatch.setattr(agent_mod, "Agent", lambda **kwargs: object())

    result, trace = agent_mod.run_aurora_postgresql_schema_agent(
        collector_path="unused",
        analysis_path="unused",
    )
    assert isinstance(result, AuroraPostgresqlModelOutputContract)
    assert trace == {"stubbed": True}
    assert "translate" in captured["prompt"]
    assert "draft" in captured["prompt"].lower()
    # The deterministic draft must reflect the built DDL for the users table.
    assert "users" in captured["prompt"]
    assert "BIGINT" in captured["prompt"]  # id column resolved deterministically
