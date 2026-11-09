# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Golden fixtures: every backend must produce exactly these rows and cast failures (#1763)."""

import datetime
import hashlib
import json
import math
import struct
from collections.abc import Collection
from decimal import Decimal
from pathlib import Path

import pytest

from src.contracts.transformation_contract import Rule
from src.tools.synthetic_data.generator import generate_rows
from src.tools.transformation_contract.duckdb_backend import (
    UnknownTargetType,
    duckdb_type,
    run_rule,
)
from src.tools.transformation_contract.plan_ir import RulePlan, plan_rule

FIXTURE_DIR = Path(__file__).parents[4] / "tests/fixtures/transformation_contract"
FIXTURES = sorted(FIXTURE_DIR.glob("*.json"))


def _f32(value: float) -> float:
    return float(struct.unpack("<f", struct.pack("<f", value))[0])


def float_text(value: float, *, single: bool) -> str:
    """The float rule (README): the shortest decimal that reads back as the same value
    at the column's precision, in plain digits with at least one after the point."""
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "Infinity" if value > 0 else "-Infinity"
    text = repr(value)
    if single:
        text = next(t for d in range(1, 10) if _f32(float(t := f"{value:.{d}g}")) == value)
    plain = format(Decimal(text), "f")
    return plain if "." in plain else plain + ".0"


def canonical_text(value: object, *, single: bool = False) -> str | None:
    """The one text form of an output value (README convention): null stays null.
    ``single``: the value comes from a single-precision float column."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return float_text(value, single=single)
    if isinstance(value, bytes):
        return "\\x" + value.hex()
    if isinstance(value, Decimal):
        return str(value)  # keeps its scale
    if isinstance(value, datetime.datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    if isinstance(value, datetime.date):
        return value.isoformat()
    return str(value)


def single_precision_columns(plan: RulePlan, engine: str) -> set[str]:
    """The output columns of a single-precision float type (MySQL FLOAT, FLOAT(p<=24),
    FLOAT(M,D); PostgreSQL real, float4, float(p<=24))."""
    out = set()
    for c in plan.columns:
        try:
            if duckdb_type(c.cast_to or c.target_type, engine) == "FLOAT":
                out.add(c.target_column)
        except UnknownTargetType:
            pass
    return out


def canonical_rows(
    rows: list[dict[str, object]], single: Collection[str] = ()
) -> list[dict[str, str | None]]:
    return [{k: canonical_text(v, single=k in single) for k, v in row.items()} for row in rows]


def canonical_failures(run) -> list[list[str]]:
    """Each failure as [target column, the source value as text, target type]."""
    return [[f.target_column, str(f.value), f.to_type] for f in run.cast_failures]


def input_digest(case: dict, generated: dict[str, list[dict[str, object]]]) -> str:
    """SHA-256 of the canonical JSON of the generated rows of every table."""
    text = json.dumps(
        generated, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def generated_rows(case: dict) -> dict[str, list[dict[str, object]]]:
    return {
        t["table_id"]: generate_rows(t, dialect=case["dialect"], seed=case["seed"]).rows
        for t in case["tables"]
    }


def _load(path: Path) -> dict:
    case: dict = json.loads(path.read_text(encoding="utf-8"))
    return case


def test_fixtures_cover_both_engines_and_the_edge_cases():
    cases = {p.stem: _load(p) for p in FIXTURES}
    assert {c["engine"] for c in cases.values()} == {"aurora_mysql", "aurora_postgresql"}
    # A pair with the same tables and rules whose engines give different results.
    pairs = [
        (a, b)
        for a in cases.values()
        for b in cases.values()
        if a["engine"] != b["engine"]
        and (a["tables"], a["rules"]) == (b["tables"], b["rules"])
        and a["expected"] != b["expected"]
    ]
    assert pairs
    zero_dates = [
        f
        for c in cases.values()
        if c["engine"] == "aurora_mysql"
        for e in c["expected"].values()
        for f in e["cast_failures"]
        if f[1].startswith("0000-00-00")
    ]
    assert zero_dates


@pytest.mark.parametrize("path", [p for p in FIXTURES if p.stem.startswith("boolean_")],
                         ids=lambda p: p.stem)  # fmt: skip
def test_boolean_fixtures_include_a_false_row(path):
    case = _load(path)
    values = [r["is_active"] for rows in generated_rows(case).values() for r in rows]
    assert 0 in values
    outputs = [r["is_active"] for e in case["expected"].values() for r in e["rows"]]
    assert ("false" if case["engine"] == "aurora_postgresql" else "0") in outputs


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_duckdb_matches_the_golden_output(path):
    case = _load(path)
    engine = case["engine"]
    tables = {t["table_id"]: t for t in case["tables"]}
    assert set(case["expected"]) == {r["source_table"] for r in case["rules"]}
    generated = generated_rows(case)
    assert input_digest(case, generated) == case["input_sha256"]
    for raw in case["rules"]:
        rule = Rule.model_validate(raw)
        table = tables[rule.source_table]
        synthetic = generate_rows(table, dialect=case["dialect"], seed=case["seed"])
        run = run_rule(
            plan_rule(rule),
            synthetic,
            source_columns=[c["column_name"] for c in table["columns"]],
            engine=engine,
        )
        want = case["expected"][rule.source_table]
        plan = plan_rule(rule)
        assert canonical_rows(run.rows, single_precision_columns(plan, engine)) == want["rows"]
        assert canonical_failures(run) == want["cast_failures"]
        assert run.unchecked_columns == want["unchecked"]


@pytest.mark.passes_on_main  # guards the fixture data, not the backend
def test_every_fixture_lists_its_unchecked_columns_and_some_list_one():
    expected = [e for p in FIXTURES for e in _load(p)["expected"].values()]
    assert all(isinstance(e.get("unchecked"), list) for e in expected)
    assert any(e["unchecked"] for e in expected)


@pytest.mark.passes_on_main  # guards the fixture data, not the backend
def test_a_fixture_pins_the_float_text_rule_for_both_precisions():
    texts = {
        (c["target_type"].lower(), r[c["target_column"]])
        for p in FIXTURES
        for raw in _load(p)["rules"]
        for c in raw["fields"]
        for e in [_load(p)["expected"][raw["source_table"]]]
        for r in e["rows"]
        if c["target_type"].lower().startswith(("float", "double", "real"))
        and r.get(c["target_column"]) is not None
    }
    assert any(t.startswith("float") for t, _ in texts)
    assert any(t.startswith("double") for t, _ in texts)
    assert all("e" not in v.lower() and "." in v for _, v in texts)


@pytest.mark.passes_on_main  # the harness's own rule, not the backend's behaviour
@pytest.mark.parametrize(
    "value,single,text",
    [(_f32(12.35), True, "12.35"), (12.35, False, "12.35"), (_f32(0.1), False,
     "0.10000000149011612"), (_f32(0.1), True, "0.1"), (1e16, False, "10000000000000000.0"),
     (100000.0, True, "100000.0"), (-0.0, False, "-0.0"), (1e-7, False, "0.0000001"),
     (float("inf"), True, "Infinity"), (float("-inf"), False, "-Infinity"),
     (float("nan"), False, "NaN")],
)  # fmt: skip
def test_the_float_text_rule(value, single, text):
    assert float_text(value, single=single) == text
