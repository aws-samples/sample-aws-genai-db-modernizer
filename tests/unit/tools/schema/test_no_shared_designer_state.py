# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The schema designer modules hold no mutable path state (#1054).

Group designs run in parallel threads, so anything a designer reads its input
through must belong to its own call. A module-level path variable, a
``global`` rebinding or an environment variable the pipeline sets would be
shared by every thread, and a group's designer would read another group's
input. This keeps the paths an argument all the way down: every designer
module and the schema design handler are checked by their source.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[4]
AGENTS = sorted((REPO / "src/tools/schema").glob("*_schema_agent.py"))
HANDLERS = [
    REPO / "src/agents/schema_design/handler.py",
    REPO / "src/agents/schema_design/revision_handler.py",
]


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _targets(node: ast.stmt) -> list[str]:
    if isinstance(node, ast.Assign):
        return [t.id for t in node.targets if isinstance(t, ast.Name)]
    if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return [node.target.id]
    return []


def _environ_writes(tree: ast.Module) -> list[int]:
    """Lines that set or remove an environment variable."""
    lines = []
    for node in ast.walk(tree):
        # os.environ[...] = ... / del os.environ[...]
        if isinstance(node, ast.Subscript) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if ast.unparse(node.value) == "os.environ":
                lines.append(node.lineno)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            owner = ast.unparse(node.func.value)
            if owner == "os.environ" and node.func.attr in {
                "pop",
                "update",
                "setdefault",
                "clear",
                "popitem",
            }:
                lines.append(node.lineno)
            if owner == "os" and node.func.attr in {"putenv", "unsetenv"}:
                lines.append(node.lineno)
    return lines


def test_every_designer_module_is_checked() -> None:
    names = {p.name for p in AGENTS}
    assert {
        "dynamodb_schema_agent.py",
        "documentdb_schema_agent.py",
        "opensearch_schema_agent.py",
        "elasticache_schema_agent.py",
        "aurora_mysql_schema_agent.py",
        "aurora_postgresql_schema_agent.py",
    } <= names


_EMPTY_CALLS = {"dict", "list", "set"}
_VALUE_BASES = {"BaseModel", "Enum", "str", "StrEnum", "IntEnum"}


def _is_slot(value: ast.expr | None) -> bool:
    """A value that only holds a place to be filled later: ``None``, ``{}``,
    ``[]``, ``set()``, ``dict()``, ``list()`` (or no value at all)."""
    if value is None:
        return True
    if isinstance(value, ast.Constant) and value.value is None:
        return True
    if isinstance(value, (ast.Dict, ast.List, ast.Set)) and not getattr(
        value, "keys", getattr(value, "elts", None)
    ):
        return True
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in _EMPTY_CALLS
        and not value.args
        and not value.keywords
    )


def _mutable_slots(tree: ast.Module) -> list[str]:
    """Module-level names, and class attributes, bound to an empty slot, plus
    any module-level path variable that is not an UPPER_CASE constant."""
    bad = []
    # A pydantic model's, an enum's or a dataclass's fields are declarations,
    # copied per instance; any other class's attributes are shared by every call.
    scopes = [tree.body] + [
        n.body
        for n in tree.body
        if isinstance(n, ast.ClassDef)
        and not any(ast.unparse(b) in _VALUE_BASES for b in n.bases)
        and not any("dataclass" in ast.unparse(d) for d in n.decorator_list)
    ]
    for body in scopes:
        for node in body:
            for name in _targets(node):
                value = getattr(node, "value", None)
                if _is_slot(value) or ("path" in name.lower() and not name.isupper()):
                    bad.append(f"{name} (line {node.lineno})")
    return bad


@pytest.mark.parametrize("path", AGENTS, ids=[p.stem for p in AGENTS])
def test_no_module_level_slot_for_call_state(path: Path) -> None:
    """No module-level (or class-level) name waits to be filled per call: a
    ``None``/``{}``/``[]`` slot, or a path variable. Constants with a value are fine."""
    bad = _mutable_slots(_tree(path))
    assert bad == [], f"{path.name}: module state a call could fill {bad}"


@pytest.mark.parametrize("path", AGENTS + HANDLERS, ids=[p.stem for p in AGENTS + HANDLERS])
def test_no_global_rebinding(path: Path) -> None:
    found = [n.lineno for n in ast.walk(_tree(path)) if isinstance(n, ast.Global)]
    assert found == [], f"{path.name}: `global` at lines {found}"


