"""Every reader of a reality-check output takes the lineage's record (#193, #215).

A completed run is kept under ``reality-check/v<N>/output.json`` and
``reality-check/output.json`` holds only the latest run, which can belong to
another assignment lineage. So code that reads a run's content must go through
``src.storage.assignment_versioning`` (``read_reality_check_output`` or
``reality_check_output_candidates``), which picks the record of the run the
assignment descends from. This guard finds every place in ``src/`` and
``scripts/`` that spells the flat path out (a string or f-string ending in
``reality-check/output.json``, or the ``"reality-check", "output.json"`` pair a
call passes), outside docstrings, and fails on any not listed in ``ALLOWED``
with the reason it is not a content read of an earlier run.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SCAN_ROOTS = ("src", "scripts")
SUFFIX = "reality-check/output.json"

# (file, enclosing function) -> why this use of the flat path is not a content read
# that must follow the lineage.
ALLOWED: dict[tuple[str, str], str] = {
    ("src/api/services/local_execution.py", "LocalExecutionService.get_execution_history"): (
        "lists the file as the reality-check stage's output: only whether a run wrote it"
    ),
    ("src/api/services/local_execution.py", "LocalExecutionService._pre_192_output"): (
        "reads the output next to the awaiting_llm marker, which is the latest run by "
        "definition, only to tell a pre-#192 output from a preview (#193)"
    ),
    ("src/orchestrator/local_orchestrator.py", "LocalOrchestrator._infer_progression"): (
        "the phase's expected output file, checked for existence only"
    ),
    ("scripts/run_reality_check.py", "run_finalize"): (
        "reports the executive_summary_source of the run this call just wrote, which is "
        "the latest run by definition"
    ),
}


def _docstrings(tree: ast.AST) -> set[int]:
    ids: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                ids.add(id(body[0].value))
    return ids


def _uses(path: Path) -> list[tuple[str, int]]:
    """(enclosing function qualname, line) of each spelled-out flat path in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docs = _docstrings(tree)
    found: list[tuple[str, int]] = []

    def visit(node: ast.AST, scope: list[str]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = [*scope, node.name]
        name = ".".join(scope) or "<module>"
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            if node.value.endswith(SUFFIX):
                found.append((name, node.lineno))
        elif isinstance(node, ast.JoinedStr):
            tail = node.values[-1] if node.values else None
            if isinstance(tail, ast.Constant) and str(tail.value).endswith(SUFFIX):
                found.append((name, node.lineno))
            return  # its parts are not separate uses
        elif isinstance(node, ast.Call):
            literals = [a.value for a in node.args if isinstance(a, ast.Constant)]
            for a, b in zip(literals, literals[1:], strict=False):
                if (a, b) == ("reality-check", "output.json"):
                    found.append((name, node.lineno))
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree, [])
    return found


def _all_uses() -> dict[tuple[str, str], list[int]]:
    uses: dict[tuple[str, str], list[int]] = {}
    for root in SCAN_ROOTS:
        for path in sorted((REPO / root).rglob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            for qualname, line in _uses(path):
                uses.setdefault((rel, qualname), []).append(line)
    return uses


USES = _all_uses()


def test_no_reader_spells_out_the_flat_output_path() -> None:
    offenders = {k: v for k, v in USES.items() if k not in ALLOWED}
    assert not offenders, (
        "These read reality-check/output.json directly, so they can show a run from "
        "another lineage (#215) or miss the one the assignment descends from (#193). "
        "Use read_reality_check_output / reality_check_output_candidates, or list the "
        "use in ALLOWED with the reason:\n"
        + "\n".join(f"  {f}:{lines} ({q})" for (f, q), lines in sorted(offenders.items()))
    )


def test_no_stale_entries() -> None:
    assert not set(ALLOWED) - set(USES)


@pytest.mark.passes_on_main  # the scanner's own check: it is new with this file
def test_the_scan_finds_each_spelling(tmp_path: Path) -> None:
    sample = tmp_path / "sample.py"
    sample.write_text(
        '"""Mentions reality-check/output.json in a docstring."""\n'
        "def a(store, db, job):\n"
        '    return store.read_json(f"{db}/{job}/reality-check/output.json")\n'
        "def b(svc, db, job):\n"
        '    return svc.read_artifact(db, job, "reality-check", "output.json")\n'
        "def c():\n"
        '    return "x/reality-check/output.json"\n'
        "def d():\n"
        '    """reality-check/output.json"""\n'
    )
    assert [q for q, _ in _uses(sample)] == ["a", "b", "c"]
