"""Entry point for the AWS Transform orchestrator container.

AgentRuntimeServer handles:
  - /ping health check (Bedrock AgentCore requires this)
  - MCP JSON-RPC 2.0 protocol
  - Queue support for long-running assessments (delayed_timeout=3600)
  - Both Bedrock AgentCore and AWS Transform compute service endpoints
"""

from __future__ import annotations

import argparse
import logging
import os

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
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


def main():
    parser = argparse.ArgumentParser(description="DB Modernization AWS Transform Orchestrator")
    parser.add_argument("--host", default="0.0.0.0", help="Bind host")  # nosec B104
    parser.add_argument("--port", type=int, default=8080, help="Bind port")
    parser.add_argument(
        "--storage-dir",
        default="/tmp/orchestrator_agent",  # nosec B108 — container-local dir created by Dockerfile
        help="Storage directory for queue, responses, and checkpoints",
    )
    parser.add_argument(
        "--binary-location",
        default="/home/amazon/AgentBuilderAgenticMCP/bin/agent-builder-agentic-mcp",
        help="Path to the agentic MCP server binary (set by Dockerfile)",
    )
    args = parser.parse_args()

    from agent_builder_sdk.server.agent_runtime_server import AgentRuntimeServer

    server = AgentRuntimeServer(
        agent_factory=build_agent_factory(),
        host=args.host,
        port=args.port,
        binary_location=args.binary_location,
        storage_dir=args.storage_dir,
        # 3600s window for long-running assessments — avoids 28s asyncio timeout
        delayed_timeout=3600,
    )

    logger.info("Starting DB Modernization orchestrator on %s:%s", args.host, args.port)
    server.start()


if __name__ == "__main__":
    main()
