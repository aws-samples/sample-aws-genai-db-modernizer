# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""PostgreSQL enum types survive a same-engine step 1 (#1834).

On a PostgreSQL to Aurora PostgreSQL step 1 an enum column whose labels the collection
carries keeps its type: the contract copies it as ``"<schema>"."<type>"`` and
``step-1.ddl.sql`` creates the type (labels in the source's order, quoted as string
literals) before the tables. Without the labels (a collection from before #1834), or with
a type name that is not a plain identifier, the column stays TEXT and a warning finding
and the DDL header say why. A MySQL ENUM is not touched.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from src.contracts.aurora_design_delta import validate_aurora_type
from src.contracts.lineage_ops import ColumnRef, CopyOp
from src.contracts.transformation_contract import FieldRule, StepContract
from src.storage.local_store import LocalArtifactStore
from src.tools.data_movement.dms_step import (
    DmsStepBundle,
    DmsStepError,
    bundle_inputs,
    load_step1,
    render_ddl,
)
from src.tools.data_movement.plan import write_plans
from src.tools.transformation_contract.step1 import build_step1
from src.tools.transformation_contract.validate import validate_step1
from src.tools.transformation_contract.write import write_step1
from tests.unit.tools.transformation_contract.conftest import col, collector, step1_inputs, table

from .dms_step_fixtures import make_bundle

MOOD = {"schema_name": "public", "type_name": "mood", "labels": ["sad", "ok", "it's fine"]}
QUALIFIED = '"public"."mood"'


def _tables() -> list[dict[str, Any]]:
    return [
        table("public", "orders", [col("id", "int8", 1), col("feel", "mood", 2)], pk=["id"]),
        table("public", "notes", [col("id", "int8", 1), col("body", "text", 2)], pk=["id"]),
    ]


def _write_job(root: Path, enums: list[dict[str, Any]] | None) -> LocalArtifactStore:
    coll = collector("postgresql", _tables(), [])
    coll["metadata"]["source_database"].update(database_name="shop", version="16.4")
    if enums is not None:
        coll["database_schema"]["enum_types"] = enums
    wave = {"wave": 1, "title": "Move", "engines": ["aurora_postgresql"], "moves_from": ["postgresql"],
            "homogeneity": "homogeneous", "tables": ["public.notes", "public.orders"], "gate": "g"}  # fmt: skip
    store = LocalArtifactStore(str(root))
    store.write_json("shop/job1/synthesis/v2/report.json",
                     {"assignment_summary": {"version": 2}, "migration_waves": [wave]})  # fmt: skip
    store.write_json("shop/job1/assignment/v2/assignment.json", {"version": 2})
    store.write_json("shop/job1/collector/output.json", coll)
    write_step1(store, "shop", "job1")
    write_plans(store, "shop", "job1")
    return store


def _pg_inputs(enums: list[dict[str, Any]] | None):
    inputs = step1_inputs(engine="postgresql", tables=_tables())
    if enums is not None:
        inputs.collector["database_schema"]["enum_types"] = enums
    return inputs


def _field(contract: StepContract, table_id: str, column: str) -> FieldRule:
    rule = next(r for r in contract.rules if r.source_table == table_id)
    return next(f for f in rule.fields if f.target_column == column)


def _findings(contract, code: str) -> list:
    return [f for r in contract.rules for f in r.findings if f.code == code]


# -- the type grammar ------------------------------------------------------------------


def test_a_quoted_qualified_type_is_an_aurora_postgresql_type_only():
    assert validate_aurora_type(QUALIFIED, "aurora_postgresql") == QUALIFIED
    for engine in ("aurora_mysql", None):
        with pytest.raises(ValueError):
            validate_aurora_type(QUALIFIED, engine)


@pytest.mark.passes_on_main
@pytest.mark.parametrize(
    "bad",
    ['"public"."mood"; DROP TABLE x', '"pub"lic"."mood"', '"public".mood', '"1x"."mood"',
     '"public"."mood"[]', '"public"."a b"'],
)  # fmt: skip
def test_anything_but_two_plain_quoted_identifiers_is_refused(bad: str):
    with pytest.raises(ValueError):
        validate_aurora_type(bad, "aurora_postgresql")


# -- the contract ----------------------------------------------------------------------


def test_an_enum_with_known_labels_is_copied_as_its_qualified_type():
    inputs = _pg_inputs([MOOD])
    contract = validate_step1(build_step1(inputs), inputs)
    feel = _field(contract, "public.orders", "feel")
    assert feel.target_type == QUALIFIED
    assert feel.op.op == "copy"
    assert not [f for r in contract.rules for f in r.findings if f.severity == "error"]
    assert not _findings(contract, "enum_labels_not_collected")


def test_without_collected_labels_the_column_stays_text_with_a_finding():
    inputs = _pg_inputs(None)
    contract = validate_step1(build_step1(inputs), inputs)
    feel = _field(contract, "public.orders", "feel")
    assert feel.target_type == "TEXT" and feel.op.op == "transform"
    (finding,) = _findings(contract, "enum_labels_not_collected")
    assert finding.severity == "warning" and finding.column == "feel"
    assert finding.table_id == "public.orders"
    # A built-in type is never flagged.
    assert not [f for f in _findings(contract, "enum_labels_not_collected") if f.column == "body"]


def test_a_type_name_that_is_not_a_plain_identifier_stays_text_with_a_finding():
    bad = {"schema_name": "public", "type_name": 'mood"; DROP TABLE x; --', "labels": ["a"]}
    tables = [table("public", "orders", [col("id", "int8", 1), col("feel", bad["type_name"], 2)],
                    pk=["id"])]  # fmt: skip
    inputs = step1_inputs(engine="postgresql", tables=tables)
    inputs.collector["database_schema"]["enum_types"] = [bad]
    contract = validate_step1(build_step1(inputs), inputs)
    assert _field(contract, "public.orders", "feel").target_type == "TEXT"
    (finding,) = _findings(contract, "enum_type_refused")
    assert finding.severity == "warning" and "DROP" not in finding.message


@pytest.mark.passes_on_main
def test_a_mysql_enum_is_unchanged():
    tables = [table("wp", "posts", [col("id", "bigint", 1), col("st", "enum('a','b''c')", 2)],
                    pk=["id"])]  # fmt: skip
    inputs = step1_inputs(tables=tables)
    contract = validate_step1(build_step1(inputs), inputs)
    st = _field(contract, "wp.posts", "st")
    assert st.target_type.upper() == "ENUM('A','B''C')"
    assert st.op.op == "copy"
    assert not _findings(contract, "enum_labels_not_collected")
    assert not _findings(contract, "enum_type_refused")


# -- step-1.ddl.sql --------------------------------------------------------------------


def test_the_ddl_creates_the_type_before_the_tables_and_uses_it(tmp_path):
    ddl = render_ddl(load_step1(_write_job(tmp_path, [MOOD]), "shop", "job1"))
    create = "CREATE TYPE \"public\".\"mood\" AS ENUM ('sad', 'ok', 'it''s fine');"
    assert create in ddl
    assert ddl.index(create) < ddl.index('CREATE TABLE "notes"')
    assert ddl.index(create) < ddl.index('CREATE TABLE "orders"')
    assert re.search(r'"feel" "public"\."mood" NOT NULL', ddl)
    # The labels are read as standard strings whatever the session's setting.
    assert ddl.index("SET standard_conforming_strings = on;") < ddl.index(create)


def test_the_ddl_creates_only_the_types_the_moved_columns_use(tmp_path):
    unused = {"schema_name": "public", "type_name": "unused", "labels": ["x"]}
    ddl = render_ddl(load_step1(_write_job(tmp_path, [MOOD, unused]), "shop", "job1"))
    assert ddl.count("CREATE TYPE") == 1 and '"unused"' not in ddl


def test_an_old_collection_keeps_text_and_the_header_says_why(tmp_path):
    ddl = render_ddl(load_step1(_write_job(tmp_path, None), "shop", "job1"))
    assert "CREATE TYPE" not in ddl
    assert re.search(r'"feel" TEXT NOT NULL', ddl)
    header = " ".join(line[3:] for line in ddl.splitlines() if line.startswith("-- "))
    assert "public.orders.feel" in header and "enum labels" in header


@pytest.mark.passes_on_main
def test_without_enums_the_ddl_has_no_enum_block(tmp_path):
    ddl = render_ddl(load_step1(_write_job(tmp_path, []), "shop", "job1"))
    assert "CREATE TYPE" not in ddl and "standard_conforming_strings" not in ddl


def test_a_qualified_type_the_inputs_cannot_create_is_refused():
    bundle = make_bundle("aurora_postgresql")
    rules = []
    for r in bundle.contract.rules:
        if r.target_table == "orders":
            ref = ColumnRef(table_id=r.source_table, column="feel")
            extra = FieldRule(source_column="feel", target_column="feel", target_type=QUALIFIED,
                              op=CopyOp(op="copy", column=ref))  # fmt: skip
            r = r.model_copy(update={"fields": [*r.fields, extra]})
        rules.append(r)
    contract = bundle.contract.model_copy(update={"rules": rules})
    plan = bundle.plan.model_copy(update={"contract": None})
    with pytest.raises(DmsStepError, match="mood"):
        bundle_inputs(DmsStepBundle(plan=plan, contract=contract, ddl=bundle.ddl))
