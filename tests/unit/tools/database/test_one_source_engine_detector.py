"""One source-engine detector for every ingest path (#1177, #381).

``offline_parser.detect_source_engine`` reads an offline collection's source engine
from its metadata (``metadata.engine`` first, then the version banner). Before
#1177 the library ingest (``core.ingest_offline_collection``) kept its own inline
check -- "postgres" in the version string, else MySQL -- and the collector handler
assumed MySQL when ``ENGINE`` was unset, so SQL Server, Oracle and DB2 collections
were recorded as MySQL on those paths.

The guard scans the AST of every module under ``src/`` and ``scripts/`` and fails on
any of these shapes outside the detector itself:

1. ``engine_substring`` -- ``"postgres" in x`` (or ``not in``), or
   ``x.startswith("postgres")``/``endswith``, for any engine keyword: an inline
   guess at the engine from a string.
2. ``engine_env_default`` -- ``os.environ.get("ENGINE", default)`` or
   ``os.getenv("ENGINE", default)`` with a non-empty default: a guess when the
   operator named no engine.
3. ``offline_input_literal`` -- a dict literal with ``"mode": "offline"`` and an
   ``"engine"`` key: an offline collector input built outside the shared builder
   (``src/agents/collector/offline_ingest.py``), which is where the detector's
   answer goes in.
4. ``offline_builder`` -- a function that parses an offline collection
   (``parse_offline_collection``) and builds the output itself (``_build_tables``,
   ``_build_queries`` or ``_build_output``) instead of calling the shared builder:
   the shape ``mysql_collector._collect_offline`` had before #1177's review.

Allowed, each with its reason (and each entry must still match something, so the
list cannot go stale): the detector, the version-label formatter next to it, the
live MariaDB check on a connected server's own banner, the MariaDB flavour of a
source already recorded as MySQL, the live PostgreSQL collector shortening its
server's own banner, and the collector input contract's documented
example. Not caught statically: a keyword built at run time, ``re.search`` on an
engine pattern, or a lookup table keyed on banner text.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from src.tools.database.offline_parser import default_port, detect_source_engine

_REPO = Path(__file__).resolve().parents[4]
_SCANNED_DIRS = (_REPO / "src", _REPO / "scripts")
_KEYWORDS = ("postgres", "mysql", "maria", "sql server", "sqlserver", "oracle", "db2")

# (file relative to the repo, enclosing function or "<module>", shape) -> reason
_ALLOWED: dict[tuple[str, str, str], str] = {
    (
        "src/tools/database/offline_parser.py",
        "recognised_source_engine",
        "engine_substring",
    ): "the one detector",
    (
        "src/tools/database/offline_parser.py",
        "offline_version_label",
        "engine_substring",
    ): "formats a SQL Server banner as a short label; never sets the engine",
    (
        "src/tools/database/mariadb_tools.py",
        "_is_mariadb",
        "engine_substring",
    ): "a live, connected server's own version banner, read by the live collector",
    (
        "src/tools/analysis/relational_gaps.py",
        "detect_relational_gaps",
        "engine_substring",
    ): "the MariaDB flavour of a source already recorded as MySQL; never sets the engine",
    (
        "src/agents/collector/postgres_collector.py",
        "_collect_live",
        "engine_substring",
    ): "shortens a live PostgreSQL server's own banner to its version number",
    (
        "src/agents/collector/offline_ingest.py",
        "build_offline_collector_output",
        "offline_input_literal",
    ): "the shared offline builder, fed by the detector",
    (
        "src/contracts/collector_input.py",
        "<module>",
        "offline_input_literal",
    ): "the contract's documented JSON Schema example",
}


def _is_keyword(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(k in node.value.lower() for k in _KEYWORDS)
    )


_OUTPUT_BUILDERS = frozenset({"_build_tables", "_build_queries", "_build_output"})


def _findings(tree: ast.AST) -> list[tuple[str, str, int]]:
    """(enclosing function, shape, line) for every shape in one module."""
    out: list[tuple[str, str, int]] = []

    def visit(node: ast.AST, func: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            func = node.name
        if isinstance(node, ast.Compare) and _is_keyword(node.left):
            if any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops):
                out.append((func, "engine_substring", node.lineno))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            attr = node.func.attr
            if attr in ("startswith", "endswith") and any(_is_keyword(a) for a in node.args):
                out.append((func, "engine_substring", node.lineno))
            is_env_get = (attr == "get" and ast.unparse(node.func.value) == "os.environ") or (
                attr == "getenv" and ast.unparse(node.func.value) == "os"
            )
            default = node.args[1] if len(node.args) >= 2 else None
            if (
                is_env_get
                and default is not None
                and not (isinstance(default, ast.Constant) and default.value in ("", None))
                and isinstance(node.args[0], ast.Constant)
                and node.args[0].value == "ENGINE"
            ):
                out.append((func, "engine_env_default", node.lineno))
        if isinstance(node, ast.Dict):
            keys = {
                k.value: v
                for k, v in zip(node.keys, node.values, strict=True)
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
            mode = keys.get("mode")
            if (
                "engine" in keys
                and isinstance(mode, ast.Constant)
                and str(mode.value).lower() == "offline"
            ):
                out.append((func, "offline_input_literal", node.lineno))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            called = {
                (n.func.id if isinstance(n.func, ast.Name) else getattr(n.func, "attr", ""))
                for n in ast.walk(node)
                if isinstance(n, ast.Call)
            }
            if "parse_offline_collection" in called and called & _OUTPUT_BUILDERS:
                out.append((node.name, "offline_builder", node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child, func)

    visit(tree, "<module>")
    return out


def _scan() -> dict[tuple[str, str, str], list[int]]:
    found: dict[tuple[str, str, str], list[int]] = {}
    for root in _SCANNED_DIRS:
        for path in sorted(root.rglob("*.py")):
            if "node_modules" in path.parts:
                continue
            rel = path.relative_to(_REPO).as_posix()
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            for func, shape, line in _findings(tree):
                found.setdefault((rel, func, shape), []).append(line)
    return found


def test_no_ingest_path_guesses_the_source_engine_itself() -> None:
    found = _scan()
    offenders = {k: v for k, v in found.items() if k not in _ALLOWED}
    assert not offenders, (
        "Source engine guessed outside offline_parser.detect_source_engine (#1177): "
        + "; ".join(f"{f}:{lines} in {fn} ({shape})" for (f, fn, shape), lines in offenders.items())
        + ". Call detect_source_engine, or build the collector output with "
        "src.agents.collector.offline_ingest.build_offline_collector_output."
    )


def test_every_allowed_entry_still_matches() -> None:
    found = _scan()
    stale = [k for k in _ALLOWED if k not in found]
    assert not stale, f"allowlist entries that match nothing (remove them): {stale}"


@pytest.mark.parametrize(
    "source",
    [
        'def f(v):\n    return "postgres" in v.lower()\n',
        'def f(v):\n    return "Microsoft SQL Server" not in v\n',
        'def f(v):\n    return v.lower().startswith("oracle")\n',
        'import os\ndef f():\n    return os.environ.get("ENGINE", "mysql")\n',
        'import os\ndef f():\n    return os.getenv("ENGINE", "mysql")\n',
        'def f(e):\n    return {"engine": e, "mode": "offline"}\n',
        "def f(raw):\n    parsed = parse_offline_collection(raw)\n"
        "    return _build_output(inp, tables=_build_tables(parsed['tables'], 'db'))\n",
    ],
)
def test_the_scanner_catches_each_shape(source: str) -> None:
    assert _findings(ast.parse(source))


@pytest.mark.parametrize(
    "source",
    [
        'def f(v):\n    return "select" in v\n',
        'import os\ndef f():\n    return os.environ.get("ENGINE")\n',
        'import os\ndef f():\n    return os.environ.get("ENGINE", "")\n',
        'def f(e):\n    return {"engine": e, "mode": "live"}\n',
    ],
)
def test_the_scanner_ignores_what_is_not_a_guess(source: str) -> None:
    assert not _findings(ast.parse(source))


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        ({"engine": "sqlserver"}, ("sqlserver", 1433)),
        ({"engine": "SQLServer "}, ("sqlserver", 1433)),
        ({"engine": "oracle"}, ("oracle", 1521)),
        ({"engine": "db2"}, ("db2", 50000)),
        ({"engine": "mariadb"}, ("mariadb", 3306)),
        ({"engine": "postgres"}, ("postgresql", 5432)),
        ({"engine": "postgresql"}, ("postgresql", 5432)),
        ({"version": "PostgreSQL 16.10 on x86_64"}, ("postgresql", 5432)),
        ({"version_full": "Microsoft SQL Server 2019 (RTM)"}, ("sqlserver", 1433)),
        ({"version": "Oracle Database 19c Enterprise Edition"}, ("oracle", 1521)),
        ({"version": "DB2 v11.5.8.0"}, ("db2", 50000)),
        ({"version": "10.11.6-MariaDB"}, ("mariadb", 3306)),
        ({"version": "8.0.36"}, ("mysql", 3306)),
        ({"engine": "unknown", "version": "8.0.36"}, ("mysql", 3306)),
        ({}, ("mysql", 3306)),
        (None, ("mysql", 3306)),
    ],
)
def test_detect_source_engine(metadata: dict | None, expected: tuple[str, int]) -> None:
    assert detect_source_engine(metadata) == expected


def test_detected_engines_are_collector_input_engines() -> None:
    """Every engine the detector can return is one ``CollectorInput`` accepts; a
    ``"postgres"`` engine used to come back as is and then be refused."""
    from src.contracts.collector_input import DatabaseEngine

    accepted = {e.value for e in DatabaseEngine}
    for engine in ("mysql", "mariadb", "postgres", "postgresql", "sqlserver", "oracle", "db2"):
        detected, port = detect_source_engine({"engine": engine})
        assert detected in accepted
        assert default_port(engine) == port


def test_default_port_of_an_unknown_engine() -> None:
    assert default_port("cassandra") is None
    assert default_port("") is None
