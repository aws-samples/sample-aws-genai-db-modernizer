# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Step 1 contracts for the verify tests (#1998): tables of tricky column types."""

from __future__ import annotations

from src.contracts.lineage_ops import ColumnRef, CopyOp
from src.contracts.transformation_contract import FieldRule, Finding, Rule, StepContract, TargetSpec
from tests.unit.tools.data_movement.dms_step_fixtures import NO_KEY

SHA = "c" * 64

PG_TYPES = [
    ("id", "bigint"), ("n", "numeric"), ("m", "numeric(10,2)"), ("f8", "double precision"),
    ("f4", "real"), ("b", "boolean"), ("ts", "timestamp(6) without time zone"),
    ("tstz", "timestamp with time zone"), ("d", "date"), ("t", "time"), ("iv", "interval"),
    ("by", "bytea"), ("j", "json"), ("jb", "jsonb"), ("arr", "text[]"), ("iarr", "integer[]"),
    ("tsarr", "timestamptz[]"), ("u", "uuid"), ("txt", "text"), ("cash", "money"), ("farr", "double precision[]"),
]  # fmt: skip
MYSQL_TYPES = [
    ("id", "bigint"), ("dec", "decimal(10,2)"), ("f", "float"), ("dbl", "double"),
    ("flag", "tinyint(1)"), ("bits", "bit(8)"), ("dt", "datetime(6)"), ("ts", "timestamp(6)"),
    ("dd", "date"), ("tm", "time(6)"), ("y", "year"), ("c", "varchar(50)"), ("bl", "blob"),
    ("j", "json"), ("e", "enum('a','b')"), ("vb", "varbinary(8)"),
]  # fmt: skip


def rule(schema: str, name: str, cols: list[tuple[str, str]], *, keyed: bool = True) -> Rule:
    table_id = f"{schema}.{name}"
    fields = [FieldRule(source_column=c, target_column=c, target_type=t,
                        op=CopyOp(op="copy", column=ColumnRef(table_id=table_id, column=c)))
              for c, t in cols]  # fmt: skip
    findings = [] if keyed else [Finding(code="no_key", severity="warning", message=NO_KEY,
                                         table_id=table_id)]  # fmt: skip
    return Rule(input_schema="collector", source_table=table_id, target_table=name,
                primary_key=["id"] if keyed else [], fields=fields, findings=findings)  # fmt: skip


def contract(engine: str, rules: list[Rule], unsupported_count: int = 0) -> StepContract:
    return StepContract(job_id="job1", assignment_version=2, step=1, wave=1,
                        wave_shape="homogeneous", target=TargetSpec(engine=engine),
                        status="valid", rules=rules, unsupported_count=unsupported_count)  # fmt: skip


def pg_contract() -> StepContract:
    return contract("aurora_postgresql", [
        rule("public", "things", PG_TYPES),
        rule("public", "nokey", [("v", "text"), ("k", "integer")], keyed=False),
        rule("public", "gone", [("id", "integer")]),
    ])  # fmt: skip


def mysql_contract() -> StepContract:
    return contract("aurora_mysql", [
        rule("shop", "things", MYSQL_TYPES),
        rule("shop", "nokey", [("v", "varchar(20)"), ("k", "int")], keyed=False),
        rule("shop", "gone", [("id", "int")]),
    ])  # fmt: skip
