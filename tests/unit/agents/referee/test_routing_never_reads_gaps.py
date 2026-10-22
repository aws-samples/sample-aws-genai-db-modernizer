"""Routing never reads the relational gap check (#471, ADR-031, #1330).

Gaps are read at synthesis only: the gap detector (``src.tools.analysis.relational_gaps``)
and its catalog (``src.shared.catalog.gaps``) list what wave 1's Aurora engine lacks, and
must never change which engine a query lands on. This walks the import graph of
``src/`` (static ``import`` and ``from ... import`` statements, relative ones resolved,
imports inside functions included) from every routing module and fails when one of them
reaches the detector, directly or through anything it imports, or when a module it
reaches takes anything from the catalog but its impact labels (the facts layer, which
the reality check's citations use, names the impacts with ``GAP_IMPACTS``).
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

# A guard of behaviour main already has: no routing module reads gaps today (#1330's
# review checked it by hand); this keeps it so.
pytestmark = pytest.mark.passes_on_main

REPO = Path(__file__).resolve().parents[4]
SRC = REPO / "src"

DETECTOR = "src.tools.analysis.relational_gaps"
CATALOG = "src.shared.catalog.gaps"
# What a module routing reaches may take from the catalog: names, never rules.
CATALOG_LABELS = frozenset({"GAP_IMPACTS", "GAP_IMPACT_LABELS", "GapImpact"})

# The modules that decide or change a query's engine: triage, analysis, the first
# assignment, the reality check, the customer gate and the reroutes.
ROUTING_MODULES = (
    "src.agents.referee.triage",
    "src.agents.referee.triage_handler",
    "src.agents.referee.assignment_resolver",
    "src.agents.referee.table_assignments",
    "src.agents.referee.reality_check_handler",
    "src.agents.referee.assignment_overrides",
    "src.agents.referee.engine_scope",
    "src.agents.referee.move_ledger",
    "src.agents.referee.tie_break",
    "src.agents.referee.write_groups",
    "src.agents.referee.query_scoring",
    "src.agents.referee.query_pattern",
)


def _module_name(path: Path) -> str:
    parts = path.relative_to(REPO).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _modules() -> dict[str, Path]:
    return {_module_name(p): p for p in SRC.rglob("*.py") if "node_modules" not in p.parts}


def _imports(name: str, path: Path, known: dict[str, Path]) -> tuple[set[str], set[str]]:
    """The src modules ``name`` imports, and the names it takes from the catalog."""
    package = name if path.name == "__init__.py" else name.rsplit(".", 1)[0]
    out: set[str] = set()
    from_catalog: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(), str(path))):
        if isinstance(node, ast.Import):
            targets = [a.name for a in node.names]
            if any(a.name == CATALOG for a in node.names):
                from_catalog.add("*")
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".")
                anchor = anchor[: len(anchor) - (node.level - 1)]
                base = ".".join([*anchor, base] if base else anchor)
            targets = [base] + [f"{base}.{a.name}" for a in node.names]
            if base == CATALOG:
                from_catalog.update(a.name for a in node.names)
            if base == "src.shared.catalog" and any(a.name == "gaps" for a in node.names):
                from_catalog.add("*")
        else:
            continue
        for target in targets:
            # The module itself, or the nearest package of it that is in src/.
            while target and target not in known:
                target = target.rsplit(".", 1)[0] if "." in target else ""
            if target:
                out.add(target)
    return out, from_catalog


def _reachable(start: str, graph: dict[str, set[str]]) -> dict[str, str | None]:
    """Every module ``start`` reaches, each with the module that imported it."""
    seen: dict[str, str | None] = {start: None}
    todo = [start]
    while todo:
        current = todo.pop()
        for nxt in sorted(graph.get(current, ())):
            if nxt not in seen:
                seen[nxt] = current
                todo.append(nxt)
    return seen


def _graph() -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    known = _modules()
    read = {name: _imports(name, path, known) for name, path in known.items()}
    return {n: r[0] for n, r in read.items()}, {n: r[1] for n, r in read.items()}


GRAPH, FROM_CATALOG = _graph()


def _chain(seen: dict[str, str | None], end: str) -> str:
    chain = [end]
    while seen[chain[-1]] is not None:
        chain.append(str(seen[chain[-1]]))
    return " <- ".join(chain)


@pytest.mark.parametrize("module", ROUTING_MODULES)
def test_no_routing_module_reaches_the_gap_check(module: str) -> None:
    assert module in GRAPH, f"{module} moved: update ROUTING_MODULES"
    seen = _reachable(module, GRAPH)
    assert DETECTOR not in seen, f"{module} imports the gap check: {_chain(seen, DETECTOR)}"
    for reached in sorted(seen):
        taken = FROM_CATALOG.get(reached, set()) - CATALOG_LABELS
        assert not taken, f"{_chain(seen, reached)} takes {sorted(taken)} from the gap catalog"


def test_the_walk_finds_the_known_readers() -> None:
    """The guard is not vacuous: synthesis reaches the detector, and the detector takes
    the rules from the catalog."""
    assert DETECTOR in _reachable("src.agents.referee.synthesis_handler", GRAPH)
    assert CATALOG in GRAPH[DETECTOR]
    assert "gap_rules" in FROM_CATALOG[DETECTOR]
