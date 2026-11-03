# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Guard test: routing never reads the score dimensions it records (#1471, ADR-031).

The resolver explains the scores it compares into a sink (``AssignmentResolver.resolve``'s
``score_dimensions``), and the access-pattern record keeps them (``score_dimensions`` per
query, ``table_dimensions`` per engine). They are a record for the detailed engineering
report: no routing decision may depend on them. This test scans the AST of every module
under ``src/`` and ``scripts/`` and fails on a read:

1. ``key_read`` -- ``x["score_dimensions"]`` / ``x["table_dimensions"]`` loaded, or
   ``x.get(...)`` / ``x.pop(...)`` of either key.
2. ``attr_read`` -- ``x.score_dimensions`` / ``x.table_dimensions`` loaded.
3. ``reader_call`` -- a call of a reader that returns the recorded dimensions:
   ``dimensions_of_record`` or the record writer's private ``_carried_dimensions``, by
   name, as an attribute, or under any name a ``from ... import ... as`` gives it.
4. ``sink_read`` -- the sink variable ``score_dimensions`` subscripted, iterated or read
   through any method other than ``setdefault`` (filling it) and ``values`` (the resolver
   drops the engine the heterogeneous Aurora choice removed). Comparing it with ``None``
   and passing it on as an argument are not reads.

Allowed to read the keys: the record writer (``query_pattern.py``, which builds and carries
them), the facts layer (``report_facts.py``, the detailed report's one reader), the
reality-check handler, which hands its resolver re-run's sink to the writer, and the context
graph's populator (``src/graph/populators.py``, #306), which copies them onto SCORED edges
through ``dimensions_of_record``; routing never reads the graph.

Not caught statically: a key built at run time, ``getattr`` with a computed name, and
``exec``/``eval``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[4]
_SCANNED_DIRS = (_REPO / "src", _REPO / "scripts")
_KEYS = frozenset({"score_dimensions", "table_dimensions"})
_SINK = "score_dimensions"
_SINK_WRITES = frozenset({"setdefault", "values"})
_READERS = frozenset({"dimensions_of_record", "_carried_dimensions"})
ALLOWED_KEY_READERS = frozenset(
    {
        "src/agents/referee/query_pattern.py",
        "src/shared/report_facts.py",
        "src/agents/referee/reality_check_handler.py",
        "src/graph/populators.py",
    }
)


def _is_key(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Constant) and node.value in _KEYS


def _findings(rel: str, source: str) -> list[tuple[str, str, int]]:
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    out: list[tuple[str, str, int]] = []
    key_reader = rel in ALLOWED_KEY_READERS
    # The names a reader is called by in this module: its own, and any alias.
    readers = set(_READERS) | {
        alias.asname
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
        if alias.name in _READERS and alias.asname
    }
    for node in ast.walk(tree):
        if not key_reader:
            if (
                isinstance(node, ast.Subscript)
                and isinstance(node.ctx, ast.Load)
                and _is_key(node.slice)
            ):
                out.append((rel, "key_read", node.lineno))
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"get", "pop"}
                and node.args
                and _is_key(node.args[0])
            ):
                out.append((rel, "key_read", node.lineno))
            elif (
                isinstance(node, ast.Attribute)
                and isinstance(node.ctx, ast.Load)
                and node.attr in _KEYS
            ):
                out.append((rel, "attr_read", node.lineno))
            elif isinstance(node, ast.Call) and (
                (isinstance(node.func, ast.Name) and node.func.id in readers)
                or (isinstance(node.func, ast.Attribute) and node.func.attr in _READERS)
            ):
                out.append((rel, "reader_call", node.lineno))
        if isinstance(node, ast.Name) and node.id == _SINK and isinstance(node.ctx, ast.Load):
            parent = parents.get(node)
            if isinstance(parent, ast.Subscript) and parent.value is node:
                out.append((rel, "sink_read", node.lineno))
            elif isinstance(parent, ast.Attribute) and parent.attr not in _SINK_WRITES:
                out.append((rel, "sink_read", node.lineno))
            elif isinstance(parent, (ast.For, ast.comprehension)) and parent.iter is node:
                out.append((rel, "sink_read", node.lineno))
    return out


