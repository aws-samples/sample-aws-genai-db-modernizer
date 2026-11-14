# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""#1817: an assignment saved with a write group keyed on a placeholder table is read
without it, everywhere (ADR-031 Amendment 9).

A saved assignment's ``write_groups`` has one reader,
``placeholder_tables.saved_write_groups``, and it drops a group keyed on a placeholder
or dummy table (``unknown``, ``UNKNOWN``, ``None``, ``null``, ``DUAL``, ...). The
guards below scan every module under ``src/`` and ``scripts/``, the way
``test_only_the_ledger_writes_engines.py`` scans engine writes: no other module reads
the raw field, and no module filters placeholder tables with a list narrower than the
shared one. The behavioural tests read one old-style assignment through each reader
that reports or routes on write groups.
"""

from __future__ import annotations

import ast
from functools import cache
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from src.storage.local_store import LocalArtifactStore

ROOT = Path(__file__).resolve().parents[4]
SCANNED = ("src", "scripts")

# The one reader of a saved assignment's ``write_groups``.
ACCESSOR = Path("src/shared/placeholder_tables.py")
# Modules whose ``write_groups`` read is not a saved assignment's, and why.
NOT_A_SAVED_ASSIGNMENT = {
    # The engineering report renders the routing detail's own ``write_groups`` list,
    # which ``report_facts.routing_detail_facts`` builds through the accessor.
    Path("src/report/detailed_report.py"),
}

# Modules that may still name ``PSEUDO_TABLES``, and why. Every other filter of
# placeholder tables uses ``placeholder_tables.is_placeholder_table``.
PSEUDO_TABLES_READERS = {
    # The resolver's own list (its definition), kept inside the shared one by
    # ``test_the_resolver_list_is_inside_the_shared_one``.
    Path("src/agents/referee/table_resolution.py"),
    # The utility-statement test reads the resolver's list, case-folded, on purpose:
    # changing it would change utility pins (#1817 keeps them as they are).
    Path("src/agents/referee/utility_statements.py"),
}

LEGACY_GROUPS = {"UNKNOWN": ["w1", "w2"], "None": ["w1", "w2"], "shop.orders": ["w3", "w4"]}


@cache
def _modules() -> tuple[Path, ...]:
    out: list[Path] = []
    for top in SCANNED:
        out.extend(p.relative_to(ROOT) for p in sorted((ROOT / top).rglob("*.py")))
    return tuple(out)


@cache
def _tree(path: Path) -> ast.Module:
    return ast.parse((ROOT / path).read_text(encoding="utf-8"), filename=str(path))


def _raw_reads(tree: ast.Module) -> list[int]:
    """Lines that read a ``write_groups`` field: ``x.get("write_groups")``,
    ``x["write_groups"]``, ``getattr(x, "write_groups")`` or ``x.write_groups``."""
    lines: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func, args = node.func, node.args
            first = args[0] if args else None
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "get"
                and isinstance(first, ast.Constant)
                and first.value == "write_groups"
            ):
                lines.append(node.lineno)
            if (
                isinstance(func, ast.Name)
                and func.id == "getattr"
                and len(args) >= 2
                and isinstance(args[1], ast.Constant)
                and args[1].value == "write_groups"
            ):
                lines.append(node.lineno)
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Load):
            if isinstance(node.slice, ast.Constant) and node.slice.value == "write_groups":
                lines.append(node.lineno)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            if node.attr == "write_groups":
                lines.append(node.lineno)
    return lines


class TestTheGuards:
    def test_no_module_reads_the_raw_saved_write_groups_but_the_accessor(self) -> None:
        offenders = {
            str(path): lines
            for path in _modules()
            if path != ACCESSOR
            and path not in NOT_A_SAVED_ASSIGNMENT
            and (lines := _raw_reads(_tree(path)))
        }
        assert offenders == {}, (
            "read a saved assignment's write groups with "
            "placeholder_tables.saved_write_groups (#1817)"
        )

    @pytest.mark.passes_on_main  # a self-test of the scan in this file
    def test_the_scan_sees_every_way_to_read_the_field(self) -> None:
        tree = ast.parse(
            "a.get('write_groups')\n"
            "a['write_groups']\n"
            "getattr(a, 'write_groups', None)\n"
            "a.write_groups\n"
            "out['write_groups'] = {}\n"
            "a.write_groups = {}\n"
            "f(write_groups=x)\n"
            "'write_groups' in a\n"
        )
        assert _raw_reads(tree) == [1, 2, 3, 4]

    def test_the_allowlists_name_no_stale_module(self) -> None:
        for path in {ACCESSOR} | NOT_A_SAVED_ASSIGNMENT:
            assert _raw_reads(_tree(path)), path
        for path in PSEUDO_TABLES_READERS:
            assert "PSEUDO_TABLES" in (ROOT / path).read_text(encoding="utf-8"), path

    def test_the_graph_flags_exactly_what_routing_skips(self) -> None:
        from src.graph.populators import _placeholder_reason
        from src.shared.placeholder_tables import PLACEHOLDER_TABLE_NAMES

        assert all(_placeholder_reason(t) for t in PLACEHOLDER_TABLE_NAMES | {"", " "})
        for real in ("Null", "none", "Unknown", "db.a"):
            assert _placeholder_reason(real) is None, real

    def test_no_module_filters_placeholders_with_a_narrower_list(self) -> None:
        names = {
            str(path)
            for path in _modules()
            for node in ast.walk(_tree(path))
            if (isinstance(node, ast.Name) and node.id == "PSEUDO_TABLES")
            or (isinstance(node, ast.Attribute) and node.attr == "PSEUDO_TABLES")
            or (
                isinstance(node, ast.ImportFrom)
                and any(a.name == "PSEUDO_TABLES" for a in node.names)
            )
        }
        assert names <= {str(p) for p in PSEUDO_TABLES_READERS}, sorted(
            names - {str(p) for p in PSEUDO_TABLES_READERS}
        )

    def test_no_module_keeps_its_own_placeholder_list(self) -> None:
        """A set literal naming two or more placeholder tables is a local copy of the
        shared list; only the shared module and the resolver's documented list hold one."""
        from src.shared.placeholder_tables import PLACEHOLDER_TABLE_NAMES

        allowed = {str(ACCESSOR), "src/agents/referee/table_resolution.py"}
        copies = set()
        for path in _modules():
            for node in ast.walk(_tree(path)):
                if isinstance(node, ast.Set):
                    values = {
                        e.value
                        for e in node.elts
                        if isinstance(e, ast.Constant) and isinstance(e.value, str)
                    }
                    if len(values & PLACEHOLDER_TABLE_NAMES) >= 2:
                        copies.add(str(path))
        assert copies <= allowed, sorted(copies - allowed)

    def test_the_resolver_list_is_inside_the_shared_one(self) -> None:
        from src.agents.referee.table_resolution import PSEUDO_TABLES
        from src.shared.placeholder_tables import is_placeholder_table

        assert all(is_placeholder_table(t) for t in PSEUDO_TABLES)


