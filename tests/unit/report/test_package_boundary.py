"""src/report must not depend on the AWS Transform integration (it outlives it)."""

from __future__ import annotations

import ast
from pathlib import Path

REPORT = Path(__file__).resolve().parents[3] / "src" / "report"
# The repo root -- the directory containing "src".
_REPO_ROOT = REPORT.parent.parent


def _resolve_relative_module(py: Path, level: int, module: str | None) -> str:
    """Resolve a relative ``ImportFrom`` node to its absolute dotted module path.

    For a file at ``src/report/<sub...>/x.py`` the package is ``src.report[.<sub>]``.
    ``level`` dots then walk up ``level - 1`` additional packages from there before
    appending ``module`` (if any) -- the standard relative-import resolution rule.
    """
    rel_parts = py.relative_to(_REPO_ROOT).with_suffix("").parts
    package_parts = list(rel_parts[:-1])  # drop the module's own filename
    up = level - 1
    if up > 0:
        package_parts = package_parts[:-up] if up <= len(package_parts) else []
    resolved = ".".join(package_parts)
    if module:
        resolved = f"{resolved}.{module}" if resolved else module
    return resolved


def _offenders_in(py: Path) -> list[str]:
    offenders = []
    for node in ast.walk(ast.parse(py.read_text())):
        if isinstance(node, ast.ImportFrom):
            if node.level > 0:
                resolved = _resolve_relative_module(py, node.level, node.module)
            else:
                resolved = node.module or ""
            if resolved.startswith("src.atx_orchestrator"):
                offenders.append(f"{py.name}:{node.lineno}")
        if isinstance(node, ast.Import) and any(
            a.name.startswith("src.atx_orchestrator") for a in node.names
        ):
            offenders.append(f"{py.name}:{node.lineno}")
    return offenders


def test_report_package_never_imports_atx_orchestrator() -> None:
    offenders = []
    for py in REPORT.rglob("*.py"):
        offenders.extend(_offenders_in(py))
    assert offenders == []


def test_relative_import_resolver_flags_an_atx_escape() -> None:
    """Self-check: the resolver must catch a relative import that escapes the package.

    This does not write a file into ``src/`` -- it unit-tests the resolver directly
    against a synthetic source string, parsed as if it lived at
    ``src/report/analysis_report.py``.
    """
    source = "from ..atx_orchestrator import tools\n"
    node = next(n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ImportFrom))
    synthetic_file = REPORT / "analysis_report.py"

    resolved = _resolve_relative_module(synthetic_file, node.level, node.module)

    assert resolved.startswith("src.atx_orchestrator")
