# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""The backend-neutral plan of a contract rule (#1763): what DuckDB and Flink SQL render.

Every output column carries its ``target_type`` (from ``FieldRule.target_type``), whether
it is a copy or a transform, so a backend checks copies against the target type too. The
plan also carries the rule's ``primary_key``.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.contracts.lineage_ops import CopyOp, TransformOp, UnsupportedOp
from src.contracts.transformation_contract import Rule


@dataclass(frozen=True)
class OutputColumn:
    target_column: str
    source_column: str | None
    cast_to: str | None  # a transform's to_type; None for a copy
    target_type: str  # FieldRule.target_type: the type of the target column


@dataclass(frozen=True)
class RulePlan:
    source_table: str
    target_table: str
    columns: tuple[OutputColumn, ...]
    unsupported: tuple[str, ...]
    primary_key: tuple[str, ...] = ()


def plan_rule(rule: Rule) -> RulePlan:
    columns: list[OutputColumn] = []
    unsupported: list[str] = []
    for f in rule.fields:
        if isinstance(f.op, UnsupportedOp):
            unsupported.append(f.target_column)
        elif isinstance(f.op, TransformOp):
            columns.append(
                OutputColumn(f.target_column, f.op.column.column, f.op.to_type, f.target_type)
            )
        elif isinstance(f.op, CopyOp):
            columns.append(OutputColumn(f.target_column, f.op.column.column, None, f.target_type))
    return RulePlan(rule.source_table, rule.target_table, tuple(columns), tuple(unsupported),
                    tuple(rule.primary_key))  # fmt: skip
