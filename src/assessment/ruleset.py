"""Which ruleset produced a result: ``ruleset_version`` and ``ruleset_hash``.

A ruleset is not a configuration file: it is the rule catalog, the routing scorer and
the passes around it, the analyses and the SQL parser together. One library version
runs exactly one ruleset, so ``ruleset_version`` is the library version, and
``ruleset_hash`` is a content hash of everything that can change a statement's facts,
scores or engine:

- every file under ``src/catalog/`` (engines, patterns, gates, gaps, routing thresholds);
- every ``src`` module the phases ``assess()`` runs can import: the import closure of
  :data:`RULESET_ROOTS` (the parser and ingest, triage, every analysis, the scorer, the
  assignment and the reality check), read statically from each module's ``import``
  statements, those inside functions included, so a module the scorer or an analysis
  imports is always covered without being listed;
- the installed SQL parser library's version (``sqlglot``).

The set is deliberately broad: a change there that moves no statement still changes
the hash, but a change that moves one never leaves it the same. Two results with the
same ``ruleset_hash`` were produced by the same rules; scores (``statements[].fit``)
compare only within one hash. Asking for an older ruleset is not supported: pin the
library version instead.
"""

from __future__ import annotations

import ast
import hashlib
import importlib.metadata
from functools import lru_cache
from pathlib import Path

from src.shared.package_version import get_package_version

_SRC = Path(__file__).resolve().parents[1]

#: The modules whose import closure is the ruleset's code: the entry points of the
#: phases ``assess()`` runs (paths under ``src/``).
RULESET_ROOTS: tuple[str, ...] = (
    "tools/database/offline_parser.py",
    "agents/collector/offline_ingest.py",
    "agents/referee/triage_handler.py",
    "agents/analysis/aurora_mysql_analysis_agent.py",
    "agents/analysis/aurora_pg_analysis_agent.py",
    "agents/analysis/documentdb_analysis_agent.py",
    "agents/analysis/dynamodb_analysis_agent.py",
    "agents/analysis/elasticache_analysis_agent.py",
    "agents/analysis/opensearch_analysis_agent.py",
    "agents/referee/query_scoring.py",
    "agents/referee/assignment_handler.py",
    "agents/referee/reality_check_handler.py",
)

#: (directory under ``src/``, glob) pairs of data files that make up the ruleset.
RULESET_DATA: tuple[tuple[str, str], ...] = (("catalog", "**/*.toml"),)

#: Third-party libraries whose version is part of the ruleset.
RULESET_LIBRARIES: tuple[str, ...] = ("sqlglot",)


def _module_file(name: str) -> Path | None:
    """The file of ``src.a.b`` (``a/b.py`` or ``a/b/__init__.py``), or None."""
    parts = name.split(".")[1:]
    if not parts:
        return None
    base = _SRC.joinpath(*parts)
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _module_name(path: Path) -> str:
    """``src.a.b`` for ``src/a/b.py``, ``src.a`` for ``src/a/__init__.py``."""
    parts = list(path.relative_to(_SRC.parent).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _src_imports(path: Path) -> set[str]:
    """Every ``src.*`` module name ``path`` imports, anywhere in the file, relative
    imports (``from .x import y``) resolved against its package."""
    names: set[str] = set()
    package = _module_name(path).split(".")
    if path.name != "__init__.py":
        package = package[:-1]
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[: len(package) - (node.level - 1)]
                module = ".".join(base + ([node.module] if node.module else []))
            else:
                module = node.module or ""
            if not module:
                continue
            names.add(module)
            # ``from src.pkg import module`` names a module too.
            names.update(f"{module}.{a.name}" for a in node.names)
    return {n for n in names if n == "src" or n.startswith("src.")}


def import_closure(roots: tuple[str, ...] | None = None) -> set[Path]:
    """The root files (default :data:`RULESET_ROOTS`) and every ``src`` file they
    import, transitively.

    The package ``__init__.py`` files on the way are included, as Python runs them.
    """
    todo = []
    for root in RULESET_ROOTS if roots is None else roots:
        path = _SRC / root
        if not path.is_file():
            raise FileNotFoundError(f"ruleset root {root} is not a file under src/")
        todo.append(path)
    seen: set[Path] = set()
    while todo:
        path = todo.pop()
        if path in seen:
            continue
        seen.add(path)
        for parent in path.parents:
            init = parent / "__init__.py"
            if parent == _SRC.parent:
                break
            if init.is_file() and init not in seen:
                todo.append(init)
        for name in _src_imports(path):
            target = _module_file(name)
            if target is not None and target not in seen:
                todo.append(target)
    return seen


def ruleset_files() -> list[Path]:
    """Every file the hash covers, sorted by its path under ``src/``."""
    files = import_closure()
    for directory, pattern in RULESET_DATA:
        files.update(p for p in (_SRC / directory).glob(pattern) if p.is_file())
    return sorted(files, key=lambda p: p.relative_to(_SRC).as_posix())


def _library_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "absent"


@lru_cache(maxsize=1)
def ruleset_hash() -> str:
    """``sha256:<hex>`` over the ruleset files (path and bytes) and library versions."""
    digest = hashlib.sha256()
    for path in ruleset_files():
        digest.update(path.relative_to(_SRC).as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    for name in RULESET_LIBRARIES:
        digest.update(f"{name}=={_library_version(name)}".encode())
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def ruleset_version() -> str:
    """The ruleset's version: the library release that carries it."""
    return get_package_version()
