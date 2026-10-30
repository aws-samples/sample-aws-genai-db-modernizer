# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Saved contract 1.3 ``assess()`` results read back with the same values (#1648).

``tests/fixtures/assess_results_v1_3/`` holds results written by the code released as
v0.4.0-beta.3, the first release of contract 1.3: WordPress at ``analysis`` and at
``reality_check`` (deterministic) and the SQL Server sample ``trailgear`` at
``analysis`` (foreign key statistics). They are a record, never regenerated (the
README there; the edit hook blocks hand edits). Every later release must read each
back through ``AssessmentResult`` and keep every saved value: a change that breaks
reading a stored result fails here. Each test guards what main already reads, so
each is marked ``passes_on_main``. A MINOR may add fields (they are not in the saved
files and are not compared); it may not drop, rename or change a saved one.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from src.assessment import AssessmentResult

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "assess_results_v1_3"
SAVED_VERSION = "1.3"

#: Per file: what it was written for and values read back from it.
CASES: dict[str, dict[str, Any]] = {
    "wordpress_analysis.json": {
        "run_through": "analysis",
        "label": "analysis_only",
        "source_engine": "mysql",
        "statement_count": 107,
        "statements_per_engine": {},
        "assignment_version": None,
        "table_count": 50,
        "fit": {
            "0452bed28b31e64ae9294ddef1d5e82c3166557fd4c02b6e7d4140a11170af82": (
                None,
                {
                    "aurora_mysql": 63,
                    "documentdb": 79,
                    "dynamodb": 95,
                    "elasticache": 51,
                    "opensearch": 0,
                },
            ),
        },
        "row_counts": {
            "wordpress.wp_actionscheduler_actions": 12537,
            "wordpress.wp_woocommerce_tax_rates": 0,
        },
        "foreign_keys": {},
    },
    "wordpress_reality_check.json": {
        "run_through": "reality_check",
        "label": "recommendation",
        "source_engine": "mysql",
        "statement_count": 107,
        "statements_per_engine": {"aurora_mysql": 67, "dynamodb": 40},
        "assignment_version": 2,
        "table_count": 50,
        "fit": {
            "0452bed28b31e64ae9294ddef1d5e82c3166557fd4c02b6e7d4140a11170af82": (
                "dynamodb",
                {
                    "aurora_mysql": 63,
                    "documentdb": 79,
                    "dynamodb": 95,
                    "elasticache": 51,
                    "opensearch": 0,
                },
            ),
            "3a5af258b286c55a86defe9dc509fee3e199c02019eb8faa10ce2d072ee1427a": (
                "aurora_mysql",
                {"aurora_mysql": 49},
            ),
        },
        "row_counts": {
            "wordpress.wp_actionscheduler_actions": 12537,
            "wordpress.wp_woocommerce_tax_rates": 0,
        },
        "foreign_keys": {},
    },
    "trailgear_analysis.json": {
        "run_through": "analysis",
        "label": "analysis_only",
        "source_engine": "sqlserver",
        "statement_count": 95,
        "statements_per_engine": {},
        "assignment_version": None,
        "table_count": 30,
        "fit": {
            "0xE715A6116BCB0673": (
                None,
                {"aurora_mysql": 67, "aurora_postgresql": 81, "documentdb": 56, "opensearch": 74},
            ),
        },
        "row_counts": {"trailgear.Brand": 140, "trailgear.Warehouse": 12},
        "foreign_keys": {
            "FK_CustomerAddress_Customer": (
                "trailgear.CustomerAddress",
                "trailgear.Customer",
                1.2864077669902914,
                "catalogue_estimate",
            ),
            "FK_Shipment_Order": (
                "trailgear.Shipment",
                "trailgear.SalesOrder",
                0.948905109489051,
                "catalogue_estimate",
            ),
        },
        "foreign_key_count": 28,
    },
}


@cache
def _text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _read(name: str) -> AssessmentResult:
    return AssessmentResult.model_validate_json(_text(name))


