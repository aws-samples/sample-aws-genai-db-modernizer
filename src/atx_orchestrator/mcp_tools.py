# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Agentic MCP tools the agents keep away from their model (#1736).

The image starts the AWS Transform agentic MCP server in the container, and the
SDK hands every tool it registers to the agent's model unless the agent
excludes it. Each tool calls one transform-agents API under the
``AgentCoreExecutionRole`` credentials, so every tool left in costs an action
in ``pipeline/iam-roles.yaml``.

No code here calls the APIs behind these tools and no prompt asks the model to,
so the orchestrator and the subagents exclude them and the role does not grant
their actions. ``tests/unit/tools/aws/test_iam_templates.py`` pins the role to
what stays reachable: removing a name here needs its action added back.
"""

from __future__ import annotations

UNUSED_MCP_TOOLS: frozenset[str] = frozenset(
    {
        "close_hitl_task",  # transform-agents:CloseHitlTask
        "list_hitl_tasks",  # transform-agents:ListHitlTasks
        "create_worklog",  # transform-agents:CreateWorklog
        "list_worklogs",  # transform-agents:ListWorklogs
        "delete_job_plan_step",  # transform-agents:DeleteJobPlanStep
        "retrieve_from_knowledge_base",  # transform-agents:RetrieveFromKnowledgeBase
    }
)
