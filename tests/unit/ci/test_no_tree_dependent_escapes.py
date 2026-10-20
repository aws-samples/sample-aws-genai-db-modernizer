"""No test passes because a repo path is missing (#1238).

Every CI environment runs the same tree (maintainer decision, 2026-10-20). A test
that checks ``if not <repo path>.exists(): return`` passes without asserting
anything in a tree that lacks the path, so a broken workflow or a stale doc path
goes green there. This guard parses every test module (``test_*.py``,
``*_test.py``) and ``conftest.py`` under ``tests/`` (fixture folders excluded) and
fails on any of these, keyed on whether a repo path exists:

- an ``if`` whose test checks a repo path's existence and where neither branch
  fails or records the outcome. That covers early returns (of anything:
  ``return``, ``return False``), ``continue``, skips, and asserts wrapped in
  ``if p.exists():`` with no ``else``;
- a conditional expression on such a check (``p.read_text() if p.exists() else ""``);
- a ``try`` that touches a repo path and whose ``except FileNotFoundError`` /
  ``OSError`` (or ``Exception``, or a bare ``except``) handler neither fails nor
  records the outcome;
- ``pytest.mark.skipif``, ``pytest.mark.xfail``, ``pytest.mark.skip``,
  ``unittest.skipIf`` or ``unittest.skipUnless`` with such a check as an argument;
- ``pytest.importorskip`` of a repo-local module (it is always there).

An existence check is ``.exists()``, ``.is_dir()``, ``.is_file()`` or
``os.path.exists/isdir/isfile/lexists`` (also through ``from os import path``,
``import os.path as osp`` or ``from os.path import exists``), directly, through a
name bound to one, or through a same-module helper that returns one. A repo path
is built from ``__file__``, ``Path.cwd()`` / ``os.getcwd()``, a repo-root name
(``REPO``, ``REPO_ROOT``, ``REPO_DIR``, ``PROJECT_ROOT``, ``ROOT``, ...) or a
relative path literal (``Path("docs")``, ``os.path.isdir("ui")``). Names are
followed through assignments in the module, the enclosing functions and the
class (``self.root``), and through ``from tests.<module> import NAME``.

A branch fails when it raises, calls ``pytest.fail`` (or a same-module helper that
does) or asserts a false constant. It records the outcome when it appends to (or
``+=``) a name that an ``assert`` in the same function reads, or returns a value
other than a constant or an empty literal (``return [f"{rel} does not exist"]``),
in both cases only if every call of the function in the module ends in an
``assert`` (directly, through a variable, or through another such function).

Not violations: paths under ``tmp_path`` (tests of planted folders), and a check on
a path built from the loop variable of a listing (``glob``, ``rglob``, ``iterdir``,
``os.walk``, ``git ls-files`` output, or a same-module helper that makes one), as
long as the branch only ``continue``-s past the item or picks a value: a literal
list is not a listing, and a ``return``, ``break`` or skip is never exempt.
Helper modules under ``tests/`` that are not test modules (the routing snapshot
generator, say) are not scanned, since writing a baseline that does not exist yet
is their job; names they define are still followed.

Out of reach, by design: iterating an empty listing (``for f in
REPO_ROOT.glob("*.yml"): assert ...``) has no branch to see, and neither does a
comprehension filter (``[p for p in names if (REPO / p).exists()]``); pair them
with a minimum-count assert. ``tests/e2e/test_customer_facing_wording.py``'s
``if not path.is_file(): ... return`` is on a job-directory path from the ``run``
fixture, not a repo path, so it is correctly outside this guard: whether a job
wrote a file is data, not the tree. The fix for a violation is to assert
unconditionally.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import TypeVar, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
TESTS_DIR = REPO_ROOT / "tests"
EXISTENCE_METHODS = {"exists", "is_dir", "is_file"}
OS_PATH_CHECKS = {"exists", "isdir", "isfile", "lexists"}
REPO_NAME = re.compile(r"(?i:repo(_?(root|dir))?|project_?(root|dir))|ROOT|ROOT_DIR")
LISTING_CALLS = {
    "glob",
    "rglob",
    "iterdir",
    "listdir",
    "scandir",
    "walk",
    "split",
    "splitlines",
    "run",
    "check_output",
    "findall",
    "finditer",
}
SKIP_CALLS = {"skip", "xfail", "importorskip"}
MARK_CALLS = {"skipif", "xfail", "skip", "skipIf", "skipUnless"}
RECORD_METHODS = {"append", "extend", "add", "update", "insert"}
OS_ERRORS = {
    "FileNotFoundError",
    "OSError",
    "IOError",
    "EnvironmentError",
    "NotADirectoryError",
    "IsADirectoryError",
}
REPO_PACKAGES = {"src", "scripts", "tests", "ci", "benchmarks"}
DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)
NESTED = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)

Env = dict[str, list[ast.expr]]
NO_ENV: Env = {}
Func = ast.FunctionDef | ast.AsyncFunctionDef
Loader = Callable[[str], "Module | None"]
T = TypeVar("T")


def _walk_scope(nodes: Iterable[ast.AST]) -> Iterable[ast.AST]:
    """Every node under ``nodes``, not entering nested functions, classes or lambdas."""
    stack = list(nodes)
    while stack:
        node = stack.pop()
        yield node
        stack.extend(c for c in ast.iter_child_nodes(node) if not isinstance(c, NESTED))


def _key(target: ast.AST) -> str | None:
    if isinstance(target, ast.Name):
        return target.id
    if (
        isinstance(target, ast.Attribute)
        and isinstance(target.value, ast.Name)
        and target.value.id in {"self", "cls"}
    ):
        return f"self.{target.attr}"
    return None


def _bindings(nodes: Iterable[ast.AST], prefix: str = "") -> Env:
    """Key -> bound expressions in one scope; loop and ``with`` targets bind to their source."""
    env: Env = {}

    def bind(target: ast.AST, value: ast.expr) -> None:
        for node in ast.walk(target):
            key = _key(node)
            if key is not None:
                env.setdefault(prefix + key if "." not in key else key, []).append(value)

    for node in _walk_scope(nodes):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                bind(target, node.value)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and node.value is not None:
            bind(node.target, node.value)
        elif isinstance(node, ast.NamedExpr):
            bind(node.target, node.value)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            bind(node.target, node.iter)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            bind(node.optional_vars, node.context_expr)
    return env


def _is_relative_literal(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and not node.value.startswith("/")
    )


def _call_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_empty_value(value: ast.expr | None) -> bool:
    if value is None or isinstance(value, ast.Constant):
        return True
    if isinstance(value, (ast.List, ast.Tuple, ast.Set)):
        return not value.elts
    if isinstance(value, ast.Dict):
        return not value.keys
    return (
        isinstance(value, ast.Call)
        and isinstance(value.func, ast.Name)
        and value.func.id in {"list", "tuple", "set", "dict", "frozenset"}
        and not value.args
    )


class Module:
    def __init__(self, source: str, path: str, loader: Loader | None = None) -> None:
        self.tree = ast.parse(source)
        self.path = path
        self.loader = loader
        self.parent: dict[ast.AST, ast.AST] = {
            c: p for p in ast.walk(self.tree) for c in ast.iter_child_nodes(p)
        }
        self.functions: dict[str, Func] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, DEFS):
                self.functions.setdefault(node.name, node)
        self.imports: dict[str, tuple[str, str]] = {}
        self.os_path_modules: set[str] = set()
        self.os_path_funcs: set[str] = set()
        for node in ast.walk(self.tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                for alias in node.names:
                    local = alias.asname or alias.name
                    if node.module == "os" and alias.name == "path":
                        self.os_path_modules.add(local)
                    elif node.module in {"os.path", "posixpath"}:
                        self.os_path_funcs.add(local)
                    else:
                        self.imports[local] = (node.module, alias.name)
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "os.path" and alias.asname:
                        self.os_path_modules.add(alias.asname)
        self.module_env = _bindings(self.tree.body)
        self._envs: dict[ast.AST, Env] = {}
        self._listing: dict[ast.AST, frozenset[str]] = {}
        self._asserted: dict[str, bool] = {}
        self._memo: dict[tuple[str, object], object] = {}
        self._calls: dict[str, list[ast.Call]] = {}
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Call):
                name = _call_name(node)
                if name is not None:
                    self._calls.setdefault(name, []).append(node)

    # --- scopes -------------------------------------------------------------

    def _chain(self, node: ast.AST) -> list[ast.AST]:
        """Enclosing classes and functions of ``node``, outermost first."""
        chain: list[ast.AST] = []
        cur = self.parent.get(node)
        while cur is not None:
            if isinstance(cur, (*DEFS, ast.ClassDef)):
                chain.append(cur)
            cur = self.parent.get(cur)
        return chain[::-1]

    def scope_of(self, node: ast.AST) -> ast.AST:
        defs = [n for n in self._chain(node) if isinstance(n, DEFS)]
        return defs[-1] if defs else self.tree

    def env_for(self, scope: ast.AST) -> Env:
        if scope in self._envs:
            return self._envs[scope]
        env: Env = {k: list(v) for k, v in self.module_env.items()}
        chain = [*self._chain(scope), scope] if scope is not self.tree else []
        for node in chain:
            if isinstance(node, ast.ClassDef):
                class_env = _bindings(node.body, prefix="self.")
                for method in node.body:
                    if isinstance(method, DEFS):
                        for k, v in _bindings(method.body).items():
                            if k.startswith("self."):
                                class_env.setdefault(k, []).extend(v)
                for k, v in class_env.items():
                    env.setdefault(k, []).extend(v)
            elif isinstance(node, DEFS):
                for k, v in _bindings(node.body).items():
                    env[k] = env.get(k, []) + v
        self._envs[scope] = env
        return env

    def _imported(self, key: str) -> tuple[Module, str] | None:
        if key not in self.imports or self.loader is None:
            return None
        module_name, name = self.imports[key]
        other = self.loader(module_name)
        return (other, name) if other is not None and other is not self else None

    def _resolve(self, node: ast.AST) -> str | None:
        if isinstance(node, (ast.Name, ast.Attribute)):
            return _key(node)
        return None

    # --- repo paths and existence checks -----------------------------------

    def _memo_get(self, tag: str, key: object, compute: Callable[[], T], default: T) -> T:
        """Memoised by ``(tag, key)``; a cycle reads ``default`` while it is computed."""
        slot = (tag, key)
        if slot in self._memo:
            return cast(T, self._memo[slot])
        self._memo[slot] = default
        value = compute()
        self._memo[slot] = value
        return value

    def is_repo_path(self, expr: ast.AST, env: Env) -> bool:
        for node in ast.walk(expr):
            if isinstance(node, ast.Name) and (
                node.id == "__file__" or REPO_NAME.fullmatch(node.id)
            ):
                return True
            if isinstance(node, ast.Attribute) and REPO_NAME.fullmatch(node.attr):
                if isinstance(node.value, ast.Name) and node.value.id in {"self", "cls"}:
                    return True
            key = self._resolve(node)
            if key is not None and self._key_is_repo_path(key, env):
                return True
            if isinstance(node, ast.Call):
                name = _call_name(node)
                if name in {"cwd", "getcwd"}:
                    return True
                if name in {"Path", "PurePath", "PosixPath"} and node.args:
                    if _is_relative_literal(node.args[0]):
                        return True
                if self._is_os_path_call(node) and node.args and _is_relative_literal(node.args[0]):
                    return True
        return False

    def _key_is_repo_path(self, key: str, env: Env) -> bool:
        def compute() -> bool:
            if any(self.is_repo_path(v, env) for v in env.get(key, [])):
                return True
            imported = self._imported(key)
            if imported is None:
                return False
            other, name = imported
            return other._key_is_repo_path(name, other.module_env)

        return self._memo_get("repo", (key, id(env)), compute, False)

    def _is_os_path_call(self, call: ast.Call) -> bool:
        func = call.func
        if isinstance(func, ast.Name):
            return func.id in self.os_path_funcs
        if not isinstance(func, ast.Attribute):
            return False
        owner = func.value
        if isinstance(owner, ast.Attribute) and owner.attr == "path":
            return isinstance(owner.value, ast.Name) and owner.value.id == "os"
        return isinstance(owner, ast.Name) and owner.id in self.os_path_modules

    def _direct_check(self, node: ast.AST, env: Env, listing: frozenset[str]) -> bool | None:
        """For an existence call on a repo path: True if listing-derived, else False."""
        if not isinstance(node, ast.Call):
            return None
        name = _call_name(node)
        target: ast.AST | None = None
        if (
            isinstance(node.func, ast.Attribute)
            and name in EXISTENCE_METHODS
            and self.is_repo_path(node.func.value, env)
        ):
            target = node.func.value
        elif (
            name in OS_PATH_CHECKS
            and self._is_os_path_call(node)
            and node.args
            and (self.is_repo_path(node.args[0], env) or _is_relative_literal(node.args[0]))
        ):
            target = node.args[0]
        return None if target is None else self.mentions(target, listing, env)

    def checks(self, expr: ast.AST, env: Env, listing: frozenset[str]) -> list[bool]:
        """One entry per repo-path existence check in ``expr``: True if listing-derived.

        Existence calls count anywhere in ``expr``; a name or a helper call counts
        only when what it is bound to (or returns) is itself a check, so a name
        bound to ``data if p.exists() else {}`` is data, not a check (the
        conditional expression is reported where it is written)."""
        out: list[bool] = []
        for node in ast.walk(expr):
            direct = self._direct_check(node, env, listing)
            if direct is not None:
                out.append(direct)
            out.extend(self._indirect(node, env, listing))
        return out

    def _indirect(self, node: ast.AST, env: Env, listing: frozenset[str]) -> list[bool]:
        if isinstance(node, ast.Call):
            fn = self._helper(node)
            if fn is not None and self._helper_returns_a_check(fn):
                return [False]
            return []
        key = self._resolve(node)
        return list(self._key_checks(key, env, listing)) if key is not None else []

    def shaped_checks(self, expr: ast.AST, env: Env, listing: frozenset[str]) -> list[bool]:
        """Checks ``expr`` *is* (``not``, ``and``/``or``, ``any``/``all``), not merely contains."""
        if isinstance(expr, ast.UnaryOp) and isinstance(expr.op, ast.Not):
            return self.shaped_checks(expr.operand, env, listing)
        if isinstance(expr, ast.BoolOp):
            return [c for v in expr.values for c in self.shaped_checks(v, env, listing)]
        if isinstance(expr, ast.Call) and _call_name(expr) in {"any", "all", "bool"}:
            return self.checks(expr, env, listing)
        direct = self._direct_check(expr, env, listing)
        if direct is not None:
            return [direct]
        return self._indirect(expr, env, listing)

    def _key_checks(self, key: str, env: Env, listing: frozenset[str]) -> tuple[bool, ...]:
        def compute() -> tuple[bool, ...]:
            found: list[bool] = []
            for value in env.get(key, []):
                found.extend(self.shaped_checks(value, env, listing))
            imported = self._imported(key)
            if imported is not None:
                other, name = imported
                found.extend(False for _ in other._key_checks(name, other.module_env, frozenset()))
            return tuple(found)

        return self._memo_get("checks", (key, id(env), listing), compute, ())

    def _helper_returns_a_check(self, fn: Func) -> bool:
        def compute() -> bool:
            env, listing = self.env_for(fn), self.listing_vars(fn)
            return any(
                isinstance(ret, ast.Return)
                and ret.value is not None
                and bool(self.shaped_checks(ret.value, env, listing))
                for ret in _walk_scope(fn.body)
            )

        return self._memo_get("helper", id(fn), compute, False)

    def _helper(self, call: ast.Call) -> Func | None:
        func = call.func
        if isinstance(func, ast.Name):
            return self.functions.get(func.id)
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in {"self", "cls"}
        ):
            return self.functions.get(func.attr)
        return None

    def mentions(self, expr: ast.AST, names: frozenset[str], env: Env) -> bool:
        if not names:
            return False
        for node in ast.walk(expr):
            key = self._resolve(node)
            if key is not None and (key in names or self._key_mentions(key, names, env)):
                return True
        return False

    def _key_mentions(self, key: str, names: frozenset[str], env: Env) -> bool:
        return self._memo_get(
            "mentions",
            (key, names, id(env)),
            lambda: any(self.mentions(v, names, env) for v in env.get(key, [])),
            False,
        )

    # --- listings -------------------------------------------------------------

    def is_listing(self, expr: ast.AST, env: Env, listing: frozenset[str]) -> bool:
        if isinstance(expr, (ast.List, ast.Tuple, ast.Set)) and all(
            isinstance(e, ast.Constant) for e in expr.elts
        ):
            return False
        for node in ast.walk(expr):
            if isinstance(node, ast.Call):
                name = _call_name(node)
                receiver = node.func.value if isinstance(node.func, ast.Attribute) else None
                if name in LISTING_CALLS and not isinstance(receiver, ast.Constant):
                    return True
                fn = self._helper(node)
                if fn is not None and any(
                    isinstance(n, ast.Call) and _call_name(n) in LISTING_CALLS
                    for n in _walk_scope(fn.body)
                ):
                    return True
        return self.mentions(expr, listing, env)

    def listing_vars(self, scope: ast.AST) -> frozenset[str]:
        if scope in self._listing:
            return self._listing[scope]
        env = self.env_for(scope)
        body = scope.body if isinstance(scope, (*DEFS, ast.Module)) else []
        loops = [
            n
            for n in _walk_scope(body)
            if isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension))
        ]
        names: frozenset[str] = frozenset()
        changed = True
        while changed:
            changed = False
            for loop in loops:
                targets = {k for n in ast.walk(loop.target) if (k := _key(n)) is not None}
                if not targets <= names and self.is_listing(loop.iter, env, names):
                    names |= targets
                    changed = True
        self._listing[scope] = names
        return names

    # --- outcomes -------------------------------------------------------------

    def _fails(self, stmts: list[ast.stmt], seen: frozenset[str] = frozenset()) -> bool:
        for stmt in stmts:
            if isinstance(stmt, ast.Raise):
                return True
            if isinstance(stmt, ast.Assert) and isinstance(stmt.test, ast.Constant):
                if not stmt.test.value:
                    return True
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                if _call_name(stmt.value) == "fail":
                    return True
                fn = self._helper(stmt.value)
                if fn is not None and fn.name not in seen:
                    if self._fails(fn.body, seen | {fn.name}):
                        return True
            if isinstance(stmt, ast.With) and self._fails(stmt.body, seen):
                return True
        return False

    def _records(self, stmts: list[ast.stmt], scope: ast.AST) -> bool:
        for stmt in stmts:
            name: str | None = None
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                func = stmt.value.func
                if isinstance(func, ast.Attribute) and func.attr in RECORD_METHODS:
                    name = _key(func.value)
            elif isinstance(stmt, ast.AugAssign):
                name = _key(stmt.target)
            elif isinstance(stmt, ast.Return) and not _is_empty_value(stmt.value):
                if isinstance(scope, DEFS) and self.result_asserted(scope):
                    return True
            if name is not None and self._name_asserted(name, scope):
                return True
        return False

    def _name_asserted(self, name: str, scope: ast.AST, seen: frozenset[str] = frozenset()) -> bool:
        body = scope.body if isinstance(scope, (*DEFS, ast.Module)) else []
        for node in _walk_scope(body):
            if isinstance(node, ast.Assert) and self.mentions(node.test, frozenset({name}), NO_ENV):
                return True
            if isinstance(node, ast.Return) and node.value is not None:
                if self.mentions(node.value, frozenset({name}), NO_ENV) and isinstance(scope, DEFS):
                    if self.result_asserted(scope, seen):
                        return True
        return False

    def result_asserted(self, fn: Func, seen: frozenset[str] = frozenset()) -> bool:
        """True if every call of ``fn`` in this module ends in an ``assert``."""
        if fn.name in seen:
            return False
        if fn.name in self._asserted and not seen:
            return self._asserted[fn.name]
        seen = seen | {fn.name}
        inside = {id(n) for n in ast.walk(fn)}
        calls = [
            n for n in self._calls.get(fn.name, []) if self._helper(n) is fn and id(n) not in inside
        ]
        ok = bool(calls) and all(self._call_asserted(c, seen) for c in calls)
        if len(seen) == 1:
            self._asserted[fn.name] = ok
        return ok

    def _call_asserted(self, call: ast.Call, seen: frozenset[str]) -> bool:
        cur: ast.AST = call
        while cur in self.parent:
            parent = self.parent[cur]
            if isinstance(parent, ast.Assert):
                return True
            if isinstance(parent, ast.stmt):
                scope = self.scope_of(parent)
                if isinstance(parent, ast.Return):
                    return isinstance(scope, DEFS) and self.result_asserted(scope, seen)
                target: ast.AST | None = None
                if isinstance(parent, ast.Assign) and len(parent.targets) == 1:
                    target = parent.targets[0]
                elif isinstance(parent, (ast.AugAssign, ast.AnnAssign)):
                    target = parent.target
                elif isinstance(parent, ast.Expr) and isinstance(parent.value, ast.Call):
                    func = parent.value.func
                    if isinstance(func, ast.Attribute) and func.attr in RECORD_METHODS:
                        target = func.value
                key = _key(target) if target is not None else None
                return key is not None and self._name_asserted(key, scope, seen)
            cur = parent
        return False

    # --- the rules -------------------------------------------------------------

    def violations(self) -> list[str]:
        found: list[str] = []

        def report(node: ast.AST, why: str) -> None:
            found.append(f"{self.path}:{getattr(node, 'lineno', 0)}: {why}")

        for node in ast.walk(self.tree):
            if not isinstance(node, (ast.If, ast.IfExp, ast.Try, ast.Call)):
                continue
            if isinstance(node, ast.Call) and _call_name(node) not in {"importorskip", *MARK_CALLS}:
                continue
            scope = self.scope_of(node)
            env = self.env_for(scope)
            listing = self.listing_vars(scope)
            if isinstance(node, ast.If):
                exempt = self.checks(node.test, env, listing)
                if not exempt:
                    continue
                accounted = any(
                    self._fails(b) or self._records(b, scope) for b in (node.body, node.orelse)
                )
                if accounted:
                    continue
                if all(exempt) and not self._returns_or_skips([*node.body, *node.orelse]):
                    continue
                report(node, "branches on whether a repo path exists")
            elif isinstance(node, ast.IfExp):
                exempt = self.checks(node.test, env, listing)
                if exempt and not all(exempt):
                    report(node, "picks a value on whether a repo path exists")
            elif isinstance(node, ast.Try):
                handlers = [h for h in node.handlers if self._catches_os_error(h)]
                if not handlers or not any(self.is_repo_path(s, env) for s in node.body):
                    continue
                for handler in handlers:
                    if not (self._fails(handler.body) or self._records(handler.body, scope)):
                        report(handler, "swallows a missing repo path")
            elif isinstance(node, ast.Call):
                name = _call_name(node)
                if name == "importorskip" and node.args:
                    arg = node.args[0]
                    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                        if arg.value.split(".")[0] in REPO_PACKAGES:
                            report(node, "importorskip of a repo-local module")
                if name in MARK_CALLS:
                    args = [*node.args, *(k.value for k in node.keywords)]
                    if any(not all(self.checks(a, env, listing) or [True]) for a in args):
                        report(node, f"{name} keyed on a repo path")
        return found

    def _returns_or_skips(self, stmts: list[ast.stmt]) -> bool:
        for node in _walk_scope(stmts):
            if isinstance(node, (ast.Return, ast.Break)):
                return True
            if isinstance(node, ast.Call) and _call_name(node) in SKIP_CALLS:
                return True
            if isinstance(node, ast.Call):
                fn = self._helper(node)
                if fn is not None and any(
                    isinstance(n, ast.Call) and _call_name(n) in SKIP_CALLS
                    for n in _walk_scope(fn.body)
                ):
                    return True
        return False

    @staticmethod
    def _catches_os_error(handler: ast.ExceptHandler) -> bool:
        kinds = handler.type
        if kinds is None:
            return True
        elts = kinds.elts if isinstance(kinds, ast.Tuple) else [kinds]
        names = {e.id if isinstance(e, ast.Name) else getattr(e, "attr", "") for e in elts}
        return bool(names & (OS_ERRORS | {"Exception", "BaseException"}))


_LOADED: dict[str, Module | None] = {}
_TEST_MODULE_FILES: dict[str, Path] = {}


def _load_test_module(module_name: str) -> Module | None:
    """A ``tests.*`` module, parsed once per session; repo-path names live there.

    Names imported from ``src``, ``scripts`` and the rest are not followed: test
    paths are defined in ``tests/``, and parsing the product code would cost more
    than the whole scan."""
    if not _TEST_MODULE_FILES:
        for path in TESTS_DIR.rglob("*.py"):
            parts = path.relative_to(REPO_ROOT).with_suffix("").parts
            dotted = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
            _TEST_MODULE_FILES[dotted] = path
    if module_name not in _LOADED:
        _LOADED[module_name] = None
        path = _TEST_MODULE_FILES.get(module_name)
        if path is not None:
            _LOADED[module_name] = Module(path.read_text(), str(path), _load_test_module)
    return _LOADED[module_name]


def find_violations(
    source: str, path: str = "<test>", modules: dict[str, str] | None = None
) -> list[str]:
    """Violations in ``source``; ``modules`` maps importable names to planted sources."""
    if modules is None:
        return Module(source, path, _load_test_module).violations()
    planted = {name: Module(src, name) for name, src in modules.items()}
    for module in planted.values():
        module.loader = planted.get
    return Module(source, path, planted.get).violations()


def _is_test_module(path: Path) -> bool:
    return (
        path.name.startswith("test_") or path.stem.endswith("_test") or path.name == "conftest.py"
    )


def _test_files() -> list[Path]:
    return sorted(
        p
        for p in TESTS_DIR.rglob("*.py")
        if _is_test_module(p) and "fixtures" not in p.relative_to(TESTS_DIR).parts
    )


def test_no_test_branches_on_whether_a_repo_path_exists() -> None:
    files = _test_files()
    assert len(files) > 100, "expected to scan the whole tests/ tree"
    found: list[str] = []
    for file in files:
        found += find_violations(file.read_text(), str(file.relative_to(REPO_ROOT)))
    assert not found, "Assert unconditionally; every CI environment runs the same tree:\n" + (
        "\n".join(found)
    )


def test_the_scan_covers_this_file_and_skips_fixture_folders() -> None:
    files = {p.resolve() for p in _test_files()}
    assert Path(__file__).resolve() in files
    assert (TESTS_DIR / "e2e" / "conftest.py").resolve() in files
    assert (TESTS_DIR / "e2e" / "routing_snapshot.py").resolve() not in files  # a generator
    assert not any("fixtures" in p.relative_to(TESTS_DIR.resolve()).parts for p in files)


# --- self-tests on planted sources ------------------------------------------------

WF = "from pathlib import Path\nWF = Path(__file__).resolve().parents[3] / '.github/workflows/ci.yml'\n"


def _one(src: str, modules: dict[str, str] | None = None) -> None:
    found = find_violations(src, modules=modules)
    assert len(found) == 1, (found, src)


def _none(src: str, modules: dict[str, str] | None = None) -> None:
    found = find_violations(src, modules=modules)
    assert found == [], (found, src)


def test_flags_an_early_return_on_a_missing_workflow() -> None:
    src = WF + "def test_x():\n    if not WF.exists():\n        return\n    assert WF.read_text()\n"
    assert find_violations(src) == ["<test>:4: branches on whether a repo path exists"]


def test_flags_asserts_wrapped_in_an_existence_if_with_no_else() -> None:
    _one(
        WF + "def test_x():\n    if WF.exists():\n        assert 'merge_group' in WF.read_text()\n"
    )
    _one(
        WF + "def test_x():\n    if WF.is_file():\n        assert WF.read_text()\n"
        "    else:\n        assert True\n"
    )


def test_flags_a_module_level_flag_even_when_the_branch_asserts() -> None:
    _one(
        WF + "SOURCE = WF.parent.is_dir()\n"
        "def test_x():\n    if not SOURCE:\n        assert not WF.exists()\n        return\n"
    )


def test_flags_a_flag_imported_from_another_module() -> None:
    paths = WF + "SOURCE = WF.parent.is_dir()\n"
    src = "from tests.paths import SOURCE\ndef test_x():\n    if not SOURCE:\n        return\n"
    _one(src, modules={"tests.paths": paths})
    _none(
        "from tests.paths import OTHER\ndef test_x():\n    if not OTHER:\n        return\n",
        modules={"tests.paths": "OTHER = 1\n"},
    )


def test_flags_helpers_that_return_any_value_on_a_missing_path() -> None:
    for value in ("", " False", " True", " None", " []", " 0"):
        _one(WF + f"def _ok():\n    if not WF.exists():\n        return{value}\n    return 1\n")


def test_flags_an_existence_check_hidden_in_a_helper() -> None:
    _one(
        WF + "def _has():\n    return WF.exists()\n"
        "def test_x():\n    if not _has():\n        return\n"
    )


def test_flags_a_conditional_expression_on_a_repo_path() -> None:
    _one(WF + "def test_x():\n    text = WF.read_text() if WF.exists() else ''\n    assert text\n")


def test_a_name_bound_to_a_conditional_value_is_data_reported_once() -> None:
    src = (
        WF + "def test_x():\n    text = WF.read_text() if WF.exists() else ''\n"
        "    if text:\n        assert 'on:' in text\n"
    )
    assert find_violations(src) == ["<test>:4: picks a value on whether a repo path exists"]


def test_flags_a_swallowed_missing_file() -> None:
    for kind in ("FileNotFoundError", "OSError", "(OSError, ValueError)", "Exception"):
        _one(
            WF + f"def test_x():\n    try:\n        text = WF.read_text()\n"
            f"    except {kind}:\n        return\n    assert text\n"
        )


def test_flags_skips_xfails_and_skips_through_a_helper() -> None:
    for call in ("pytest.skip('x')", "pytest.xfail('x')", "_skip()"):
        _one(
            "import pytest\n" + WF + "def _skip():\n    pytest.skip('no tree')\n"
            f"def test_x():\n    if not WF.exists():\n        {call}\n"
        )


def test_flags_marks_keyed_on_a_repo_path() -> None:
    for mark in (
        "pytest.mark.skipif(not WF.exists(), reason='x')",
        "pytest.mark.skipif(condition=not WF.exists(), reason='x')",
        "pytest.mark.xfail(not WF.exists(), reason='x')",
        "unittest.skipUnless(WF.exists(), 'x')",
        "unittest.skipIf(not WF.is_file(), 'x')",
    ):
        _one(f"import pytest, unittest\n{WF}@{mark}\ndef test_x():\n    assert WF.read_text()\n")
    _one(
        "import pytest\n" + WF + "@pytest.mark.parametrize('x', [pytest.param(1, "
        "marks=pytest.mark.skip if not WF.exists() else ())])\ndef test_x(x):\n    assert x\n"
    )


def test_flags_importorskip_of_a_repo_module_only() -> None:
    _one("import pytest\nmod = pytest.importorskip('src.agents.referee')\n")
    _one("import pytest\nmod = pytest.importorskip('tests.paths')\n")
    _none("import pytest\npw = pytest.importorskip('playwright')\n")


def test_recognises_every_spelling_of_a_repo_path() -> None:
    bodies = {
        "REPO from another module": "from tests.e2e.conftest import REPO\nP = REPO / 'a'\n",
        "Path.cwd()": "from pathlib import Path\nP = Path.cwd() / '.github'\n",
        "os.getcwd()": "import os\nP = os.path.join(os.getcwd(), '.github')\n",
        "a relative literal": "from pathlib import Path\nP = Path('.github/workflows')\n",
        "REPO_DIR": "from x import REPO_DIR\nP = REPO_DIR / 'a'\n",
        "a name defined elsewhere": "from tests.paths import DOCS\nP = DOCS / 'a'\n",
    }
    modules = {"tests.paths": WF + "DOCS = WF.parents[2] / 'docs'\n"}
    for why, head in bodies.items():
        src = head + "def test_x():\n    if not P.exists():\n        return\n"
        assert len(find_violations(src, modules=modules)) == 1, why
    for spelling in (
        "import os\ndef test_x():\n    if not os.path.isdir('docs'):\n        return\n",
        "from os import path\ndef test_x():\n    if not path.exists('docs'):\n        return\n",
        "import os.path as osp\ndef test_x():\n    if not osp.isfile('ci/x'):\n        return\n",
        "from os.path import exists\ndef test_x():\n    if not exists('docs'):\n        return\n",
    ):
        _one(spelling)


def test_recognises_repo_paths_on_self() -> None:
    _one(
        "from pathlib import Path\nclass TestX:\n    root = Path(__file__).parents[2]\n"
        "    def test_x(self):\n        if not (self.root / 'x').exists():\n            return\n"
    )
    _one(
        "from pathlib import Path\nclass TestX:\n    def setup_method(self):\n"
        "        self.base = Path(__file__).parent\n"
        "    def test_x(self):\n        if not (self.base / 'x').exists():\n            return\n"
    )
    _one(
        "class TestX:\n    def test_x(self):\n"
        "        if not (self.repo_root / 'x').exists():\n            return\n"
    )


def test_a_literal_list_is_not_a_listing() -> None:
    for loop in ("['.github/workflows/ci.yml']", "('a', 'b')", "sorted(['a'])"):
        _one(
            "from tests.paths import REPO_ROOT\ndef test_x():\n"
            f"    for rel in {loop}:\n        if not (REPO_ROOT / rel).exists():\n"
            "            continue\n        assert rel\n"
        )


def test_a_return_or_skip_in_a_listing_loop_is_not_exempt() -> None:
    for escape in ("return", "pytest.skip('x')", "break"):
        _one(
            "import pytest\nfrom tests.paths import REPO_ROOT\ndef test_x():\n"
            "    for p in REPO_ROOT.glob('*.yml'):\n        if not p.is_file():\n"
            f"            {escape}\n        assert p.read_text()\n"
        )


def test_allows_filtering_a_listing_by_its_loop_variable() -> None:
    _none(
        "import subprocess\nfrom tests.paths import REPO_ROOT\n"
        "def _tracked():\n    out = subprocess.run(['git', 'ls-files'], capture_output=True)\n"
        "    return out.stdout.decode().split()\n"
        "def test_x():\n    for rel in _tracked():\n        path = REPO_ROOT / rel\n"
        "        if not path.is_file():\n            continue\n        assert path.read_bytes()\n"
    )
    _none(
        "from tests.paths import REPO_ROOT\ndef test_x():\n"
        "    for p in (REPO_ROOT / 'docs').rglob('*'):\n        if p.is_dir():\n"
        "            continue\n        assert p.read_text()\n"
    )
    _none(
        "import ast\nfrom tests.paths import REPO_ROOT\ndef _mods(tree):\n    out = set()\n"
        "    for node in ast.walk(tree):\n        for alias in node.names:\n"
        "            sub = f'{alias.name}.py'\n"
        "            out.add(sub if (REPO_ROOT / sub).exists() else 'x/__init__.py')\n"
        "    return out\n"
    )


def test_a_reporting_return_must_reach_an_assert() -> None:
    helper = (
        "from tests.paths import REPO_ROOT\ndef _problems(rel):\n"
        "    if not (REPO_ROOT / rel).is_file():\n        return [f'{rel} does not exist']\n"
        "    return []\n"
    )
    _none(helper + "def test_x():\n    assert _problems('a') == []\n")
    _none(helper + "def test_x():\n    problems = _problems('a')\n    assert not problems\n")
    _none(
        helper + "def _all(rels):\n    return [p for r in rels for p in _problems(r)]\n"
        "def test_x():\n    assert _all(['a']) == []\n"
    )
    _one(helper + "def test_x():\n    _problems('a')\n    assert True\n")
    _one(helper)  # never called: nothing reads the report


def test_allows_branches_that_fail_or_record_the_missing_path() -> None:
    for branch in (
        "raise AssertionError('missing')",
        "pytest.fail('missing')",
        "assert False, 'missing'",
        "_fail()",
    ):
        _none(
            "import pytest\n" + WF + "def _fail():\n    pytest.fail('x')\n"
            f"def test_x():\n    if not WF.exists():\n        {branch}\n    assert WF.read_text()\n"
        )
    _none(
        "def _missing(repo_root, cited):\n    missing = []\n    for p in sorted(cited):\n"
        "        if not (repo_root / p).exists():\n            missing.append(p)\n"
        "    return missing\n"
        "def test_x(tmp_path):\n    assert _missing(tmp_path, {'a'}) == ['a']\n"
    )
    _none(
        WF + "def test_x():\n    try:\n        text = WF.read_text()\n"
        "    except FileNotFoundError:\n        raise AssertionError('no workflow')\n    assert text\n"
    )


def test_allows_unconditional_asserts_data_branches_and_tmp_path_trees() -> None:
    _none(WF + "def test_x():\n    assert WF.exists()\n    assert WF.read_text()\n")
    _none(
        WF + "def test_x(reviewed):\n    if not reviewed:\n        assert not WF.exists()\n"
        "    else:\n        assert WF.read_text()\n"
    )
    _none(
        "def _files(root):\n    if not root.exists():\n        return []\n"
        "    return list(root.rglob('*'))\n"
        "def test_x(tmp_path):\n    target = tmp_path / 'out'\n"
        "    if not target.exists():\n        return\n"
    )
    _none("import pytest\n@pytest.mark.skip(reason='flaky')\ndef test_x():\n    assert True\n")


def test_a_nested_function_does_not_leak_its_escape_to_the_outer_if() -> None:
    _none(
        WF
        + "def test_x():\n    if not WF.exists():\n        def inner():\n            return None\n"
        "        raise AssertionError(inner())\n"
    )
