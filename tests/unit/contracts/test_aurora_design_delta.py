"""The Aurora design delta contract is small, versioned and strict (issue #273)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.contracts.aurora_design_delta import (
    DELTA_VERSION,
    AuroraDesignDeltaContract,
    is_design_delta,
)


def test_minimal_delta_is_just_the_version():
    delta = AuroraDesignDeltaContract.model_validate({"delta_version": "1.0"})
    assert delta.tables == [] and delta.type_rules == [] and delta.trade_offs == []
    assert DELTA_VERSION == "1.0"


@pytest.mark.parametrize("payload", [{}, {"delta_version": "2.0"}, {"delta_version": 1}])
def test_version_is_required_and_pinned(payload):
    with pytest.raises(ValidationError):
        AuroraDesignDeltaContract.model_validate(payload)


@pytest.mark.parametrize(
    "payload",
    [
        {"delta_version": "1.0", "table_definitions": []},  # full-contract field
        {"delta_version": "1.0", "tables": [{"table_name": "t", "add_index": ["x"]}]},
        {"delta_version": "1.0", "type_rules": [{"source_data_type": "int"}]},
    ],
)
def test_unknown_or_incomplete_fields_are_rejected(payload):
    with pytest.raises(ValidationError):
        AuroraDesignDeltaContract.model_validate(payload)


def test_is_design_delta_tells_a_delta_from_a_full_contract():
    assert is_design_delta({"delta_version": "1.0"})
    assert not is_design_delta({"table_definitions": [], "generated_ddl": ""})
    assert not is_design_delta(["delta_version"])
