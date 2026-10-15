"""Guard test: only the move ledger writes a query's engine or tie (#616, ADR-031 Amendment 3).

Every change of a query's engine (``assigned_engine``) or of its unresolved
tie (``tied_engines``) is a Move, recorded by
``src/agents/referee/move_ledger.py`` (``apply`` / ``apply_to_assignment``),
the one module allowed to set either field. This test scans the AST of every
module under ``src/`` and ``scripts/`` and fails when any other module writes
one, so a new pass (or a quick fix in an old one) cannot change an engine or a
tie without leaving a record that replays.

Shapes (one finding each). ``F`` is either field, written as a string literal
or as a name bound to one anywhere in the same module (``F = "assigned_engine"``):

1. ``attr_store`` -- ``x.assigned_engine = ...`` (also ``+=``, an annotated
   assignment, a ``for``/``with`` target or a tuple unpack).
2. ``key_store`` -- ``x[F] = ...`` (``x.__dict__[F]``, ``vars(x)[F]`` too).
3. ``setattr_call`` -- ``setattr(x, F, ...)``, ``object.__setattr__(x, F, ...)``
   or ``operator.setitem(x, F, ...)``.
4. ``update_call`` -- ``x.update({F: ...})``, ``x.update(F=...)``,
   ``x.update(**{F: ...})``, ``x.update([(F, ...)])``, ``x.setdefault(F, ...)``
   or ``x |= {F: ...}``.
5. ``copy_with_engine`` -- a copy of an existing record with a new engine or
   tie: ``{**qa, F: ...}``, ``qa | {F: ...}``, ``dict(qa, F=...)``,
   ``Model(**qa.model_dump(...), F=...)``, ``dataclasses.replace(qa, F=...)``,
   or ``model_copy`` / pydantic v1 ``copy`` with ``update={F: ...}``.
6. ``copy_with_unknown_update`` -- ``model_copy(update=u)`` / ``copy(update=u)``
   where ``u`` is not a dict literal: the scanner cannot see which fields it
   sets, so it is flagged rather than assumed safe.
7. ``private_ledger_access`` -- any module importing or reading a private
   (``_``-prefixed) name of ``move_ledger``: its internals are not a back door.

Not caught statically (say so rather than pretend): ``x.update(d)`` with a
variable ``d`` (``dict.update`` is everywhere, and its keys are only known at
run time), a field name built at run time (``"assigned" + "_engine"``, read
from config), ``setattr`` through a function alias, and ``exec``/``eval``.
Not flagged, by design: building a NEW record whose engine is read from
somewhere else (a dict literal without a ``**`` spread, a constructor's
``assigned_engine=`` keyword with no ``**`` copy). The resolver builds every
``QueryAssignment`` of v1 from ``ledger.engines`` / ``ledger.tied_of``;
reports, the graph, the API and the review surface build projections or
requests (``QueryOverrideInput``) that are not an engine change. Every one of
these cases is covered at run time instead: each writer of an assignment
version (the first assignment, the reality check, the customer gate, the
post-schema router's reroute and rollback) calls
``move_ledger.check_replay`` before writing and refuses a version whose
engines or ties do not follow from its input plus its Moves.

There are no exceptions: since the post-schema router's reroute and
rollback versions record their own Moves (#489 Track L PR4, #770), every
writer goes through the ledger, and the guard fails on any write elsewhere.
Do not add an allowlist back; record the change as a Move instead.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[4]
_SCANNED_DIRS = (_REPO / "src", _REPO / "scripts")
_LEDGER = _REPO / "src" / "agents" / "referee" / "move_ledger.py"
_FIELDS = frozenset({"assigned_engine", "tied_engines"})


@dataclass(frozen=True)
class Finding:
    file: str
    qualname: str
    shape: str
    lineno: int


class _Scanner:
    """The shapes above, for one module (``consts`` are its field-name constants)."""

    def __init__(self, tree: ast.AST) -> None:
        self.consts: set[str] = set()
        for node in ast.walk(tree):
            targets: list[ast.AST] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = [node.target]
            value = getattr(node, "value", None)
            if targets and isinstance(value, ast.Constant) and value.value in _FIELDS:
                self.consts.update(t.id for t in targets if isinstance(t, ast.Name))

    def is_field(self, node: ast.AST | None) -> bool:
        if isinstance(node, ast.Constant):
            return node.value in _FIELDS
        return isinstance(node, ast.Name) and node.id in self.consts

    def dict_has_field(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Dict):
            return any(self.is_field(k) for k in node.keys)
        if isinstance(node, ast.Call) and _call_name(node.func) == "dict":
            return any(kw.arg in _FIELDS for kw in node.keywords)
        return False

    def pairs_have_field(self, node: ast.AST) -> bool:
        return isinstance(node, (ast.List, ast.Tuple)) and any(
            isinstance(e, (ast.Tuple, ast.List)) and e.elts and self.is_field(e.elts[0])
            for e in node.elts
        )

    def shape(self, node: ast.AST) -> str | None:
        """The guard's shape for ``node``, or ``None`` when it does not write a field."""
        if (
            isinstance(node, ast.Attribute)
            and node.attr in _FIELDS
            and isinstance(node.ctx, ast.Store)
        ):
            return "attr_store"
        if (
            isinstance(node, ast.Subscript)
            and self.is_field(node.slice)
            and isinstance(node.ctx, ast.Store)
        ):
            return "key_store"
        if isinstance(node, ast.Dict) and None in node.keys and self.dict_has_field(node):
            return "copy_with_engine"
        if isinstance(node, ast.AugAssign) and isinstance(node.op, ast.BitOr):
            if self.dict_has_field(node.value):
                return "update_call"
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            if self.dict_has_field(node.left) or self.dict_has_field(node.right):
                return "copy_with_engine"
        if isinstance(node, (ast.ImportFrom, ast.Attribute)):
            return _private_ledger_access(node)
        if not isinstance(node, ast.Call):
            return None
        name = _call_name(node.func)
        keywords = {kw.arg for kw in node.keywords}
        spread = None in keywords
        if name in ("setattr", "__setattr__", "setitem") and any(
            self.is_field(a) for a in node.args[:2]
        ):
            return "setattr_call"
        if name == "update" and (
            keywords & _FIELDS
            or any(self.dict_has_field(a) or self.pairs_have_field(a) for a in node.args)
            or any(kw.arg is None and self.dict_has_field(kw.value) for kw in node.keywords)
        ):
            return "update_call"
        if name == "setdefault" and node.args and self.is_field(node.args[0]):
            return "update_call"
        if name in ("model_copy", "copy"):
            for kw in node.keywords:
                if kw.arg != "update":
                    continue
                if self.dict_has_field(kw.value):
                    return "copy_with_engine"
                if not isinstance(kw.value, ast.Dict):
                    return "copy_with_unknown_update"
        # ``Model(**x.model_dump(...), F=...)``: a class (capitalised name) built
        # from a spread copy with a new value. A lowercase call with a spread is a
        # function call (the ledger's own ``apply_to_assignment(..., **scores)``).
        constructor = name[:1].isupper() or name == "model_validate"
        if keywords & _FIELDS and (name in ("dict", "replace") or (spread and constructor)):
            return "copy_with_engine"
        return None


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _private_ledger_access(node: ast.AST) -> str | None:
    """``from ...move_ledger import _x`` or ``move_ledger._x``."""
    if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("move_ledger"):
        if any(alias.name.startswith("_") for alias in node.names):
            return "private_ledger_access"
    if (
        isinstance(node, ast.Attribute)
        and node.attr.startswith("_")
        and not node.attr.startswith("__")
        and isinstance(node.value, ast.Name)
        and node.value.id == "move_ledger"
    ):
        return "private_ledger_access"
    return None


