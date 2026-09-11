import pytest
from pydantic import ValidationError

from src.contracts.aurora_postgresql_model_output import AuroraPostgresqlModelOutputContract


def _minimal_payload():
    return {
        "job_id": "job-1",
        "source_database": "sales",
        "migration_strategy": "translate",
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
        "generated_ddl": 'CREATE TABLE "users" (...);',
        "trade_offs": [
            {
                "description": "Use IDENTITY over sequences",
                "impact": "Simpler, PostgreSQL-native auto-increment; no sequence objects to manage.",
            }
        ],
        "validation_passed": True,
    }


def test_minimal_valid_contract():
    contract = AuroraPostgresqlModelOutputContract.model_validate(_minimal_payload())
    assert contract.target_engine == "aurora_postgresql"
    assert contract.migration_strategy == "translate"
    assert contract.table_definitions[0].columns[0].aurora_type == "BIGINT"


def test_requires_at_least_one_table():
    payload = _minimal_payload()
    payload["table_definitions"] = []
    with pytest.raises(ValidationError):
        AuroraPostgresqlModelOutputContract.model_validate(payload)


def test_invalid_migration_strategy_rejected():
    payload = _minimal_payload()
    payload["migration_strategy"] = "rewrite"
    with pytest.raises(ValidationError):
        AuroraPostgresqlModelOutputContract.model_validate(payload)


def test_requires_trade_offs():
    payload = _minimal_payload()
    payload["trade_offs"] = []
    with pytest.raises(ValidationError):
        AuroraPostgresqlModelOutputContract.model_validate(payload)


def test_round_trip_json():
    contract = AuroraPostgresqlModelOutputContract.model_validate(_minimal_payload())
    dumped = contract.model_dump_json()
    reparsed = AuroraPostgresqlModelOutputContract.model_validate_json(dumped)
    assert reparsed == contract
