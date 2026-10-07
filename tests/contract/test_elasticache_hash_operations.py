"""Hash existence checks must survive the schema-design output boundary."""

import pytest
from pydantic import ValidationError

from src.contracts.elasticache_model_output import AccessPattern


@pytest.mark.parametrize("operation", ["HEXISTS", "HGET", "HSET"])
def test_hash_operation_round_trip(operation: str) -> None:
    pattern = AccessPattern.model_validate(
        {
            "pattern_id": "EC-AP-1",
            "description": "Check whether a metadata field exists",
            "operation": operation,
            "key_pattern": "post:{id}:meta",
            "command_example": f"{operation} post:42:meta published",
            "source_query_ids": ["q1"],
        }
    )
    assert AccessPattern.model_validate_json(pattern.model_dump_json()).operation == operation


def test_unknown_hash_operation_is_rejected() -> None:
    with pytest.raises(ValidationError, match="operation"):
        AccessPattern.model_validate(
            {
                "pattern_id": "EC-AP-1",
                "description": "Invalid operation",
                "operation": "HEXIST",
                "key_pattern": "post:{id}:meta",
                "command_example": "HEXIST post:42:meta published",
                "source_query_ids": ["q1"],
            }
        )