def _legacy(**extra: Any) -> dict[str, Any]:
    return {
        "version": 1,
        "co_dependency_groups": [],
        "write_groups": dict(LEGACY_GROUPS),
        "query_assignments": [
            {"query_id": q, "assigned_engine": e, "source_tables": t, "in_scope": True}
            for q, e, t in (
                ("w1", "dynamodb", ["UNKNOWN"]),
                ("w2", "dynamodb", ["UNKNOWN"]),
                ("w3", "aurora_postgresql", ["shop.orders"]),
                ("w4", "aurora_postgresql", ["shop.orders"]),
            )
        ],
        **extra,
    }


class TestEveryReaderDropsASavedPlaceholderGroup:
    def test_the_accessor(self) -> None:
        from src.contracts.assignment_models import Assignment
        from src.shared.placeholder_tables import saved_write_groups

        assert saved_write_groups(_legacy()) == {"shop.orders": ["w3", "w4"]}
        assert saved_write_groups(None) == {}
        assert saved_write_groups({}) == {}
        model = Assignment.model_construct(write_groups=dict(LEGACY_GROUPS))  # type: ignore[call-arg]
        assert saved_write_groups(model) == {"shop.orders": ["w3", "w4"]}

    def test_the_table_usage_behind_the_owner_reasons(self) -> None:
        from src.shared.report_facts import assignment_table_usage

        usage = assignment_table_usage(_legacy())
        assert {t for t, u in usage.items() if u["writes"]} == {"shop.orders"}

    def test_the_assess_groups(self) -> None:
        from src.assessment.result_builder import RunState, _group_sources

        state = RunState(run_through="reality_check", collector={}, triage={}, lineage=[_legacy()])
        _, write_groups = _group_sources(state)
        assert write_groups == {"shop.orders": ["w3", "w4"]}

    def test_the_engineering_reports_write_group_section(self, tmp_path: Path) -> None:
        from src.shared.routing_detail import load_routing_detail
        from tests.unit.report import test_detailed_report as dr

        store = dr._store(tmp_path)
        for n in (1, 2):
            key = f"{dr.ROOT}/assignment/v{n}/assignment.json"
            version = store.read_json(key)
            version["write_groups"] = {**version["write_groups"], "UNKNOWN": ["w1", "w2"]}
            store.write_json(key, version)
        detail = load_routing_detail(
            store,
            dr.REPORT_KEY,
            dr._report(),
            store.read_json(f"{dr.ROOT}/collector/output.json"),
        )
        assert [g["name"] for g in detail["write_groups"]] == ["shop.orders"]

    def test_the_reality_check_patterns(self) -> None:
        from src.agents.referee import reality_check_patterns as rcp

        seen: list[Any] = []
        result = {"revised_assignments": [], "assignment": _legacy(), "consolidations": []}
        with (
            patch.object(rcp, "patterns_for_assignment", lambda qas, wg: seen.append(wg) or []),  # type: ignore[func-returns-value]
            patch.object(rcp, "_build_recommendations", lambda *a, **k: []),
        ):
            rcp.refresh_patterns_and_recommendations(result)
        assert seen == [{"shop.orders": ["w3", "w4"]}]


