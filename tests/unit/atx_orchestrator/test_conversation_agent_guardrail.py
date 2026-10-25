"""The AWS Transform conversation agent gets the optional Bedrock guardrail (#1043)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from src.atx_orchestrator.app import build_agent_factory
from src.shared.bedrock_guardrail import GuardrailConfigError

_ENVS = (
    "BEDROCK_GUARDRAIL_ID",
    "BEDROCK_GUARDRAIL_VERSION",
    "BEDROCK_GUARDRAIL_TRACE",
    "BEDROCK_GUARDRAIL_STREAM_MODE",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _ENVS:
        monkeypatch.delenv(name, raising=False)


def _built_kwargs() -> dict[str, Any]:
    with patch("src.atx_orchestrator.orchestrator.DBModernizationOrchestrator") as orchestrator:
        orchestrator.return_value = MagicMock()
        build_agent_factory()(MagicMock())
    return dict(orchestrator.call_args.kwargs)


def test_the_guardrail_is_attached_when_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-abc123")
    monkeypatch.setenv("BEDROCK_GUARDRAIL_VERSION", "3")

    kwargs = _built_kwargs()

    assert kwargs["guardrail_id"] == "gr-abc123"
    assert kwargs["guardrail_version"] == "3"


@pytest.mark.passes_on_main  # the off and invalid cases must keep their behaviour
def test_nothing_is_added_without_a_guardrail() -> None:
    kwargs = _built_kwargs()

    assert "guardrail_id" not in kwargs
    assert "guardrail_version" not in kwargs


@pytest.mark.passes_on_main  # the off and invalid cases must keep their behaviour
def test_a_partial_configuration_stops_the_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BEDROCK_GUARDRAIL_ID", "gr-abc123")

    with pytest.raises(GuardrailConfigError):
        build_agent_factory()