def scan_source(source: str, file: str) -> list[Finding]:
    """Every write of a guarded field in ``source``, with its enclosing function."""
    findings: list[Finding] = []
    tree = ast.parse(source)
    scanner = _Scanner(tree)

    def visit(node: ast.AST, stack: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                visit(child, [*stack, child.name])
                continue
            shape = scanner.shape(child)
            if shape:
                findings.append(
                    Finding(file, ".".join(stack) or "<module>", shape, child.lineno)  # type: ignore[attr-defined]
                )
            visit(child, stack)

    visit(tree, [])
    return findings


def _scanned_files() -> list[Path]:
    files = sorted(p for d in _SCANNED_DIRS for p in d.rglob("*.py"))
    return [p for p in files if p != _LEDGER]


def _all_findings() -> list[Finding]:
    findings: list[Finding] = []
    for path in _scanned_files():
        text = path.read_text(encoding="utf-8")
        if not any(f in text for f in _FIELDS) and "move_ledger" not in text:
            continue  # cheap pre-filter: no mention, no write
        findings.extend(scan_source(text, str(path.relative_to(_REPO))))
    return findings


def test_only_the_move_ledger_writes_engines_and_ties() -> None:
    findings = _all_findings()
    assert not findings, (
        "assigned_engine or tied_engines is written outside src/agents/referee/move_ledger.py. "
        "Record the change as a Move (move_ledger.apply / apply_to_assignment) instead:\n"
        + "\n".join(f"  {f.file}:{f.lineno} in {f.qualname}() [{f.shape}]" for f in findings)
    )


def test_the_post_schema_router_writes_through_the_ledger() -> None:
    """The last writer outside the ledger (#489 Track L PR4) is gone: the
    reroute and rollback versions are built through move_ledger, so the scan
    of the orchestrator finds nothing (not even under an exception)."""
    path = _REPO / "src" / "orchestrator" / "local_orchestrator.py"
    assert scan_source(path.read_text(encoding="utf-8"), str(path.relative_to(_REPO))) == []


def test_the_ledger_itself_is_the_writer() -> None:
    """Self-check: the scanner does see the ledger's own writes, so an empty
    result elsewhere means no writes, not a blind scanner."""
    shapes = {f.shape for f in scan_source(_LEDGER.read_text(encoding="utf-8"), "move_ledger.py")}
    assert shapes, "the scanner found no write in move_ledger.py itself"


@pytest.mark.parametrize(
    ("snippet", "shape"),
    [
        ("def f(qa):\n    qa.assigned_engine = 'dynamodb'\n", "attr_store"),
        ("def f(qa):\n    qa.assigned_engine += ''\n", "attr_store"),
        ("def f(qa, b):\n    qa.assigned_engine, b = 'x', 1\n", "attr_store"),
        ("def f(qa):\n    qa['assigned_engine'] = 'dynamodb'\n", "key_store"),
        ("def f(qa):\n    qa.__dict__['assigned_engine'] = 'dynamodb'\n", "key_store"),
        ("def f(qa):\n    setattr(qa, 'assigned_engine', 'dynamodb')\n", "setattr_call"),
        (
            "def f(qa):\n    object.__setattr__(qa, 'assigned_engine', 'dynamodb')\n",
            "setattr_call",
        ),
        ("def f(qa):\n    qa.update({'assigned_engine': 'dynamodb'})\n", "update_call"),
        ("def f(qa):\n    qa.update(assigned_engine='dynamodb')\n", "update_call"),
        ("def f(qa):\n    qa.setdefault('assigned_engine', 'dynamodb')\n", "update_call"),
        ("def f(qa):\n    return {**qa, 'assigned_engine': 'dynamodb'}\n", "copy_with_engine"),
        ("def f(qa):\n    return dict(qa, assigned_engine='dynamodb')\n", "copy_with_engine"),
        (
            "def f(qa):\n    return qa.model_copy(update={'assigned_engine': 'dynamodb'})\n",
            "copy_with_engine",
        ),
        ("def f(qa):\n    qa['tied_engines'] = []\n", "key_store"),
        ("def f(qa):\n    qa.tied_engines = []\n", "attr_store"),
        ("F = 'assigned_engine'\ndef f(qa, e):\n    qa[F] = e\n", "key_store"),
        ("F = 'assigned_engine'\ndef f(qa, e):\n    setattr(qa, F, e)\n", "setattr_call"),
        ("def f(qa, e):\n    qa.update(**{'assigned_engine': e})\n", "update_call"),
        ("def f(qa, e):\n    qa.update([('assigned_engine', e)])\n", "update_call"),
        ("def f(qa, e):\n    qa |= {'assigned_engine': e}\n", "update_call"),
        ("def f(qa, e):\n    return qa | {'assigned_engine': e}\n", "copy_with_engine"),
        ("def f(qa, u):\n    return qa.model_copy(update=u)\n", "copy_with_unknown_update"),
        ("def f(qa, e):\n    return qa.copy(update={'assigned_engine': e})\n", "copy_with_engine"),
        (
            "import dataclasses\ndef f(qa, e):\n    return dataclasses.replace(qa, assigned_engine=e)\n",
            "copy_with_engine",
        ),
        (
            "import operator\ndef f(qa, e):\n    operator.setitem(qa, 'assigned_engine', e)\n",
            "setattr_call",
        ),
        (
            "def f(qa, e):\n    return QueryAssignment(\n"
            "        **qa.model_dump(exclude={'assigned_engine'}), assigned_engine=e\n    )\n",
            "copy_with_engine",
        ),
        (
            "from src.agents.referee.move_ledger import _get\n",
            "private_ledger_access",
        ),
        (
            "from src.agents.referee import move_ledger\n"
            "def f(qa):\n    return move_ledger._get(qa, 'query_id')\n",
            "private_ledger_access",
        ),
    ],
)
def test_each_shape_is_caught(snippet: str, shape: str) -> None:
    assert [f.shape for f in scan_source(snippet, "x.py")] == [shape]


@pytest.mark.parametrize(
    "snippet",
    [
        "def f(qa):\n    return qa['assigned_engine']\n",
        "def f(qa):\n    return qa.assigned_engine == 'dynamodb'\n",
        "def f(e):\n    return {'query_id': 'q', 'assigned_engine': e}\n",
        "def f(e):\n    return QueryOverrideInput('q', assigned_engine=e)\n",
        "def f(qa):\n    return qa.get('assigned_engine')\n",
        "def f(qa, ledger):\n    return QueryAssignment(query_id='q', tied_engines=ledger.tied_of('q'))\n",
        "def f(a):\n    return a.model_copy(update={'version': 2})\n",
        "def f(s):\n    s |= {'x'}\n",
        "def f(ledger, qa, s):\n    apply_to_assignment(ledger, qa, 'x', tied_engines=[], **s)\n",
    ],
)
def test_reads_and_new_records_are_not_writes(snippet: str) -> None:
    assert scan_source(snippet, "x.py") == []


def test_a_finding_names_its_function() -> None:
    src = "class A:\n    def m(self, qa):\n        qa.assigned_engine = 'x'\n"
    assert [f.qualname for f in scan_source(src, "x.py")] == ["A.m"]
