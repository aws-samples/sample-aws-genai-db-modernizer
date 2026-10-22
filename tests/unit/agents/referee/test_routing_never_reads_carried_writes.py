"""Routing never reads the carried writes or the write-intent catalog (#1362, ADR-031
Decision 1 Amendment 9).

A write-group member its engine fits poorly is named to the designer and the customer
(``src.agents.referee.carried_writes``, worded by ``src.shared.catalog.write_intents``),
never re-routed. This walks the import graph of ``src/`` from every routing module (the
same walk and module list as the gap guard, ``test_routing_never_reads_gaps``) and fails
when one of them reaches either module, directly or through anything it imports.
"""

from __future__ import annotations

import pytest

from tests.unit.agents.referee.test_routing_never_reads_gaps import (
    GRAPH,
    ROUTING_MODULES,
    _chain,
    _reachable,
)

CARRIED = "src.agents.referee.carried_writes"
CATALOG = "src.shared.catalog.write_intents"


@pytest.mark.passes_on_main  # the modules do not exist on main, so nothing reads them
@pytest.mark.parametrize("module", ROUTING_MODULES)
def test_no_routing_module_reaches_the_carried_writes(module: str) -> None:
    assert module in GRAPH, f"{module} moved: update ROUTING_MODULES"
    seen = _reachable(module, GRAPH)
    for target in (CARRIED, CATALOG):
        assert target not in seen, f"{module} imports {target}: {_chain(seen, target)}"


def test_the_walk_finds_the_known_readers() -> None:
    """The guard is not vacuous: synthesis and the schema-design step reach both."""
    for reader in ("src.agents.referee.synthesis_handler", "src.agents.schema_design.handler"):
        seen = _reachable(reader, GRAPH)
        assert CARRIED in seen and CATALOG in seen, reader