def _changed(saved: Any, read: Any, path: str = "$") -> list[str]:
    """Where ``read`` lost or changed a value of ``saved`` (fields only ``read`` has
    are a later MINOR's additions and are ignored)."""
    if isinstance(saved, dict):
        if not isinstance(read, dict):
            return [f"{path}: was an object"]
        return [
            change
            for key, value in saved.items()
            for change in (
                _changed(value, read[key], f"{path}.{key}")
                if key in read
                else [f"{path}.{key}: missing"]
            )
        ]
    if isinstance(saved, list):
        if not isinstance(read, list) or len(read) != len(saved):
            return [f"{path}: was a list of {len(saved)}"]
        return [
            c
            for i, (s, r) in enumerate(zip(saved, read, strict=True))
            for c in _changed(s, r, f"{path}[{i}]")
        ]
    if not _same(saved, read):
        return [f"{path}: {saved!r} became {read!r}"]
    return []


def _same(saved: Any, read: Any) -> bool:
    # A number saved as 5 that the model reads as a float (5.0) is the same value; a
    # None, a bool or a string must stay one.
    if any(v is None or isinstance(v, (bool, str)) for v in (saved, read)):
        return type(saved) is type(read) and saved == read
    return bool(saved == read)


@pytest.mark.passes_on_main
def test_every_saved_result_has_a_case() -> None:
    assert sorted(p.name for p in FIXTURES.glob("*.json")) == sorted(CASES)


@pytest.mark.passes_on_main
@pytest.mark.parametrize("name", sorted(CASES))
def test_a_saved_result_reads_back_with_every_saved_value(name: str) -> None:
    saved = json.loads(_text(name))
    read = _read(name).model_dump(mode="json")
    changes = _changed(saved, read)
    assert not changes, f"{name}: {changes[:10]}"


@pytest.mark.passes_on_main
@pytest.mark.parametrize("name", sorted(CASES))
def test_a_saved_result_keeps_its_key_values(name: str) -> None:
    case = CASES[name]
    result = _read(name)
    summary = result.summary
    assert result.contract_version == SAVED_VERSION
    assert result.label == case["label"]
    assert result.options.run_through == case["run_through"]
    assert summary.run_through_reached == case["run_through"]
    assert summary.source_engine == case["source_engine"]
    assert summary.statement_count == len(result.statements) == case["statement_count"]
    assert summary.statements_per_engine == case["statements_per_engine"]
    assert summary.assignment_version == case["assignment_version"]
    statements = {s.query_id: s for s in result.statements}
    for query_id, (engine, fit) in case["fit"].items():
        assert statements[query_id].engine == engine
        assert statements[query_id].fit == fit
    facts = {t.table_id: t for t in summary.table_facts}
    assert len(facts) == case["table_count"]
    for table_id, rows in case["row_counts"].items():
        assert facts[table_id].row_count == rows


@pytest.mark.passes_on_main
@pytest.mark.parametrize("name", sorted(CASES))
def test_row_count_null_ness_survives(name: str) -> None:
    # Each sample counts every table: a known 0 (an empty table) stays 0, never None.
    saved = {
        t["table_id"]: t["row_count"] for t in json.loads(_text(name))["summary"]["table_facts"]
    }
    read = {t.table_id: t.row_count for t in _read(name).summary.table_facts}
    assert {k: v is None for k, v in read.items()} == {k: v is None for k, v in saved.items()}
    assert read == saved


@pytest.mark.passes_on_main
def test_trailgear_foreign_key_averages_survive() -> None:
    case = CASES["trailgear_analysis.json"]
    tables = _read("trailgear_analysis.json").summary.table_facts
    fks = {fk.constraint_name: (t.table_id, fk) for t in tables for fk in t.foreign_keys}
    assert len(fks) == case["foreign_key_count"]
    for name, (table, parent, average, basis) in case["foreign_keys"].items():
        table_id, fk = fks[name]
        assert (table_id, fk.referenced_table) == (table, parent)
        assert fk.avg_children_per_parent == average
        assert fk.basis == basis
    assert all(fk.avg_children_per_parent is not None for _, fk in fks.values())


@pytest.mark.passes_on_main
def test_a_saved_result_with_unknown_counts_reads_back_with_its_nulls() -> None:
    # A 1.3 result may carry an unknown count (None) and so no average: the saved
    # samples count every table, so the trailgear one is read back with one unknown.
    saved = json.loads(_text("trailgear_analysis.json"))
    table = next(t for t in saved["summary"]["table_facts"] if t["foreign_keys"])
    table["row_count"] = table["row_count_basis"] = table["size_mb"] = None
    for fk in table["foreign_keys"]:
        fk["avg_children_per_parent"] = fk["basis"] = None
    read = AssessmentResult.model_validate_json(json.dumps(saved)).model_dump(mode="json")
    assert not _changed(saved, read)
