"""src/report must not depend on the AWS Transform integration (it outlives it)."""

from __future__ import annotations

import ast
from pathlib import Path

REPORT = Path(__file__).resolve().parents[3] / "src" / "report"


def test_report_package_never_imports_atx_orchestrator() -> None:
    offenders = []
    for py in REPORT.rglob("*.py"):
        for node in ast.walk(ast.parse(py.read_text())):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith(
                "src.atx_orchestrator"
            ):
                offenders.append(f"{py.name}:{node.lineno}")
            if isinstance(node, ast.Import) and any(
                a.name.startswith("src.atx_orchestrator") for a in node.names
            ):
                offenders.append(f"{py.name}:{node.lineno}")
    assert offenders == []
