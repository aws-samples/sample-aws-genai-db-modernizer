# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""No report, model input or API response reads the routine sources (#1652, ADR-034
Decision 4): only the collector modules that write them and the routine-migration phase
that checks them (#1934) import their modules, nothing outside ``routine_sources.py`` and
that phase reads a chunk, and only the writer and the contract registry name the files."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
_MODULES = {"src.agents.collector.routine_sources", "src.contracts.routine_sources"}
# Who may import those modules, and why: the collectors and ingests that build and write
# the routine sources (or each routine's body fields), the parser that marks each
# routine's body status, and the contract registry.
_IMPORTERS = {
    "src/agents/collector/handler.py",  # writes them (both modes) or clears them
    "src/agents/collector/mysql_collector.py",  # builds them, live
    "src/agents/collector/postgres_collector.py",  # builds them, live
    "src/agents/collector/sqlserver_collector.py",  # body fields, triggers outside DML
    "src/agents/collector/oracle_collector.py",  # body fields, triggers outside DML
    "src/agents/collector/offline_ingest.py",  # triggers outside the DML list (#2023)
    "src/agents/collector/routine_sources.py",  # the module itself
    "src/tools/database/offline_parser.py",  # body statuses, never a CREATE
    "scripts/run_collect.py",  # writes them
    "scripts/run_assessment.py",  # writes them
    "src/tools/routines/migration.py",  # reads them: the routine-migration phase (#1934)
    "src/tools/data_movement/routine_scripts.py",  # writes step 1's deploy script (#1947)
    "src/tools/data_movement/cutover_scripts.py",  # writes the cutover script (#1948)
    "src/tools/data_movement/cutover_check.py",  # reads an event's status only (#1948)
}
_READERS = {"read_routine_sources", "RoutineSourcesChunk"}
_PATHS = ("collector/routine_sources", "routine_sources.json", "routine_sources/part-")


def _python_files() -> list[Path]:
    return sorted([*(REPO / "src").rglob("*.py"), *(REPO / "scripts").rglob("*.py")])


def _imports(tree: ast.AST) -> list[tuple[str, set[str]]]:
    out: list[tuple[str, set[str]]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            out.append((node.module, {a.name for a in node.names}))
        elif isinstance(node, ast.Import):
            out.extend((a.name, set()) for a in node.names)
    return out


@pytest.mark.passes_on_main
def test_a_new_importer_or_reader_is_caught():
    tree = ast.parse("from src.agents.collector.routine_sources import read_routine_sources")
    assert any(m in _MODULES for m, _ in _imports(tree))
    assert {n for _, names in _imports(tree) for n in names} & _READERS


def test_only_the_collectors_that_write_the_routine_sources_import_them():
    importers = set()
    for path in _python_files():
        rel = str(path.relative_to(REPO))
        if any(module in _MODULES for module, _ in _imports(ast.parse(path.read_text()))):
            importers.add(rel)
    assert importers - {"src/contracts/routine_sources.py"} == _IMPORTERS


@pytest.mark.passes_on_main
def test_nothing_but_the_routine_sources_module_reads_a_chunk():
    readers = set()
    for path in _python_files():
        rel = str(path.relative_to(REPO))
        tree = ast.parse(path.read_text())
        named = {n for _, names in _imports(tree) for n in names}
        calls = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
            n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
        }
        if (named | calls) & _READERS:
            readers.add(rel)
    assert readers <= {
        "src/agents/collector/routine_sources.py",
        "src/contracts/routine_sources.py",
        # Checks each routine on its exact text and keeps no text (#1934, ADR-034).
        "src/tools/routines/migration.py",
        # Writes the exact text only into step 1's deploy script (#1947, ADR-034).
        "src/tools/data_movement/routine_scripts.py",
        # Reads only the status each MySQL event's CREATE EVENT gives, never keeps text (#1948).
        "src/tools/data_movement/cutover_check.py",
    }


@pytest.mark.passes_on_main
def test_only_the_writer_and_the_registry_name_the_files():
    naming = set()
    for path in _python_files():
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if any(p in node.value for p in _PATHS) and not _is_docstring(tree, node):
                    naming.add(str(path.relative_to(REPO)))
    assert naming <= {
        "src/agents/collector/routine_sources.py",
        "src/contracts/routine_sources.py",  # the chunk path pattern
        "src/shared/contract_versioning.py",
    }, naming


def _is_docstring(tree: ast.AST, node: ast.Constant) -> bool:
    for parent in ast.walk(tree):
        body = getattr(parent, "body", None)
        if isinstance(body, list) and body and isinstance(body[0], ast.Expr):
            if body[0].value is node:
                return True
    return False
