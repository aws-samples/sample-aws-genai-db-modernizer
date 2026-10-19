"""Guard test: only ``write_schema_output`` writes a ``schema_output.json`` (#1186).

``src/agents/schema_design/output_contract.py`` checks a design against its
engine's output contract (or a skipped placeholder against its own) before it
writes ``schema-<engine>/v<N>/schema_output.json``. This test scans the AST of
every module under ``src/`` and ``scripts/`` and fails when any other module
writes one directly, so a new writer cannot put a design on disk unchecked.

Shapes (one finding each):

1. ``literal_key`` -- ``x.write_json(<key>, ...)`` (also ``write_text`` /
   ``write_bytes``) where ``<key>`` contains the string ``schema_output.json``.
2. ``bound_key`` -- the same call with a name for the key, bound in the same
   function (or module) to an expression that contains ``schema_output.json``.
3. ``prefix_copy`` -- a function that builds a ``schema-{engine}`` path, lists a
   prefix (``list_prefix``) and writes, without calling ``write_schema_output``:
   a copy loop over a schema directory carries the design along with every
   other file in it.

Not caught statically (say so rather than pretend): a key built at run time
from parts that never spell ``schema_output.json`` in the same function, and
writes through a function alias or another store method name.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

_REPO = Path(__file__).resolve().parents[4]
_SCANNED_DIRS = (_REPO / "src", _REPO / "scripts")
_GATE = _REPO / "src" / "agents" / "schema_design" / "output_contract.py"
_TARGET = "schema_output.json"
_WRITES = frozenset({"write_json", "write_text", "write_bytes"})


@dataclass(frozen=True)
class Finding:
    file: str
    function: str
    shape: str
    lineno: int


def _mentions(node: ast.AST, text: str) -> bool:
    return any(
        isinstance(n, ast.Constant) and isinstance(n.value, str) and text in n.value
        for n in ast.walk(node)
    )


def _builds_schema_dir(node: ast.AST) -> bool:
    """An f-string path ``.../schema-{engine}/...`` anywhere in ``node``."""
    for n in ast.walk(node):
        if isinstance(n, ast.JoinedStr):
            for part, nxt in zip(n.values, n.values[1:], strict=False):
                if (
                    isinstance(part, ast.Constant)
                    and str(part.value).endswith("schema-")
                    and isinstance(nxt, ast.FormattedValue)
                ):
                    return True
    return False


def _bound_names(scope: ast.AST) -> set[str]:
    """Names assigned, anywhere in ``scope``, an expression mentioning the target."""
    names: set[str] = set()
    for node in ast.walk(scope):
        if isinstance(node, ast.Assign) and _mentions(node.value, _TARGET):
            names |= {t.id for t in node.targets if isinstance(t, ast.Name)}
        elif (
            isinstance(node, ast.AnnAssign | ast.NamedExpr)
            and node.value is not None
            and _mentions(node.value, _TARGET)
            and isinstance(node.target, ast.Name)
        ):
            names.add(node.target.id)
    return names


def _calls(scope: ast.AST, attrs: frozenset[str]) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(scope)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in attrs
    ]


def _calls_gate(scope: ast.AST) -> bool:
    for node in ast.walk(scope):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == "write_schema_output":
                return True
    return False


def _scan_scope(scope: ast.AST, file: str, function: str, module_names: set[str]) -> list[Finding]:
    findings: list[Finding] = []
    names = _bound_names(scope) | module_names
    for call in _calls(scope, _WRITES):
        if not call.args:
            continue
        key = call.args[0]
        if _mentions(key, _TARGET):
            findings.append(Finding(file, function, "literal_key", call.lineno))
        elif any(isinstance(n, ast.Name) and n.id in names for n in ast.walk(key)):
            findings.append(Finding(file, function, "bound_key", call.lineno))
    lists_a_prefix = bool(_calls(scope, frozenset({"list_prefix"})))
    if (
        lists_a_prefix
        and _builds_schema_dir(scope)
        and _calls(scope, _WRITES)
        and not _calls_gate(scope)
    ):
        findings.append(Finding(file, function, "prefix_copy", scope.lineno))  # type: ignore[attr-defined]
    return findings


def scan_source(source: str, file: str = "<memory>") -> list[Finding]:
    tree = ast.parse(source)
    module_names = {
        t.id
        for node in tree.body
        if isinstance(node, ast.Assign) and _mentions(node.value, _TARGET)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    findings: list[Finding] = []
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)]
    # Innermost functions only own their calls: scan each function without its
    # nested functions so a call is reported once.
    for fn in functions:
        body = copy_without_nested(fn)
        findings += _scan_scope(body, file, fn.name, module_names)
    return sorted(set(findings), key=lambda f: (f.file, f.lineno, f.shape))


def copy_without_nested(fn: ast.AST) -> ast.AST:
    """``fn`` with the bodies of its nested functions removed."""
    import copy

    clone = copy.deepcopy(fn)
    for node in ast.walk(clone):
        if node is clone:
            continue
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            node.body = [ast.Pass()]
    return clone


def _scan_repo() -> list[Finding]:
    findings: list[Finding] = []
    for directory in _SCANNED_DIRS:
        for path in sorted(directory.rglob("*.py")):
            if path == _GATE:
                continue
            rel = str(path.relative_to(_REPO))
            findings += scan_source(path.read_text(encoding="utf-8"), rel)
    return findings


def test_no_module_writes_a_schema_output_without_the_gate():
    findings = _scan_repo()
    assert not findings, "write schema_output.json with output_contract.write_schema_output:\n" + (
        "\n".join(f"  {f.file}:{f.lineno} {f.function} ({f.shape})" for f in findings)
    )


# ---------------------------------------------------------------------------
# The scanner itself: each shape is caught, the gate is not flagged.
# ---------------------------------------------------------------------------


def test_a_literal_key_is_caught():
    src = 'def f(store, d):\n    store.write_json(f"{d}/schema_output.json", {})\n'
    assert [f.shape for f in scan_source(src)] == ["literal_key"]


def test_a_bound_key_is_caught():
    src = (
        "def f(store, base, out):\n"
        '    key = f"{base}/schema_output.json"\n'
        "    store.write_json(key, out)\n"
    )
    assert [f.shape for f in scan_source(src)] == ["bound_key"]


def test_a_copy_loop_over_a_schema_directory_is_caught():
    src = (
        "def f(store, db, job, engine):\n"
        '    old = f"{db}/{job}/schema-{engine}/v1/"\n'
        "    for key in store.list_prefix(old):\n"
        '        store.write_json(key.replace("v1", "v2"), store.read_json(key))\n'
    )
    assert [f.shape for f in scan_source(src)] == ["prefix_copy"]


def test_writing_through_the_gate_is_not_flagged():
    src = (
        "def f(store, base, out, engine):\n"
        '    key = f"{base}/schema_output.json"\n'
        "    write_schema_output(store, key, out, engine)\n"
        '    store.write_json(f"{base}/design_trace.json", {})\n'
    )
    assert scan_source(src) == []


def test_reading_a_schema_output_is_not_flagged():
    src = 'def f(store, base):\n    return store.read_json(f"{base}/schema_output.json")\n'
    assert scan_source(src) == []
