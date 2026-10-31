# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The agents do not hand the unused agentic MCP tools to their model (#1736).

Each of those tools calls a transform-agents action that
``pipeline/iam-roles.yaml`` does not grant, so a model that called one would
get an access error. ``tests/unit/tools/aws/test_iam_templates.py`` pins the
role's actions to what stays reachable.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from src.atx_orchestrator.app import build_agent_factory
from src.atx_orchestrator.mcp_tools import UNUSED_MCP_TOOLS


def test_only_delete_job_plan_step_is_excluded():
    # The Worklogs, HITL close/list and knowledge-base tools stay: they back
    # parts of AWS Transform the customer sees (#1736 review).
    assert UNUSED_MCP_TOOLS == {"delete_job_plan_step"}


def test_the_orchestrator_excludes_the_unused_tools():
    with patch("src.atx_orchestrator.orchestrator.DBModernizationOrchestrator") as orchestrator:
        orchestrator.return_value = MagicMock()
        build_agent_factory()(MagicMock())
    kwargs = orchestrator.call_args.kwargs

    assert kwargs["excluded_mcp_tool_names"] == set(UNUSED_MCP_TOOLS)


def test_a_subagent_excludes_the_unused_tools():
    from agent_builder_sdk.base_subagent.base_subagent import AsyncBaseSubagent

    from src.atx_orchestrator.subagents.base import make_subagent_factory

    seen: dict[str, Any] = {}

    def _init(self, **kwargs):
        seen.update(kwargs)

    with patch.object(AsyncBaseSubagent, "__init__", _init):
        make_subagent_factory("prompt", lambda params: {})(MagicMock())

    assert seen["excluded_mcp_tool_names"] == set(UNUSED_MCP_TOOLS)
    # The SDK's default subagent exclusions still apply (excluded_tool_names unset).
    assert "excluded_tool_names" not in seen