def _modules() -> list[tuple[str, str]]:
    return [
        (str(p.relative_to(_REPO)), p.read_text(encoding="utf-8"))
        for d in _SCANNED_DIRS
        for p in sorted(d.rglob("*.py"))
        if "node_modules" not in p.parts
    ]


# A guard: main has no read of the dimensions (they do not exist there).
@pytest.mark.passes_on_main
def test_no_routing_module_reads_the_score_dimensions() -> None:
    found = [f for rel, src in _modules() for f in _findings(rel, src)]
    assert not found, (
        "Routing reads the recorded score dimensions; they are a record only (#1471): " f"{found}"
    )


# A guard: main has no read of the dimensions (they do not exist there).
@pytest.mark.passes_on_main
def test_the_allowed_readers_exist() -> None:
    assert all((_REPO / rel).is_file() for rel in ALLOWED_KEY_READERS)


# A guard: main has no read of the dimensions (they do not exist there).
@pytest.mark.passes_on_main
def test_a_planted_read_is_caught() -> None:
    planted = (
        "def route(record, row, ctx, score_dimensions):\n"
        "    a = row['score_dimensions']\n"
        "    b = record.get('table_dimensions')\n"
        "    c = ctx.score_dimensions\n"
        "    d = score_dimensions[row['query_id']]\n"
        "    e = score_dimensions.items()\n"
        "    for q in score_dimensions:\n"
        "        pass\n"
        "    return a, b, c, d, e\n"
    )
    shapes = [s for _, s, _ in _findings("src/agents/referee/planted.py", planted)]
    assert shapes == ["key_read", "key_read", "attr_read", "sink_read", "sink_read", "sink_read"]


# A guard of the guard: it checks the scan above, which lives in this test file.
@pytest.mark.passes_on_main
def test_a_planted_reader_call_is_caught() -> None:
    """A routing module that reads a record through ``dimensions_of_record`` (by name,
    attribute or alias) or the private ``_carried_dimensions`` is caught; the graph
    populator, an allowed reader, is not."""
    planted = (
        "from src.agents.referee import query_pattern\n"
        "from src.agents.referee.query_pattern import dimensions_of_record\n"
        "from src.agents.referee.query_pattern import dimensions_of_record as recorded\n"
        "from src.agents.referee.query_pattern import _carried_dimensions as carried\n"
        "def route(record, store):\n"
        "    a = dimensions_of_record(record)\n"
        "    b = query_pattern.dimensions_of_record(record)\n"
        "    c = recorded(record)\n"
        "    d = carried(store, 'db/job', 2)\n"
        "    e = query_pattern._carried_dimensions(store, 'db/job', 2)\n"
        "    return a, b, c, d, e\n"
    )
    shapes = [s for _, s, _ in _findings("src/agents/referee/planted.py", planted)]
    assert shapes == ["reader_call"] * 5
    assert _findings("src/graph/populators.py", planted) == []


# A guard: main has no read of the dimensions (they do not exist there).
@pytest.mark.passes_on_main
def test_filling_and_passing_the_sink_is_not_a_read() -> None:
    allowed = (
        "def resolve(q, e, r, score_dimensions=None):\n"
        "    if score_dimensions is not None:\n"
        "        score_dimensions.setdefault(q, {})[e] = r\n"
        "    for explained in (score_dimensions or {}).values():\n"
        "        explained.pop(e, None)\n"
        "    write(score_dimensions=score_dimensions)\n"
        "    return {'score_dimensions': score_dimensions}\n"
    )
    assert _findings("src/agents/referee/x.py", allowed) == []
