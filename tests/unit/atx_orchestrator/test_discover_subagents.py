"""Unit tests for subagent discovery integration.

Verifies that ``discover_subagents`` (from the SDK's ``SubagentRegistryTools``)
is instantiated and registered in ``PIPELINE_TOOLS``. As of SDK v1.0.2 this
method is a mock that returns hardcoded sample data — see
``agent_builder_sdk/orchestrator_strands/tools/subagent_registry_tools.py``.
When the SDK replaces the mock with a real registry API call, this wiring
will light up automatically without changes on our side.
"""

from __future__ import annotations

import pytest

from src.atx_orchestrator.orchestrator import PIPELINE_TOOLS, discover_subagents


class TestDiscoverSubagentsRegistration:
    def test_discover_subagents_in_pipeline_tools(self) -> None:
        tool_names = [getattr(t, "tool_name", getattr(t, "__name__", "")) for t in PIPELINE_TOOLS]
        assert "discover_subagents" in tool_names

    def test_pipeline_tools_count_is_13(self) -> None:
        """10 in-process + 2 A2A + 1 discovery = 13 tools total."""
        assert len(PIPELINE_TOOLS) == 13


class TestDiscoverSubagentsCallable:
    """Async invocation via the SDK's mock — proves the wiring reaches the tool."""

    @pytest.mark.asyncio
    async def test_returns_list_of_agent_versions(self) -> None:
        # discover_subagents is a bound method decorated with @tool.
        # DecoratedFunctionTool exposes the underlying async callable.
        result = await discover_subagents()
        assert isinstance(result, list)
        assert len(result) >= 1

    @pytest.mark.asyncio
    async def test_mock_shape_has_expected_fields(self) -> None:
        """SDK mock returns GetAgentVersionOutput with version+metadata+configuration."""
        result = await discover_subagents()
        first = result[0]
        # Access via attribute (Pydantic model, not dict)
        assert getattr(first, "version", None) is not None
        assert getattr(first, "metadata", None) is not None
        assert getattr(first, "configuration", None) is not None
