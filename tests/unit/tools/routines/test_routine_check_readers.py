# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Who reads the routine check catalog (#1935, ADR-031 Decision 8).

The routine check rules (``src/catalog/routine_checks/*.toml``) load through
``src/shared/catalog/routine_checks.py``, and the routine rewrite rules
(``src/catalog/routine_rewrites/*.toml``, #1937) through
``src/shared/catalog/routine_rewrites.py``. They are read only by the routine checkers and
rewriters in ``src/tools/routines/`` and by the rule-catalog page's renderer. No routing module and no
synthesis module reaches the loader or a checker through the import graph (the same walk
as the gap guard, ``test_routing_never_reads_gaps.py``).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.unit.agents.referee.test_routing_never_reads_gaps import (
    GRAPH,
    ROUTING_MODULES,
    _chain,
    _reachable,
)

REPO = Path(__file__).resolve().parents[4]
LOADER = "src.shared.catalog.routine_checks"
REWRITE_LOADER = "src.shared.catalog.routine_rewrites"
LOADERS = frozenset({LOADER, REWRITE_LOADER})
CHECKERS = "src.tools.routines"
SYNTHESIS_MODULES = (
    "src.agents.referee.synthesis_handler",
    "src.agents.referee.synthesis_data",
    "src.agents.referee.synthesis_report",
    "src.agents.referee.synthesis_prose",
    "src.agents.referee.synthesis_grounding",
    "src.report.facts",
)
# The modules that may import the loader or a checker, besides the checkers themselves.
# scripts.run_assessment and scripts.run_post_schema_routing run the routine-migration
# phase (#1934); neither is a routing or synthesis module, which the walk below keeps
# away from the checks.
READERS = LOADERS | {
    "scripts.render_catalog",
    "scripts.run_assessment",
    "scripts.run_post_schema_routing",
}


def _reads_routine_checks(path: Path) -> bool:
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module] + [f"{node.module}.{a.name}" for a in node.names]
        else:
            continue
        if any(n in LOADERS or n == CHECKERS or n.startswith(CHECKERS + ".") for n in names):
            return True
    return False


def test_only_the_routine_checkers_and_the_page_renderer_read_the_routine_catalog() -> None:
    readers = set()
    for root in ("src", "scripts"):
        for path in (REPO / root).rglob("*.py"):
            if "node_modules" in path.parts or not _reads_routine_checks(path):
                continue
            readers.add(".".join(path.relative_to(REPO).with_suffix("").parts))
    strays = {r for r in readers if r not in READERS and not r.startswith(CHECKERS + ".")}
    assert not strays, f"modules outside src/tools/routines/ read the routine checks: {strays}"
    assert "src.tools.routines.postgresql_checks" in readers
    assert "src.tools.routines.mysql_checks" in readers
    assert "src.tools.routines.rewrites" in readers
    assert "src.tools.routines.dependencies" in readers


# A guard of behaviour main already has: nothing reaches the checkers there.
@pytest.mark.passes_on_main
def test_no_routing_or_synthesis_module_reaches_the_routine_checks() -> None:
    found = []
    for start in (*ROUTING_MODULES, *SYNTHESIS_MODULES):
        assert start in GRAPH, start
        seen = _reachable(start, GRAPH)
        for module in seen:
            if module in LOADERS or module == CHECKERS or module.startswith(CHECKERS + "."):
                found.append(_chain(seen, module))
    assert not found, "routing or synthesis reaches the routine checks:\n" + "\n".join(found)
