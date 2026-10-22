"""Two guards on the schema design trace contract (#1258).

1. ``routing_flags`` stays declared on the model. The post-schema router reads the PE
   reviewer's flags from it; the contract keeps unknown keys, so if the field were
   renamed or dropped the flags would still load, as an extra, and be silently read
   as none.
2. Every write of a ``design_trace.json`` or ``design_trace_group_<G>.json`` goes
   through ``write_design_trace``. A direct ``write_json`` of a design-trace path
   writes a file with no ``contract_version`` (or an old one) that no model checked.
   The scan covers ``src/``, ``scripts/`` and ``tests/e2e/`` helpers that build jobs.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any, get_args, get_origin

from src.contracts.schema_design_trace import DesignTrace, DesignTraceGroup, DesignTraceRun

REPO = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("src", "scripts", "tests/e2e")
WRITE_METHODS = {"write_json", "write_text", "write_bytes", "dump"}
# The files whose writers must call ``write_design_trace``.
WRITER_FILES = (
    "src/agents/schema_design/handler.py",
    "src/agents/schema_design/group_merger.py",
    "src/agents/schema_design/pe_review_external.py",
    "src/atx_orchestrator/tools.py",
)
# Not scanned for a stray write: the writer itself takes its key as a parameter. The
# local orchestrator's verbatim copy of an engine's files to the next version builds its
# key from a listing, so this scan cannot see it (it is an exception of
# ``test_contract_writes_go_through_model.py``).


def test_routing_flags_is_declared_on_every_shape_of_the_trace() -> None:
    for model in (DesignTraceRun, DesignTraceGroup, DesignTrace):
        field = model.model_fields.get("routing_flags")
        assert field is not None, f"{model.__name__} no longer declares routing_flags"
        args = [a for a in get_args(field.annotation) if a is not type(None)]
        assert args and get_origin(args[0]) is list, "routing_flags is a list of flags"
    merged_groups = DesignTrace.model_fields["groups"].annotation
    item = [a for a in get_args(merged_groups) if a is not type(None)][0]
    assert get_args(item)[0] is DesignTraceRun, "each group's body carries routing_flags too"


def test_routing_flags_is_what_the_router_reads() -> None:
    from src.agents.referee.post_schema_router import routing_flags_from_trace

    flag: dict[str, Any] = {"query_ids": ["q"], "flagged_engine": "dynamodb"}
    assert routing_flags_from_trace(DesignTrace(routing_flags=[flag])) == [flag]
    assert routing_flags_from_trace(DesignTrace(groups=[DesignTraceRun(routing_flags=[flag])])) == [
        flag
    ]


def _names_a_trace_file(value: ast.AST) -> bool:
    """A string literal in ``value`` that names a design-trace file."""
    return any(
        isinstance(n, ast.Constant) and isinstance(n.value, str) and "design_trace" in n.value
        for n in ast.walk(value)
    )


def _design_trace_writes(source: str, path: str) -> list[tuple[str, str, int]]:
    """(file, enclosing function, line) of each write call whose arguments name a design trace."""
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr not in WRITE_METHODS:
            continue
        func: ast.AST | None = parents.get(node)
        while func is not None and not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
            func = parents.get(func)
        scope: ast.AST = func if func is not None else tree
        # Names bound to a design-trace path in the same function (``key = f"...design_trace..."``).
        names = {
            t.id
            for a in ast.walk(scope)
            if isinstance(a, ast.Assign) and _names_a_trace_file(a.value)
            for t in a.targets
            if isinstance(t, ast.Name)
        }
        args = [*node.args, *(k.value for k in node.keywords)]
        literal = any(_names_a_trace_file(a) for a in args)
        named = any(isinstance(n, ast.Name) and n.id in names for a in args for n in ast.walk(a))
        if not literal and not named:
            continue
        found.append((path, func.name if func is not None else "<module>", node.lineno))  # type: ignore[attr-defined]
    return found


def _all_writes() -> list[tuple[str, str, int]]:
    writes: list[tuple[str, str, int]] = []
    for root in SCAN_ROOTS:
        for file in sorted((REPO / root).rglob("*.py")):
            rel = file.relative_to(REPO).as_posix()
            writes += _design_trace_writes(file.read_text(), rel)
    return writes


def test_every_writer_file_calls_write_design_trace() -> None:
    """Not vacuous: the writers this guard protects do call the one writer."""
    missing = [
        f
        for f in WRITER_FILES
        if "write_design_trace(" not in (REPO / f).read_text(encoding="utf-8")
    ]
    assert not missing, missing


def test_no_design_trace_is_written_except_through_write_design_trace() -> None:
    stray = _all_writes()
    assert not stray, (
        "write a design trace with write_design_trace (src/agents/schema_design/"
        "design_trace.py), not directly: " + ", ".join(f"{f}:{ln} ({fn})" for f, fn, ln in stray)
    )


def test_the_scan_catches_a_direct_write() -> None:
    src = (
        "def f(store, base, trace):\n"
        '    store.write_json(f"{base}/design_trace.json", trace)\n'
        '    store.write_json(f"{base}/design_trace_group_{1}.json", trace)\n'
        '    store.write_json(f"{base}/schema_output.json", trace)\n'
    )
    assert [w[2] for w in _design_trace_writes(src, "x.py")] == [2, 3]


def test_the_scan_follows_a_path_held_in_a_name() -> None:
    src = (
        "def f(store, base, trace):\n"
        '    key = f"{base}/design_trace.json"\n'
        "    store.write_json(key, trace)\n"
        "    store.write_json(other, trace)\n"
    )
    assert [w[2] for w in _design_trace_writes(src, "x.py")] == [3]
