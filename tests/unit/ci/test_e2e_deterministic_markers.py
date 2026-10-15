"""A tests/e2e test that skips for an external job must be marked deterministic.

The headless e2e-llm run executes ``pytest tests/e2e -m "e2e and not deterministic"
--fail-on-skip`` against an external job. A test that calls ``pytest.skip`` when
``run.external`` is true, and is not marked ``deterministic``, therefore runs there,
skips, and fails the run after the long model phase. This guard parses
``tests/e2e/*.py`` and fails at unit-test time instead.

It follows fixtures a test requests by name and calls to functions defined in the same
module or in ``conftest.py``, transitively. It looks for a ``pytest.skip(...)`` reachable
only when an ``if`` test mentioning ``external`` (an attribute, a name or a call such as
``_external_env()``) holds. ``is None`` / ``is not None`` and ``not`` pick the branch.
"""

from __future__ import annotations

import ast
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parents[3] / "tests" / "e2e"
MARKER = "deterministic"

Defs = dict[str, ast.FunctionDef | ast.AsyncFunctionDef]


def _is_marker(node: ast.AST) -> bool:
    if isinstance(node, ast.Call):
        node = node.func
    return isinstance(node, ast.Attribute) and node.attr == MARKER


def _mentions_marker(node: ast.AST) -> bool:
    return any(_is_marker(n) for n in ast.walk(node))


