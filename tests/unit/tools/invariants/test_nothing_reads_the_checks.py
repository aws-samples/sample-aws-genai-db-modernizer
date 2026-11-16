# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The invariant checks only check: nothing in the product reads them (#2143).

``src/tools/invariants/`` runs each ADR invariant as a query over a job's saved
artifacts; Python stays the decision maker. If a pipeline module imported the checks, a
query could start deciding what it is meant to verify. This walks ``src/``'s import
graph (the walk the routing guards use, ``test_routing_never_reads_gaps``) and fails when
any module outside the package imports it, and reads every script's imports: only
``scripts/check_invariants.py`` may.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from tests.unit.agents.referee.test_routing_never_reads_gaps import GRAPH

REPO = Path(__file__).resolve().parents[4]
PACKAGE = "src.tools.invariants"
SCRIPT = "scripts/check_invariants.py"


def _inside(module: str) -> bool:
    return module == PACKAGE or module.startswith(PACKAGE + ".")


@pytest.mark.passes_on_main  # the package does not exist on main, so nothing imports it
def test_no_product_module_imports_the_invariant_checks() -> None:
    readers = sorted(
        m for m, deps in GRAPH.items() if not _inside(m) and any(_inside(d) for d in deps)
    )
    assert readers == [], f"{readers} import {PACKAGE}: the checks only check, never decide"


def _imported(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def test_only_the_check_script_imports_them() -> None:
    readers = sorted(
        p.relative_to(REPO).as_posix()
        for p in (REPO / "scripts").rglob("*.py")
        if any(_inside(name) for name in _imported(p))
    )
    assert readers == [SCRIPT]


def test_the_walk_sees_the_package() -> None:
    """The guard is not vacuous: the graph holds the package's modules and their imports."""
    assert f"{PACKAGE}.load" in GRAPH and f"{PACKAGE}.checks" in GRAPH
    assert "src.shared.contract_versioning" in GRAPH[f"{PACKAGE}.load"]
