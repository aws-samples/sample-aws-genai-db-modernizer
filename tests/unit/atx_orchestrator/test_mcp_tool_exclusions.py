# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The agents do not hand the unused agentic MCP tools to their model (#1736).

Each of those tools calls a transform-agents action that
``pipeline/iam-roles.yaml`` no longer grants, so a model that called one would
get an access error. ``tests/unit/tools/aws/test_iam_templates.py`` pins the
role's actions to what stays reachable.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

from src.atx_orchestrator.app import build_agent_factory
from src.atx_orchestrator.mcp_tools import UNUSED_MCP_TOOLS


def test_the_unused_tools_are_the_six_the_role_dropped():
    assert UNUSED_MCP_TOOLS == {
        "close_hitl_task",
        "list_hitl_tasks",
        "create_worklog",
        "list_worklogs",
        "delete_job_plan_step",
        "retrieve_from_knowledge_base",
    }


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
