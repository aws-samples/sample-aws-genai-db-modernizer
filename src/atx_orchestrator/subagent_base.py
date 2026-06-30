"""Shared subagent scaffolding for AWS Transform.

Each existing AGENT_TYPE becomes its own deployable subagent (one agent per
image). They all share the same A2A message handling, status management, and
local-mode fallback — only the work function differs.

CRITICAL SDK patterns (see steering/subagent-patterns.md):
  - The AsyncBaseSubagent subclass MUST be created inside a factory function
    (module-level subclasses hang in production containers).
  - process_message_async extracts the A2A text, runs the work, and MANUALLY
    sets COMPLETED/FAILED via the agent instance manager.
  - Use AgentRuntimeServer with delayed_timeout, never the stateless server.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable

logger = logging.getLogger(__name__)


def extract_text(message) -> str:
    """Extract user text from a ProcessMessageRequest or A2A dict/str."""
    msg = getattr(message, "message", message)
    if isinstance(msg, dict):
        parts = msg.get("parts") or [{}]
        first = parts[0] if parts else {}
        text = first.get("text", json.dumps(msg))
        return str(text)
    return str(msg)


def parse_invocation(text: str) -> dict:
    """Parse job_id / database_name / input_key from invocation text.

    Accepts a JSON object embedded in the text, or 'key: value' / 'key=value' lines.
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return {
                    "job_id": str(data.get("job_id", "")).strip(),
                    "database_name": str(data.get("database_name", "")).strip(),
                    "input_key": str(data.get("input_key", "")).strip(),
                }
        except json.JSONDecodeError:
            pass

    result = {"job_id": "", "database_name": "", "input_key": ""}
    for key in result:
        m = re.search(rf"{key}\s*[:=]\s*([^\s,;]+)", text, re.IGNORECASE)
        if m:
            result[key] = m.group(1).strip().strip("\"'")
    return result


def make_subagent_factory(
    system_prompt: str,
    work_fn: Callable[[dict], dict],
):
    """Build an agent_factory for a subagent whose work is `work_fn`.

    Args:
        system_prompt: The subagent's system prompt.
        work_fn: Callable taking the parsed invocation dict
            ({job_id, database_name, input_key}) and returning a summary dict.
            Raises on failure (the wrapper marks the instance FAILED).

    Returns:
        agent_factory(mcp_client, storage_dir=None) suitable for AgentRuntimeServer.
    """

    def agent_factory(mcp_client, storage_dir=None):
        import os

        from agent_builder_sdk.base_subagent.base_subagent import AsyncBaseSubagent

        class _Subagent(AsyncBaseSubagent):
            async def process_message_async(self, message):  # type: ignore[override]
                text = extract_text(message)
                params = parse_invocation(text)

                # Resolve status manager lazily (None when outside ATX runtime).
                manager = None
                instance_id = None
                try:
                    from agent_builder_sdk.agentic_framework.agent_lifecycle import (
                        get_agent_instance_manager,
                    )

                    manager = get_agent_instance_manager()
                    instance_id = manager.agent_instance_id
                except Exception as exc:  # noqa: BLE001
                    # Expected when running outside the ATX runtime (e.g. local
                    # tests): no agent context env vars. Status updates are skipped.
                    logger.debug("No ATX agent context, running in local mode: %s", exc)

                try:
                    if not params["job_id"] or not params["database_name"]:
                        raise ValueError(
                            "Invocation must include 'job_id' and 'database_name'. "
                            f"Parsed: {params}"
                        )
                    summary = work_fn(params)
                    payload = json.dumps({"response": summary})
                    if manager and instance_id:
                        manager.update_status(instance_id, "COMPLETED", agent_output=payload)
                    logger.info("Subagent COMPLETED: %s", summary)
                    return payload
                except Exception as e:  # noqa: BLE001
                    logger.exception("Subagent FAILED")
                    if manager and instance_id:
                        manager.update_status(instance_id, "FAILED", status_reason=str(e)[:1024])
                    raise

        model_id = os.environ.get("MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
        return _Subagent(
            system_prompt=system_prompt,
            mcp_clients=[mcp_client] if mcp_client is not None else None,
            region_name=os.environ.get("AWS_REGION", "us-east-1"),
            model_id=model_id,
        )

    return agent_factory


def run_server(agent_factory, default_storage_dir: str):
    """Start an AgentRuntimeServer for the given factory (shared CLI entry logic)."""
    import argparse

    parser = argparse.ArgumentParser(description="AWS Transform Subagent")
    parser.add_argument("--host", default="0.0.0.0")  # nosec B104
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--storage-dir", default=default_storage_dir)
    parser.add_argument(
        "--binary-location",
        default="/home/amazon/AgentBuilderAgenticMCP/bin/agent-builder-agentic-mcp",
    )
    args = parser.parse_args()

    from agent_builder_sdk.server.agent_runtime_server import AgentRuntimeServer

    server = AgentRuntimeServer(
        agent_factory=agent_factory,
        host=args.host,
        port=args.port,
        binary_location=args.binary_location,
        storage_dir=args.storage_dir,
        delayed_timeout=3600,
    )
    logger.info("Starting subagent on %s:%s", args.host, args.port)
    server.start()