@pytest.mark.parametrize("path", AGENTS + HANDLERS, ids=[p.stem for p in AGENTS + HANDLERS])
def test_no_environment_variable_is_set(path: Path) -> None:
    lines = _environ_writes(_tree(path))
    assert lines == [], f"{path.name}: sets or removes an environment variable at lines {lines}"


def test_the_checks_catch_the_old_shape() -> None:
    """The three checks find what the designer modules used to hold."""
    old = ast.parse(
        "import os\n"
        "_collector_path: str | None = None\n"
        "def run(p):\n"
        "    global _collector_path\n"
        "    _collector_path = p\n"
        "    os.environ['DECISION_TRACE_PATH'] = p\n"
        "    os.environ.pop('DECISION_TRACE_PATH', None)\n"
    )
    assert _mutable_slots(old) == ["_collector_path (line 2)"]
    for slot in ("_current = None", "_inputs = {}", "_seen = []", "_cache = dict()"):
        assert _mutable_slots(ast.parse(slot)), slot
    assert _mutable_slots(ast.parse("class A:\n    inputs = []\n")) == ["inputs (line 2)"]
    assert _mutable_slots(ast.parse("class M(BaseModel):\n    x: str | None = None\n")) == []
    assert _mutable_slots(ast.parse("@dataclass\nclass D:\n    x: str | None = None\n")) == []
    assert _mutable_slots(ast.parse("DEFAULT_SKILL_PATH = 'x'\nlogger = get(1)\n")) == []
    assert any(isinstance(n, ast.Global) for n in ast.walk(old))
    assert _environ_writes(old) == [6, 7]


def _tool_modules() -> list[Any]:
    from src.tools.schema import dynamodb_schema_agent, opensearch_schema_agent

    return [dynamodb_schema_agent, opensearch_schema_agent]


@pytest.mark.parametrize("index", [0, 1], ids=["dynamodb", "opensearch"])
def test_each_input_tool_reads_the_files_it_was_built_with(
    index: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two tools built for two calls each read their own files, whichever was
    built last (the tool-reading designers, #1054)."""
    from src.tools.schema.base_schema_agent import AgentInputPaths

    mod = _tool_modules()[index]
    seen: list[str | None] = []

    def fake_project(paths: AgentInputPaths | None = None) -> dict:
        seen.append(paths.collector if paths else None)
        return {}

    first = mod._input_tool(AgentInputPaths("a.json", "a-analysis.json"))
    second = mod._input_tool(AgentInputPaths("b.json", "b-analysis.json"))
    monkeypatch.setattr(mod, "_project_input", fake_project)
    first()
    second()
    first()
    assert seen == ["a.json", "b.json", "a.json"]
    assert first.tool_name == "load_agent_input"


class TestAPipelineCallNeverReadsTheEnvironment:
    """#1054 review: the pipeline passes the collector and analysis paths, and
    then a variable left in the shell never fills the others."""

    def test_given_paths_ignore_the_environment(self, monkeypatch) -> None:
        from src.tools.schema.base_schema_agent import AgentInputPaths

        monkeypatch.setenv("DECISION_TRACE_PATH", "/stale/trace.json")
        monkeypatch.setenv("COLLECTOR_OUTPUT_PATH", "/stale/collector.json")
        paths = AgentInputPaths.resolve("c.json", "a.json", None, "r.json")
        assert paths == AgentInputPaths("c.json", "a.json", None, "r.json")

    def test_a_standalone_call_with_no_paths_reads_it(self, monkeypatch) -> None:
        from src.tools.schema.base_schema_agent import AgentInputPaths

        monkeypatch.setenv("COLLECTOR_OUTPUT_PATH", "c.json")
        monkeypatch.setenv("ANALYSIS_OUTPUT_PATH", "a.json")
        monkeypatch.setenv("DECISION_TRACE_PATH", "t.json")
        assert AgentInputPaths.resolve() == AgentInputPaths("c.json", "a.json", "t.json")


@pytest.mark.parametrize("index", [0, 1], ids=["dynamodb", "opensearch"])
def test_an_unreadable_input_fails_the_numbers_check_loudly(index: int) -> None:
    """#1054 review: the numbers check reads the call's own input; without it the
    design fails instead of running with the check silently off."""
    from src.tools.schema.base_schema_agent import AgentInputPaths

    mod = _tool_modules()[index]
    with pytest.raises((ValueError, OSError)):
        mod._project_input(AgentInputPaths("/does/not/exist.json", "/nor/this.json"))
    with pytest.raises(ValueError):
        mod._project_input(AgentInputPaths())
