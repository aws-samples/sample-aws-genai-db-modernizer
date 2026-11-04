# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Golden fixtures: every backend must produce exactly these rows and cast failures (#1763)."""

import datetime
import hashlib
import json
from decimal import Decimal
from pathlib import Path

import pytest

from src.contracts.transformation_contract import Rule
from src.tools.synthetic_data.generator import generate_rows
from src.tools.transformation_contract.duckdb_backend import run_rule
from src.tools.transformation_contract.plan_ir import plan_rule

FIXTURE_DIR = Path(__file__).parents[4] / "tests/fixtures/transformation_contract"
FIXTURES = sorted(FIXTURE_DIR.glob("*.json"))


def canonical_text(value: object) -> str | None:
    """The one text form of an output value (README convention): null stays null."""
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, Decimal):
        return str(value)  # keeps its scale
    if isinstance(value, datetime.datetime):
        return value.strftime("%Y-%m-%dT%H:%M:%S")
    if isinstance(value, datetime.date):
        return value.isoformat()
    return str(value)


def canonical_rows(rows: list[dict[str, object]]) -> list[dict[str, str | None]]:
    return [{k: canonical_text(v) for k, v in row.items()} for row in rows]


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
    return json.loads(path.read_text(encoding="utf-8"))


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
        assert canonical_rows(run.rows) == want["rows"]
        assert canonical_failures(run) == want["cast_failures"]
