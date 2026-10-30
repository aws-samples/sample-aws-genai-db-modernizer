# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Writers take ``contract_version`` from the model, never a literal (ADR-008 Decision 2, #974).

A writer that passes ``contract_version="2.1"`` itself keeps writing ``2.1``
after a MINOR bump of the model's default, so the file claims a version it
was not written at. The analysis agents did this in 7 places. ADR-008
Decision 2: a writer takes the contract version from the model's default.

This AST guard scans ``src/`` (except ``src/contracts/``, where the defaults
live) and ``scripts/`` for a version written as a string literal:

- a keyword argument ``contract_version="X.Y"`` (``Model(contract_version=...)``),
- a dict entry ``"contract_version": "X.Y"`` (a contract built as a dict),
- an assignment ``x.contract_version = "X.Y"`` or ``x["contract_version"] = "X.Y"``.

Tests and fixtures are not scanned: they spell versions out on purpose (an
old file, a refused MAJOR).
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("src", "scripts")
EXEMPT_DIRS = (REPO / "src" / "contracts",)
FIELD = "contract_version"


def _is_literal(node: ast.expr | None) -> bool:
    return isinstance(node, ast.JoinedStr) or (
        isinstance(node, ast.Constant) and isinstance(node.value, str)
    )


def _is_field_target(target: ast.expr) -> bool:
    if isinstance(target, ast.Attribute):
        return target.attr == FIELD
    if isinstance(target, ast.Subscript):
        return isinstance(target.slice, ast.Constant) and target.slice.value == FIELD
    return False


def literal_versions(tree: ast.AST) -> list[tuple[int, str]]:
    """``(line, source)`` of every ``contract_version`` written as a literal in ``tree``."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg == FIELD and _is_literal(kw.value):
                    found.append((kw.value.lineno, f"{FIELD}={ast.unparse(kw.value)}"))
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if isinstance(key, ast.Constant) and key.value == FIELD and _is_literal(value):
                    found.append((key.lineno, f"{ast.unparse(key)}: {ast.unparse(value)}"))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if _is_literal(node.value) and any(_is_field_target(t) for t in targets):
                found.append((node.lineno, ast.unparse(node)))
    return sorted(found)


def _scanned_files() -> list[Path]:
    files: list[Path] = []
    for root in SCAN_ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            if "node_modules" in path.parts or any(path.is_relative_to(d) for d in EXEMPT_DIRS):
                continue
            files.append(path)
    return files


def test_no_writer_passes_a_contract_version_literal() -> None:
    offenders = [
        f"{path.relative_to(REPO)}:{line}: {source}"
        for path in _scanned_files()
        for line, source in literal_versions(ast.parse(path.read_text(), filename=str(path)))
    ]
    assert not offenders, (
        "These writers spell contract_version out instead of taking it from the model "
        "(ADR-008 Decision 2). Drop the argument so the model's default applies, or read "
        'it from the model (Model.model_fields["contract_version"].default):\n'
        + "\n".join(offenders)
    )


def test_the_scan_covers_the_writers() -> None:
    """Not vacuous: the analysis agents and the Aurora delta merge are scanned."""
    scanned = {str(p.relative_to(REPO)) for p in _scanned_files()}
    assert "src/agents/analysis/handler.py" in scanned
    assert "src/tools/schema/aurora_common/delta_merge.py" in scanned
    assert not any(p.startswith("src/contracts/") for p in scanned)


def _found(source: str) -> list[str]:
    return [s for _, s in literal_versions(ast.parse(source))]


def test_it_catches_each_literal_form() -> None:
    assert _found('Out(contract_version="2.1")') == ["contract_version='2.1'"]
    assert _found('x = {"contract_version": "1.0", "job_id": j}') == ["'contract_version': '1.0'"]
    assert _found('out.contract_version = "1.0"') == ["out.contract_version = '1.0'"]
    assert _found('out["contract_version"] = "1.0"') == ["out['contract_version'] = '1.0'"]
    assert _found('Out(contract_version=f"{major}.0")') == ["contract_version=f'{major}.0'"]


def test_it_allows_a_version_taken_from_the_model_or_the_input() -> None:
    assert not _found("Out(job_id=j)")
    assert not _found('x = {"contract_version": Out.model_fields["contract_version"].default}')
    assert not _found("Agent(contract_version=collector_output.contract_version)")
    assert not _found('v = data.get("contract_version")')
    assert not _found('if out["contract_version"] == "1.0": pass')
