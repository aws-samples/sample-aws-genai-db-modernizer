"""Unit tests for subagent discovery wiring.

Note (Y-3): ``discover_subagents`` is INTENTIONALLY not in ``PIPELINE_TOOLS``.
As of SDK v1.0.2 this method is a hardcoded mock returning a
"dynamic-showcase-subagent" (weather agent), which caused the LLM to
mis-conclude our real subagents weren't deployed when unrelated A2A errors
occurred. The function stays imported for future re-enablement when the SDK
ships a real registry-backed implementation.

See:
- ``agent_builder_sdk/orchestrator_strands/tools/subagent_registry_tools.py``
  (SDK source with the mock)
- ATX_POC_STATE.md F8 for the F8 fix context
"""

from __future__ import annotations

import pytest

from src.atx_orchestrator.orchestrator import PIPELINE_TOOLS, discover_subagents


class TestDiscoverSubagentsRegistration:
    def test_discover_subagents_NOT_in_pipeline_tools(self) -> None:
        """Y-3 fix: intentionally unregistered because the SDK mock misleads the LLM."""
        tool_names = [getattr(t, "tool_name", getattr(t, "__name__", "")) for t in PIPELINE_TOOLS]
        assert "discover_subagents" not in tool_names

    def test_pipeline_tools_count_is_10(self) -> None:
        """Post-A8 tool count: 3 A2A (collect + triage + analysis-dynamodb) +
        5 pipeline + 2 status = 10 (discover_subagents excluded — SDK mock)."""
        assert len(PIPELINE_TOOLS) == 10

    def test_discover_subagents_still_importable(self) -> None:
        """The function itself remains available for future re-enablement."""
        assert discover_subagents is not None


class TestDiscoverSubagentsCallable:
    """Async invocation via the SDK's mock — proves the wiring is intact (even
    though we've unregistered it from PIPELINE_TOOLS to protect the LLM from
    the mock's misleading output).
    """

    @pytest.mark.asyncio
    async def test_returns_list_of_agent_versions(self) -> None:
        result = await discover_subagents()
        assert isinstance(result, list)
        assert len(result) >= 1

    @pytest.mark.asyncio
    async def test_mock_shape_has_expected_fields(self) -> None:
        """SDK mock returns GetAgentVersionOutput with version+metadata+configuration."""
        result = await discover_subagents()
        first = result[0]
        assert getattr(first, "version", None) is not None
        assert getattr(first, "metadata", None) is not None
        assert getattr(first, "configuration", None) is not None
