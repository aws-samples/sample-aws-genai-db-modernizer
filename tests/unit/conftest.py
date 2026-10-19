"""Fixtures shared by the unit tests."""

import pytest

from tests.unit.agents.referee.fixed_scores import table_confidence_scores

__all__ = ["schema_contract_off", "table_confidence_scores"]


@pytest.fixture
def schema_contract_off(monkeypatch):
    """Turn off the write gate on ``schema_output.json`` (#1186), and nothing else.

    Finalize's own contract check and ``--check-costs``'s ``contract_errors``
    stay on. For tests whose deliberately minimal designs exercise something
    else (scope, revisions, orchestration); the gate itself is tested in
    tests/unit/agents/schema_design/test_output_contract_gate.py.
    """
    from src.agents.schema_design import output_contract

    monkeypatch.setattr(output_contract, "_gate_issues", lambda *_args, **_kwargs: [])