def _is_fixture(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for dec in fn.decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        if isinstance(target, ast.Attribute) and target.attr == "fixture":
            return True
        if isinstance(target, ast.Name) and target.id == "fixture":
            return True
    return False


def _mentions_external(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and "external" in n.id.lower():
            return True
        if isinstance(n, ast.Attribute) and "external" in n.attr.lower():
            return True
    return False


def _external_branch(test: ast.expr) -> str | None:
    """``"body"`` or ``"orelse"``: the branch of an ``if`` reachable only when external."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = _external_branch(test.operand)
        return {"body": "orelse", "orelse": "body"}.get(inner or "")
    if not _mentions_external(test):
        return None
    if (
        isinstance(test, ast.Compare)
        and len(test.ops) == 1
        and isinstance(test.ops[0], (ast.Is, ast.IsNot))
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value is None
    ):
        return "orelse" if isinstance(test.ops[0], ast.Is) else "body"
    return "body"


def _is_skip(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "skip"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "pytest"
    )


def _external_skips(stmts: list[ast.stmt], external_only: bool = False) -> list[int]:
    """Line numbers of ``pytest.skip`` calls reachable only for an external run."""
    lines: list[int] = []
    for stmt in stmts:
        if isinstance(stmt, ast.If):
            branch = _external_branch(stmt.test)
            lines += _external_skips(stmt.body, external_only or branch == "body")
            lines += _external_skips(stmt.orelse, external_only or branch == "orelse")
            continue
        if external_only:
            lines += [n.lineno for n in ast.walk(stmt) if isinstance(n, ast.Call) and _is_skip(n)]
        else:
            for field in ("body", "orelse", "finalbody"):
                inner = getattr(stmt, field, None)
                if isinstance(inner, list) and inner and isinstance(inner[0], ast.stmt):
                    lines += _external_skips(inner, False)
            for handler in getattr(stmt, "handlers", []):
                lines += _external_skips(handler.body, False)
    return lines


def _functions(tree: ast.Module) -> Defs:
    return {
        n.name: n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _module_marked(tree: ast.Module) -> bool:
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in stmt.targets
        ):
            if _mentions_marker(stmt.value):
                return True
    return False


def _args(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    a = fn.args
    return [x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)]


def find_violations(sources: dict[str, str], conftest: str | None = None) -> list[str]:
    """Return one message per unmarked test that can skip for an external run."""
    conf_defs: Defs = _functions(ast.parse(conftest)) if conftest else {}
    problems: list[str] = []
    for name, source in sources.items():
        tree = ast.parse(source)
        if _module_marked(tree):
            continue
        defs = _functions(tree)
        class_marked = {
            id(m): _mentions_marker(c)
            for c in ast.walk(tree)
            if isinstance(c, ast.ClassDef)
            for m in c.body
        }
        for fn in defs.values():
            if not fn.name.startswith("test_") or _is_fixture(fn):
                continue
            if any(_is_marker(d) for d in fn.decorator_list) or class_marked.get(id(fn)):
                continue
            seen: set[str] = set()
            todo = [(fn, name)]
            while todo:
                cur, where = todo.pop()
                for line in _external_skips(cur.body):
                    problems.append(
                        f"{where}:{line}: {fn.name} can pytest.skip for an external job "
                        f"(via {cur.name}) but is not marked deterministic"
                    )
                for call in (n for n in ast.walk(cur) if isinstance(n, ast.Call)):
                    if not isinstance(call.func, ast.Name):
                        continue
                    callee = call.func.id
                    for pool, label in ((defs, name), (conf_defs, "tests/e2e/conftest.py")):
                        dep = pool.get(callee)
                        if dep is not None and callee not in seen:
                            seen.add(callee)
                            todo.append((dep, label))
                            break
                for arg in _args(cur):
                    for pool, label in ((defs, name), (conf_defs, "tests/e2e/conftest.py")):
                        dep = pool.get(arg)
                        if dep is not None and _is_fixture(dep) and arg not in seen:
                            seen.add(arg)
                            todo.append((dep, label))
                            break
    return problems


def _scan() -> list[str]:
    sources = {f"tests/e2e/{p.name}": p.read_text() for p in sorted(E2E_DIR.glob("test_*.py"))}
    conftest = (E2E_DIR / "conftest.py").read_text()
    return find_violations(sources, conftest)


def test_e2e_tests_that_skip_for_external_jobs_are_marked_deterministic() -> None:
    problems = _scan()
    assert not problems, (
        "ci/e2e-llm.sh runs `pytest tests/e2e -m 'e2e and not deterministic' --fail-on-skip` "
        "on an external job, so these tests would skip there and fail the run:\n  "
        + "\n  ".join(problems)
        + "\nFix: add @pytest.mark.deterministic to the test (or `pytestmark = "
        "pytest.mark.deterministic` to the module), or remove the external skip so the "
        "test asserts for external jobs too."
    )


_SKIPPING = """
import pytest

def test_a(run):
    if run.external:
        pytest.skip("x")
"""


def test_guard_flags_an_unmarked_external_skip() -> None:
    assert len(find_violations({"m.py": _SKIPPING})) == 1


def test_guard_accepts_a_marker_on_the_test_or_the_module() -> None:
    on_test = _SKIPPING.replace("def test_a", "@pytest.mark.deterministic\ndef test_a")
    on_module = "pytestmark = pytest.mark.deterministic\n" + _SKIPPING
    assert find_violations({"m.py": on_test}) == []
    assert find_violations({"m.py": on_module}) == []


def test_guard_follows_fixtures_in_the_module_and_in_conftest() -> None:
    fixture = (
        "import pytest\n@pytest.fixture\ndef need(run):\n"
        "    if run.external:\n        pytest.skip('x')\n"
    )
    test = "def test_b(need):\n    pass\n"
    assert len(find_violations({"m.py": fixture + test})) == 1
    assert len(find_violations({"m.py": test}, conftest=fixture)) == 1


def test_guard_handles_negated_conditions_and_unrelated_skips() -> None:
    only_internal = (
        "import pytest\ndef test_c(run):\n    if not run.external:\n        pytest.skip('x')\n"
    )
    else_branch = (
        "import pytest\ndef test_d(run):\n    if not run.external:\n        pass\n"
        "    else:\n        pytest.skip('x')\n"
    )
    data_skip = "import pytest\ndef test_e(run):\n    if not run.rows:\n        pytest.skip('x')\n"
    assert find_violations({"m.py": only_internal}) == []
    assert len(find_violations({"m.py": else_branch})) == 1
    assert find_violations({"m.py": data_skip}) == []


_HELPER_891 = """
import pytest

def _external_env():
    return None

def _sample_run():
    if _external_env() is not None:
        pytest.skip("needs the deterministic pipeline")
    return object()

@pytest.fixture(scope="module")
def run():
    return _sample_run()

def test_f(run):
    pass
"""


def test_guard_flags_the_891_shape_helper_called_from_a_fixture() -> None:
    assert len(find_violations({"m.py": _HELPER_891})) == 1
    marked = "pytestmark = pytest.mark.deterministic\n" + _HELPER_891
    assert find_violations({"m.py": marked}) == []


def test_guard_follows_helpers_from_tests_conftest_and_chains() -> None:
    chain = (
        "import pytest\ndef _inner(r):\n    if r.external:\n        pytest.skip('x')\n"
        "def _outer():\n    _inner(1)\ndef test_g():\n    _outer()\n"
    )
    assert len(find_violations({"m.py": chain})) == 1
    helper = "import pytest\ndef h(r):\n    if r.external:\n        pytest.skip('x')\n"
    assert len(find_violations({"m.py": "def test_h():\n    h(1)\n"}, conftest=helper)) == 1


def test_guard_terminates_on_recursive_helpers() -> None:
    loop = "def a():\n    b()\ndef b():\n    a()\ndef test_i():\n    a()\n"
    assert find_violations({"m.py": loop}) == []


def test_guard_recognises_external_names_calls_and_none_comparisons() -> None:
    def case(test: str, body: str = "pytest.skip('x')", orelse: str = "pass") -> list[str]:
        src = (
            f"import pytest\ndef test_j(external):\n    if {test}:\n        {body}\n"
            f"    else:\n        {orelse}\n"
        )
        return find_violations({"m.py": src})

    assert len(case("external")) == 1
    assert len(case("is_external(1)")) == 1
    assert len(case("_external_env() is not None")) == 1
    assert case("_external_env() is None") == []
    assert len(case("_external_env() is None", body="pass", orelse="pytest.skip('x')")) == 1
    assert case("_external_env() is not None", body="pass", orelse="pytest.skip('x')") == []
    assert case("not external") == []
    assert len(case("not external", body="pass", orelse="pytest.skip('x')")) == 1
    assert case("rows is None") == []
