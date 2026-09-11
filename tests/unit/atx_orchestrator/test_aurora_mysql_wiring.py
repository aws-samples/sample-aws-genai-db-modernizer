"""Assert the four wiring points that register Aurora MySQL as a schema designer."""

from src.atx_orchestrator.core import IMPLEMENTED_SCHEMA_DESIGNERS, _design_shape


def test_aurora_mysql_is_implemented_designer():
    assert "aurora_mysql" in IMPLEMENTED_SCHEMA_DESIGNERS


def test_aurora_mysql_design_shape():
    field, unit = _design_shape("aurora_mysql")
    assert field == "table_definitions"
    assert unit == "target tables"


def test_engine_contract_registered():
    from src.agents.schema_design.handler import validate_schema_design_output

    payload = {
        "job_id": "j",
        "source_database": "db",
        "migration_strategy": "carry_over",
        "table_definitions": [
            {
                "table_name": "t",
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
        "generated_ddl": "CREATE TABLE `t` ...",
        "trade_offs": [{"description": "d", "impact": "i"}],
        "validation_passed": True,
    }
    assert validate_schema_design_output(payload, "aurora_mysql") == {"valid": True}
