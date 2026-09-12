import pytest
from pydantic import ValidationError

from src.contracts.aurora_mysql_model_output import AuroraMySQLModelOutputContract


def _minimal_payload():
    return {
        "job_id": "job-1",
        "source_database": "sales",
        "migration_strategy": "carry_over",
        "table_definitions": [
            {
                "table_name": "users",
                "primary_key": ["id"],
                "columns": [
                    {
                        "name": "id",
                        "aurora_type": "BIGINT",
                        "source_type": "integer",
                        "script_derived": True,
                        "needs_judgment": False,
                    }
                ],
            }
        ],
        "generated_ddl": "CREATE TABLE `users` (...);",
        "trade_offs": [
            {
                "description": "Keep AUTO_INCREMENT",
                "impact": "Native MySQL identity; no app changes.",
            }
        ],
        "validation_passed": True,
    }


def test_minimal_valid_contract():
    c = AuroraMySQLModelOutputContract.model_validate(_minimal_payload())
    assert c.target_engine == "aurora_mysql"
    assert c.migration_strategy == "carry_over"
    assert c.table_definitions[0].columns[0].aurora_type == "BIGINT"


def test_requires_at_least_one_table():
    p = _minimal_payload()
    p["table_definitions"] = []
    with pytest.raises(ValidationError):
        AuroraMySQLModelOutputContract.model_validate(p)


def test_invalid_migration_strategy_rejected():
    p = _minimal_payload()
    p["migration_strategy"] = "rewrite"
    with pytest.raises(ValidationError):
        AuroraMySQLModelOutputContract.model_validate(p)


def test_requires_trade_offs():
    p = _minimal_payload()
    p["trade_offs"] = []
    with pytest.raises(ValidationError):
        AuroraMySQLModelOutputContract.model_validate(p)


def test_round_trip_json():
    c = AuroraMySQLModelOutputContract.model_validate(_minimal_payload())
    assert AuroraMySQLModelOutputContract.model_validate_json(c.model_dump_json()) == c
