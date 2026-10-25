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

from src.shared.bedrock_guardrail import guardrail_settings, require_valid_guardrail_config
from src.shared.models import Role, resolve_model_id

logger = logging.getLogger(__name__)


def build_agent_factory():
    """Return an agent_factory function for AgentRuntimeServer."""
    from src.atx_orchestrator.orchestrator import DBModernizationOrchestrator

    require_valid_guardrail_config()

    # Model ID (role ATX_AGENT) — must use a cross-region inference profile.
    # Override via the MODEL_ID env var if needed.
    model_id = resolve_model_id(Role.ATX_AGENT)

    # The SDK builds the conversation agent's model from a model id, and takes
    # the guardrail as an id and a version (#1043). The trace and stream-mode
    # settings have no parameter there, so the SDK's defaults apply to it.
    settings = guardrail_settings()
    guardrail_kwargs = (
        {"guardrail_id": settings.identifier, "guardrail_version": settings.version}
        if settings
        else {}
    )

    def agent_factory(mcp_client, storage_dir=None):
        logger.info("Creating DBModernizationOrchestrator model_id=%s", model_id)
        return DBModernizationOrchestrator(
            **guardrail_kwargs,
            # Wrap MCP client in a list as required by the SDK
            mcp_clients=[mcp_client] if mcp_client is not None else None,
            model_id=model_id,
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
        )

    return agent_factory
