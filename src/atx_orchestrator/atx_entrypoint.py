"""Single entrypoint for every AWS Transform agent in this image (modernizer-atx-v2).

Sibling to core-modernizer's ``src/agents/entrypoint.py``, which dispatches
run-to-completion batch agents on ``AGENT_TYPE``. This module does the same for
long-running A2A/AgentCore servers: read ``AGENT_TYPE``, resolve the matching
agent factory, and hand it to :func:`subagent_base.run_server`.

This is the single entry point for every agent. It replaced ten near-identical
per-agent entrypoints (nine ``*_app.py`` files plus ``app.py::main``), which have
been removed. ``app.py`` remains only for ``build_agent_factory`` — the
orchestrator factory this module calls.

Why one entrypoint is safe
--------------------------
The old ``app.py::main()`` and the nine per-agent ``*_app.py`` mains were the
same function, duplicated: identical argparse defaults (host ``0.0.0.0``, port
8080, the agentic MCP binary path), identical ``AgentRuntimeServer``
construction, identical ``delayed_timeout=3600``, identical ``server.start()``.
They varied in exactly two things — which factory, and which ``/tmp`` directory —
both of which this module now derives from ``AGENT_TYPE``.

Dispatch style
--------------
Imports are literal, inside an ``if`` chain, mirroring core-modernizer's
``entrypoint.py``. A dict of module-name strings plus ``importlib`` would be more
compact, but literal imports keep the dependency graph visible to static
analysis, IDEs and the repo's security tooling, and they match the house style of
the module this one is a sibling to.

Contract
--------
``AGENT_TYPE`` is **required** and has no default. An unset or unrecognised
value logs the valid set and exits non-zero rather than falling back to
something plausible — a silent default here would mean a runtime happily
serving the wrong agent.

``AGENT_TYPE`` values are ATX's own vocabulary. They are **not** identical to
core-modernizer's: for the analysis phases core-modernizer uses bare engine
names (``ANALYSIS_AGENTS = {dynamodb, documentdb, elasticache, opensearch,
neptune, keyspaces, aurora, aurora_postgresql, aurora_mysql}``) and builds the
artifact prefix as ``analysis-{agent_type}``. The values below instead match the
**artifact key prefix** an operator sees in S3 (``analysis-dynamodb``), which is
unambiguous and does not collide with the non-analysis phase names. Two mapping
differences to remember if these are ever passed through to
``run_analysis(job_id, db, agent_type, store)``:

* strip the ``analysis-`` prefix — passing it verbatim yields
  ``analysis-analysis-dynamodb/`` as the key prefix;
* ``analysis-aurora-pg`` / ``analysis-aurora-mysql`` correspond to
  core-modernizer's ``aurora_postgresql`` / ``aurora_mysql``.

``orchestrator`` is ATX-only and has no core-modernizer counterpart. The
non-analysis phase names (``collector``, ``referee-triage``,
``assignment-resolver``, ``referee-synthesis``, and the four not yet
implemented) do match core-modernizer exactly.
"""

from __future__ import annotations

import logging
import os
import sys
from typing import NoReturn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger(__name__)


# AGENT_TYPE -> container-local storage directory. This is also the authoritative
# list of valid agent types, used for validation and for the fail-loud message.
# Every directory here must be created and chowned in Dockerfile.atx; that pairing
# has no compile-time guard, so re-check it whenever a row is added.
_AGENTS: dict[str, str] = {
    # ATX-only: the chat-facing orchestrator. No core-modernizer counterpart.
    "orchestrator": "/tmp/orchestrator_agent",  # nosec B108
    # Pipeline phases. Names match the artifact key prefix in S3, not
    # core-modernizer's AGENT_TYPE values — see the module docstring for the two
    # mapping differences.
    "collector": "/tmp/collector_agent",  # nosec B108
    "referee-triage": "/tmp/triage_agent",  # nosec B108
    # One consolidated analysis agent runs every triage-selected engine in-process
    # (see ADR-024); it replaced six per-engine analysis-<engine> agents.
    "analysis": "/tmp/analysis_agent",  # nosec B108
    "assignment-resolver": "/tmp/assignment_agent",  # nosec B108
    "referee-synthesis": "/tmp/synthesis_agent",  # nosec B108
    # Schema design, one per target engine. Upstream exposes a single
    # run_schema_design parameterised by target_type, so these share one module
    # (schema_subagent.make_schema_agent_factory) rather than having six.
    "schema-dynamodb": "/tmp/schema_dynamodb_agent",  # nosec B108
    "schema-documentdb": "/tmp/schema_documentdb_agent",  # nosec B108
    "schema-elasticache": "/tmp/schema_elasticache_agent",  # nosec B108
    "schema-opensearch": "/tmp/schema_opensearch_agent",  # nosec B108
    "schema-aurora-pg": "/tmp/schema_aurora_pg_agent",  # nosec B108
    "schema-aurora-mysql": "/tmp/schema_aurora_mysql_agent",  # nosec B108
    # Not yet implemented as ATX subagents. Adding one means a row here, a branch
    # in _resolve_factory, a subagent module (SYSTEM_PROMPT + _work +
    # make_subagent_factory), and a directory in Dockerfile.atx:
    #   reality-check, schema-split, schema-merge
    # All three accept an assignment_version, and their core-modernizer defaults
    # disagree with each other (synthesis 0, reality-check 1). The orchestrator
    # must pass the version explicitly to every one — see the v2 plan, Dive 1.
}


