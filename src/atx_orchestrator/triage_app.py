"""Entry point for the Triage subagent container."""

from __future__ import annotations

import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


def main():
    from src.atx_orchestrator.subagent_base import run_server
    from src.atx_orchestrator.triage_subagent import agent_factory

    storage_dir = "/tmp/triage_agent"  # nosec B108 — container-local dir created by Dockerfile
    run_server(agent_factory, default_storage_dir=storage_dir)


if __name__ == "__main__":
    main()
