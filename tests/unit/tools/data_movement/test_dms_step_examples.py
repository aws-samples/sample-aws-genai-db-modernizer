# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The committed DMS step examples are the renderer's output, and they lint (#1798)."""

from __future__ import annotations

import json
import re

import pytest
from cfnlint.api import ManualArgs, lint

from scripts import render_dms_step as script
from src.tools.data_movement.dms_step import AURORA_TEMPLATE, DDL_FILE, DMS_TEMPLATE, SEQUENCES_FILE


def _cfn_lint(text: str) -> list[str]:
    matches = lint(text, config=ManualArgs(regions=["us-east-1"], ignore_checks=["W3037"]))
    return [f"{m.rule.id} {m.message}" for m in matches]


def _example(name: str):
    return next(p for p in script.example_dirs() if p.name == name)


def test_examples_cover_mysql_and_postgresql():
    assert {p.name for p in script.example_dirs()} == {"wordpress-mysql", "postgresql-shop"}


@pytest.mark.parametrize("example", script.example_dirs(), ids=lambda p: p.name)
def test_examples_are_the_rendered_snapshot(example):
    """Regenerate with ``uv run python scripts/render_dms_step.py --examples``."""
    for name, text in script.render_example(example).items():
        assert (example / name).read_text() == text, name


@pytest.mark.parametrize("example", script.example_dirs(), ids=lambda p: p.name)
def test_example_templates_pass_cfn_lint(example):
    for name in (AURORA_TEMPLATE, DMS_TEMPLATE):
        assert _cfn_lint((example / name).read_text()) == [], name


@pytest.mark.parametrize("example", script.example_dirs(), ids=lambda p: p.name)
def test_example_mappings_list_exactly_the_contract_tables(example):
    rules = json.loads((example / "input.json").read_text())["contract"]["rules"]
    text = (example / DMS_TEMPLATE).read_text()
    for r in rules:
        assert f'"table-name": "{r["source_table"].rpartition(".")[2]}"' in text
    assert text.count('"rule-type": "selection"') == len(rules)
    assert '"rule-type": "transformation"' not in text and '"%"' not in text


def test_the_postgresql_example_recommends_replica_identity_for_every_table():
    ddl = (_example("postgresql-shop") / DDL_FILE).read_text()
    assert len(re.findall(r"^-- ALTER TABLE .* REPLICA IDENTITY FULL;$", ddl, re.M)) == 3


def test_the_postgresql_example_raises_the_unbounded_varchar_limit():
    text = (_example("postgresql-shop") / DMS_TEMPLATE).read_text()
    assert "unboundedVarcharMaxSize=10485760" in text


def test_the_mysql_example_shows_its_zero_date_counts():
    example = _example("wordpress-mysql")
    bundle = json.loads((example / "input.json").read_text())
    assert bundle["zero_date_defaults"]
    text = (example / AURORA_TEMPLATE).read_text()
    assert f"{len(bundle['zero_date_defaults'])} of them default to `0000-00-00`" in text
    assert "ALTER TABLE" not in (example / DDL_FILE).read_text()


def test_the_examples_say_nothing_internal():
    for example in script.example_dirs():
        for name in (AURORA_TEMPLATE, DMS_TEMPLATE, DDL_FILE, SEQUENCES_FILE):
            if (example / name).exists():
                assert "ADR-" not in (example / name).read_text(), (example.name, name)


def test_the_postgresql_example_sets_every_identity_columns_sequence_and_mysql_has_none():
    """The PostgreSQL example's two identity columns each get a setval (#1905); the MySQL
    example has no sequences file, and both DDL headers list what step 1 does not move."""
    text = (_example("postgresql-shop") / SEQUENCES_FILE).read_text()
    assert len(re.findall(r"^SELECT setval\(pg_get_serial_sequence\(", text, re.M)) == 2
    assert not (_example("wordpress-mysql") / SEQUENCES_FILE).exists()
    for name in ("postgresql-shop", "wordpress-mysql"):
        assert "-- Not moved by step 1" in (_example(name) / DDL_FILE).read_text(), name


def test_examples_check_mode_passes(capsys):
    assert script.main(["--examples", "--check"]) == 0
    assert json.loads(capsys.readouterr().out) == {"status": "ok"}
