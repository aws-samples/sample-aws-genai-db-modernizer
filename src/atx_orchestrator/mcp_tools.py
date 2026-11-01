# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Agentic MCP tools the agents keep away from their model (#1736).

The image starts the AWS Transform agentic MCP server in the container, and the
SDK hands every tool it registers to the agent's model unless the agent
excludes it. Each tool calls transform-agents APIs under the
``AgentCoreExecutionRole`` credentials, so every tool left in costs its actions
in ``pipeline/iam-roles.yaml``.

``delete_job_plan_step`` removes a step from the job's progress panel. The
plan is declared by code, the prompt tells the model it cannot change the plan,
and the SDK already hides this tool from subagents, so the orchestrator
excludes it too and the role does not grant ``DeleteJobPlanStep``.
``tests/unit/tools/aws/test_iam_templates.py`` pins the role to the tools left
in: removing a name here needs its actions added back.
"""

from __future__ import annotations

UNUSED_MCP_TOOLS: frozenset[str] = frozenset(
    {
        "delete_job_plan_step",  # transform-agents:DeleteJobPlanStep
    }
)