def _fail(message: str) -> NoReturn:
    """Log a configuration error with the valid agent set, then exit non-zero."""
    logger.error("%s", message)
    logger.error("Valid AGENT_TYPE values: %s", ", ".join(sorted(_AGENTS)))
    sys.exit(1)


def _resolve_factory(agent_type: str):
    """Return the agent factory for ``agent_type``.

    One literal import per branch. The orchestrator exposes
    ``build_agent_factory()``, which *returns* the factory; every subagent
    exposes a module-level ``agent_factory`` directly.
    """
    if agent_type == "orchestrator":
        from src.atx_orchestrator.app import build_agent_factory

        return build_agent_factory()
    if agent_type == "collector":
        from src.atx_orchestrator.subagents.collector import agent_factory

        return agent_factory
    if agent_type == "referee-triage":
        from src.atx_orchestrator.subagents.triage import agent_factory

        return agent_factory
    if agent_type == "analysis":
        # One consolidated agent runs every triage-selected engine in-process
        # (ADR-024), replacing the six per-engine analysis-<engine> branches.
        from src.atx_orchestrator.subagents.analysis import agent_factory

        return agent_factory
    if agent_type == "assignment-resolver":
        from src.atx_orchestrator.subagents.assignment import agent_factory

        return agent_factory
    if agent_type == "referee-synthesis":
        from src.atx_orchestrator.subagents.synthesis import agent_factory

        return agent_factory
    # One branch for all six schema-design targets: upstream's run_schema_design
    # is parameterised by target_type, so the factory is built per engine from a
    # single module rather than imported from six near-identical ones. Routed on
    # the prefix to keep the subagent import lazy like the branches above.
    if agent_type.startswith("schema-"):
        from src.atx_orchestrator.subagents.schema import SCHEMA_TARGETS, make_schema_agent_factory

        if agent_type not in SCHEMA_TARGETS:
            # schema-split and schema-merge are separate phases, not design
            # targets. Fail loudly rather than dispatching one as the other.
            _fail(
                f"AGENT_TYPE={agent_type!r} starts with 'schema-' but is not a design "
                f"target. Known targets: {sorted(SCHEMA_TARGETS)}"
            )
        return make_schema_agent_factory(SCHEMA_TARGETS[agent_type])

    # Unreachable: main() validates against _AGENTS before calling. Guards against
    # a row being added to the table without a matching branch here.
    _fail(f"AGENT_TYPE={agent_type!r} is in the table but has no dispatch branch.")


def main() -> None:
    agent_type = os.environ.get("AGENT_TYPE", "").strip()

    if not agent_type:
        _fail("AGENT_TYPE is not set. It is required and has no default.")
    if agent_type not in _AGENTS:
        _fail(f"AGENT_TYPE={agent_type!r} is not recognised.")

    storage_dir = _AGENTS[agent_type]
    logger.info("Resolving agent: AGENT_TYPE=%s storage_dir=%s", agent_type, storage_dir)

    factory = _resolve_factory(agent_type)

    from src.atx_orchestrator.subagents.base import run_server

    logger.info("Starting agent: %s", agent_type)
    run_server(factory, default_storage_dir=storage_dir)


if __name__ == "__main__":
    main()
