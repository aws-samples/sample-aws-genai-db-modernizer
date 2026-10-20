"""Guard: ``query_type`` has one rule, ``statement_query_type`` (#1178).

Before #1178 six copies typed a collected statement -- a regex in
``offline_parser._transform_queries`` and ``_extract_query_type`` in the MySQL,
PostgreSQL, SQL Server, Oracle and RDS tools -- and none skipped a leading comment.
This test scans the AST of every module under ``src/`` and ``scripts/`` and fails on
a new copy, in any of these shapes:

1. ``typing_function`` -- a function whose name contains ``query_type`` and that is
   not :func:`src.tools.sql.query_type.statement_query_type`.
2. ``verb_regex`` -- a string literal with a ``SELECT|INSERT`` alternation (the
   regex every copy used), outside ``src/tools/sql/query_type.py``.
3. ``verb_prefix`` -- ``x.startswith("SELECT")`` (any statement verb), or a loop
   over a literal of verbs that calls ``startswith`` (the SQL Server copy).
4. ``first_token_verb`` -- ``x.split()[0]`` compared with a literal of verbs (the
   Oracle copy).
5. ``collected_type`` -- in a collector tool module (``src/tools/database/``,
   ``src/tools/aws/``), a ``"query_type"`` dict entry whose value is a call to
   anything but ``statement_query_type``.

Allowed, with its reason (an entry that matches nothing fails, so the list cannot
go stale): ``regex_facts._is_update`` asks whether one write-target match is an
``UPDATE``, not what a statement is. Not caught statically: a verb list built at
run time, or a regex assembled from pieces.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[4]
_SCANNED_DIRS = (_REPO / "src", _REPO / "scripts")
_RULE_MODULE = "src/tools/sql/query_type.py"
_COLLECTOR_TOOL_DIRS = ("src/tools/database/", "src/tools/aws/")
_VERBS = frozenset({"SELECT", "INSERT", "UPDATE", "DELETE", "MERGE", "REPLACE"})
# (module, shape) -> reason
_ALLOWED: dict[tuple[str, str], str] = {
    ("src/tools/sql/regex_facts.py", "verb_prefix"): (
        "_is_update: whether one write-target regex match is an UPDATE"
    ),
}


def _verb_literal(node: ast.AST) -> bool:
    """A tuple/list/set literal holding at least two statement verbs."""
    if not isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return False
    words = {
        str(e.value).upper()
        for e in node.elts
        if isinstance(e, ast.Constant) and isinstance(e.value, str)
    }
    return len(words & _VERBS) >= 2


def _is_verb(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) and str(node.value).upper() in _VERBS


def _is_first_token(node: ast.AST) -> bool:
    """``<expr>.split(...)[0]`` (with any ``.upper()``/``.lower()`` around it)."""
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr in ("upper", "lower", "strip"):
            node = node.func.value
        else:
            break
    return (
        isinstance(node, ast.Subscript)
        and isinstance(node.slice, ast.Constant)
        and node.slice.value == 0
        and isinstance(node.value, ast.Call)
        and isinstance(node.value.func, ast.Attribute)
        and node.value.func.attr == "split"
    )


def findings(tree: ast.AST, rel: str) -> list[tuple[str, int]]:
    """(shape, line) for every shape above in one module."""
    out: list[tuple[str, int]] = []
    in_rule_module = rel == _RULE_MODULE
    in_collector_tools = rel.startswith(_COLLECTOR_TOOL_DIRS)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if "query_type" in node.name and node.name != "statement_query_type":
                out.append(("typing_function", node.lineno))
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and "SELECT|INSERT" in node.value.upper()
            and not in_rule_module
        ):
            out.append(("verb_regex", node.lineno))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in ("startswith", "match") and any(_is_verb(a) for a in node.args):
                out.append(("verb_prefix", node.lineno))
        elif isinstance(node, ast.For) and _verb_literal(node.iter):
            calls = [
                n
                for n in ast.walk(node)
                if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "startswith"
            ]
            if calls:
                out.append(("verb_prefix", node.lineno))
        elif isinstance(node, ast.Compare):
            sides = [node.left, *node.comparators]
            if any(_is_first_token(s) for s in sides) and any(
                _verb_literal(s) or _is_verb(s) for s in sides
            ):
                out.append(("first_token_verb", node.lineno))
        if in_collector_tools and isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if (
                    isinstance(key, ast.Constant)
                    and key.value == "query_type"
                    and isinstance(value, ast.Call)
                ):
                    func = value.func
                    name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
                    if name != "statement_query_type":
                        out.append(("collected_type", value.lineno))
    return out


def _scan() -> dict[str, list[tuple[str, int]]]:
    found: dict[str, list[tuple[str, int]]] = {}
    for root in _SCANNED_DIRS:
        for path in sorted(root.rglob("*.py")):
            if "node_modules" in path.parts:
                continue
            rel = path.relative_to(_REPO).as_posix()
            hits = findings(ast.parse(path.read_text(encoding="utf-8"), filename=rel), rel)
            if hits:
                found[rel] = hits
    return found


def test_query_type_has_one_rule() -> None:
    found = {rel: [h for h in hits if (rel, h[0]) not in _ALLOWED] for rel, hits in _scan().items()}
    found = {rel: hits for rel, hits in found.items() if hits}
    assert not found, (
        "A private query_type rule (#1178): "
        + "; ".join(f"{f}: {hits}" for f, hits in found.items())
        + ". Call src.tools.sql.query_type.statement_query_type(sql, source_engine)."
    )


@pytest.mark.passes_on_main
def test_every_allowed_entry_still_matches() -> None:
    found = {(rel, shape) for rel, hits in _scan().items() for shape, _ in hits}
    assert not set(_ALLOWED) - found


def test_the_collector_tools_type_through_the_rule() -> None:
    """Every collector tool module that types a query calls the one rule."""
    callers = {
        rel: path.read_text(encoding="utf-8")
        for rel, path in (
            (p.relative_to(_REPO).as_posix(), p)
            for d in _COLLECTOR_TOOL_DIRS
            for p in sorted((_REPO / d).glob("*.py"))
        )
        if '"query_type":' in path.read_text(encoding="utf-8")
    }
    expected = {
        "src/tools/aws/rds_tools.py",
        "src/tools/database/mariadb_tools.py",
        "src/tools/database/mysql_tools.py",
        "src/tools/database/offline_parser.py",
        "src/tools/database/oracle_tools.py",
        "src/tools/database/postgres_tools.py",
        "src/tools/database/sqlserver_tools.py",
    }
    assert set(callers) == expected
    for rel, text in callers.items():
        assert "statement_query_type(" in text, rel


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    ("source", "shape"),
    [
        ("def _extract_query_type(sql):\n    return 'OTHER'\n", "typing_function"),
        (
            "import re\nR = re.compile(r'^\\s*(SELECT|INSERT|UPDATE)\\b')\n",
            "verb_regex",
        ),
        ("def f(s):\n    return s.upper().startswith('SELECT')\n", "verb_prefix"),
        (
            "def f(s):\n    for k in ('SELECT', 'INSERT'):\n        if s.startswith(k):\n"
            "            return k\n",
            "verb_prefix",
        ),
        (
            "def f(s):\n    first = s.split()[0].upper()\n"
            "    return s.split()[0].upper() in ('SELECT', 'INSERT')\n",
            "first_token_verb",
        ),
    ],
)
def test_the_scanner_catches_each_shape(source: str, shape: str) -> None:
    assert shape in {s for s, _ in findings(ast.parse(source), "src/x.py")}


@pytest.mark.passes_on_main
def test_the_scanner_catches_a_collected_type_from_another_function() -> None:
    source = "def f(q):\n    return {'query_type': guess(q)}\n"
    assert findings(ast.parse(source), "src/tools/database/x_tools.py") == [("collected_type", 2)]
    # Reading a type that is already there is not typing.
    assert not findings(ast.parse("def f(q):\n    return {'query_type': q.get('t')}\n"), "src/x.py")


@pytest.mark.passes_on_main
def test_the_scanner_ignores_reading_a_query_type() -> None:
    source = (
        "def f(q):\n    qt = str(q.get('query_type') or '').upper()\n"
        "    return qt in {'SELECT', 'INSERT'}\n"
    )
    assert not findings(ast.parse(source), "src/x.py")