class TestThePostSchemaReroute:
    """A saved ``UNKNOWN`` group ties the flagged q-1 to the customer-pinned q-4. Read
    raw, the pin holds q-1 in place (``held_queries``); read through the accessor, q-1
    is free and moves to the best remaining engine."""

    @pytest.mark.usefixtures("schema_contract_off")
    def test_a_placeholder_group_never_holds_a_flagged_query(self, tmp_path: Path) -> None:
        from src.orchestrator.local_orchestrator import LocalOrchestrator
        from tests.unit.tools.schema import test_pe_routing_flags as pe_routing

        case = pe_routing.TestTheOrchestratorRoutesAFlagByScore()
        store = LocalArtifactStore(base_dir=str(tmp_path))
        case._seed(store)
        prefix = f"{case.DB}/{case.JOB}"
        key = f"{prefix}/assignment/v1/assignment.json"
        v1 = store.read_json(key)
        v1["write_groups"] = {"UNKNOWN": ["q-1", "q-4"]}
        for qa in v1["query_assignments"]:
            if qa["query_id"] == "q-4":
                qa["customer_override"] = True
        store.write_json(key, v1)

        orch = LocalOrchestrator(store, llm_mode="bedrock")
        with patch(
            "src.agents.schema_design.handler.run_schema_design_auto",
            side_effect=case._fake_design([]),
        ):
            orch._run_post_schema_routing(case.JOB, case.DB)

        v2 = store.read_json(f"{prefix}/assignment/v2/assignment.json")
        engine_of = {qa["query_id"]: qa["assigned_engine"] for qa in v2["query_assignments"]}
        assert engine_of["q-1"] == "aurora_mysql"
        assert engine_of["q-4"] == "dynamodb"
