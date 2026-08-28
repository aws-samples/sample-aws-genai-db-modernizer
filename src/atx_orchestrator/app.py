"""Orchestrator agent factory for AWS Transform.

Provides ``build_agent_factory`` for the ``orchestrator`` AGENT_TYPE. The
container entry point is ``atx_entrypoint.py`` (a single image dispatched on
``AGENT_TYPE``); it calls this factory and hands it to
``subagent_base.run_server``, which runs the AgentRuntimeServer — the /ping
health check, the MCP JSON-RPC protocol, and the ``delayed_timeout`` queue for
long-running assessments.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def build_agent_factory():
    """Return an agent_factory function for AgentRuntimeServer."""
    from src.atx_orchestrator.orchestrator import DBModernizationOrchestrator

    # Model ID — must use cross-region inference profile prefix.
    # Override via MODEL_ID env var if needed.
    model_id = os.environ.get(
        "MODEL_ID",
        "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
    )

    def agent_factory(mcp_client, storage_dir=None):
        logger.info("Creating DBModernizationOrchestrator model_id=%s", model_id)
        return DBModernizationOrchestrator(
            # Wrap MCP client in a list as required by the SDK
            mcp_clients=[mcp_client] if mcp_client is not None else None,
            model_id=model_id,
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )

    return agent_factory
