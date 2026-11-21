# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Nothing starts PostgreSQL through ``pgserver.get_server`` (#2522).

``get_server`` and pgserver's ``PostgresServer`` take one lock shared by every process
of the user and wait on it with no time limit while ``initdb`` and ``pg_ctl`` run, so
parallel test runs queued on it until pytest-timeout took a worker down (#2518). The
product starts pgserver's binaries through the disposable engine, and tests through
``tests/pg_server.py`` on top of it. This guard reads every Python file under
``src/``, ``scripts/`` and ``tests/`` and fails on any reference to ``get_server``
reached from pgserver, and on any call of ``PostgresServer``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ROOTS = ("src", "scripts", "tests")
_FORBIDDEN = {"get_server", "PostgresServer"}
_SKIP_DIRS = {"node_modules", "build", "__pycache__", ".venv", "dist", "coverage"}


def _is_pgserver(module: str) -> bool:
    return module == "pgserver" or module.startswith("pgserver.")


def _imports_pgserver(value: ast.expr) -> bool:
    """``pytest.importorskip("pgserver")`` or ``importlib.import_module("pgserver...")``."""
    if not isinstance(value, ast.Call) or not value.args:
        return False
    func = value.func
    name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
    first = value.args[0]
    return (
        name in {"importorskip", "import_module", "__import__"}
        and isinstance(first, ast.Constant)
        and isinstance(first.value, str)
        and _is_pgserver(first.value)
    )


def _reaches(node: ast.expr, aliases: set[str]) -> bool:
    """Whether ``node`` is pgserver or one of its modules: a name bound to one, an
    import call (``pytest.importorskip("pgserver")``), or an attribute of either."""
    while isinstance(node, ast.Attribute):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id in aliases
    return _imports_pgserver(node)


def findings(source: str) -> list[str]:
    """Each place ``source`` reaches pgserver's lock-taking start, as ``line: text``."""
    tree = ast.parse(source)
    aliases: set[str] = set()  # names bound to pgserver or one of its modules
    direct: set[str] = set()  # names bound to get_server or PostgresServer themselves
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if _is_pgserver(alias.name):
                    aliases.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module and _is_pgserver(node.module):
            for alias in node.names:
                if alias.name in {"get_server", "*"}:
                    found.append((node.lineno, f"imports {alias.name} from {node.module}"))
                if alias.name in _FORBIDDEN:
                    direct.add(alias.asname or alias.name)
                else:
                    aliases.add(alias.asname or alias.name)
    # ``ps = pgserver.postgres_server`` and ``P = ps.PostgresServer`` bind new names:
    # follow assignments until no name is added.
    assigns = [n for n in ast.walk(tree) if isinstance(n, ast.Assign)]
    while True:
        before = len(aliases) + len(direct)
        for node in assigns:
            names = {t.id for t in node.targets if isinstance(t, ast.Name)}
            value = node.value
            if isinstance(value, ast.Attribute) and value.attr in _FORBIDDEN:
                if _reaches(value.value, aliases):
                    direct.update(names)
            elif _reaches(value, aliases):
                aliases.update(names)
        if len(aliases) + len(direct) == before:
            break
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN:
            if node.attr == "PostgresServer":
                continue  # reading PostgresServer.lock_path is harmless; its call is below
            if _reaches(node.value, aliases):
                found.append((node.lineno, f"uses {ast.unparse(node)}"))
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in direct:
                found.append((node.lineno, f"calls {func.id}"))
            elif (
                isinstance(func, ast.Attribute)
                and func.attr in _FORBIDDEN
                and _reaches(func.value, aliases)
            ):
                found.append((node.lineno, f"calls {ast.unparse(func)}"))
            elif (
                isinstance(func, ast.Name)
                and func.id == "getattr"
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in _FORBIDDEN
                and _reaches(node.args[0], aliases)
            ):
                found.append((node.lineno, f"getattr of {node.args[1].value}"))
    return [f"{line}: {text}" for line, text in sorted(set(found))]


def _python_files() -> list[Path]:
    return [
        path
        for root in ROOTS
        for path in sorted((REPO / root).rglob("*.py"))
        if not _SKIP_DIRS.intersection(path.relative_to(REPO).parts)
    ]


@pytest.mark.passes_on_main
def test_no_file_starts_postgresql_through_pgserver_get_server():
    files = _python_files()
    assert any(p.name == "pg_server.py" for p in files)  # the scan reaches tests/
    assert any(p.name == "disposable_engine.py" for p in files)  # and src/
    bad = {
        str(path.relative_to(REPO)): hits
        for path in files
        if (hits := findings(path.read_text(encoding="utf-8")))
    }
    assert not bad, (
        "pgserver.get_server waits on a user-wide lock with no time limit (#2518); "
        f"start a test PostgreSQL with tests.pg_server.start_pg_server instead: {bad}"
    )


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "source",
    [
        'import pgserver\nsrv = pgserver.get_server(path, cleanup_mode="delete")\n',
        'import pytest\npgserver = pytest.importorskip("pgserver")\npgserver.get_server(p)\n',
        "def f():\n    pg = __import__('pgserver')\n    return pg.get_server(p)\n",
        "import pgserver as pg\nstart = pg.get_server\n",
        "from pgserver import get_server\nget_server(p)\n",
        "from pgserver import get_server as gs\ngs(p)\n",
        "from pgserver.postgres_server import PostgresServer\nPostgresServer(p)\n",
        "import pgserver.postgres_server as ps\nps.get_server(p)\n",
        "from pgserver import postgres_server\npostgres_server.PostgresServer(p)\n",
        "import pgserver\nstart = getattr(pgserver, 'get_server')\n",
        "from pgserver import *\n",
        "import pgserver\nps = pgserver.postgres_server\nps.PostgresServer(p)\n",
        "import pgserver\nps = pgserver.postgres_server\nP = ps.PostgresServer\nP(p)\n",
        "import pgserver as a\nb = a\nb.get_server(p)\n",
        'import pytest\npytest.importorskip("pgserver").get_server(p)\n',
        'import importlib\nimportlib.import_module("pgserver.postgres_server").PostgresServer(p)\n',
    ],
    ids=lambda s: s.splitlines()[-1][:40],
)
def test_the_rule_catches_each_way_to_reach_get_server(source):
    assert findings(source), source


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "source",
    [
        "from tests.pg_server import start_pg_server\nsrv = start_pg_server()\n",
        "import pgserver\nmonkeypatch.setattr(pgserver, 'get_server', refuse)\n",
        "# pgserver.get_server is not used\nx = 'pgserver.get_server(p)'\n",
        "from pgserver.postgres_server import PostgresServer\nlock = PostgresServer.lock_path\n",
        "import pgserver\nlock = pgserver.postgres_server.PostgresServer.lock_path\n",
        "import pgserver._commands as commands\nbin_dir = commands.POSTGRES_BIN_PATH\n",
        "import pgserver\nps = pgserver.postgres_server\nlock = ps.PostgresServer.lock_path\n",
        "import pytest\nbin_dir = pytest.importorskip('pgserver').__file__\n",
        "class Other:\n    def get_server(self):\n        return 1\nOther().get_server()\n",
    ],
)
def test_the_rule_leaves_other_code_alone(source):
    assert not findings(source), source
