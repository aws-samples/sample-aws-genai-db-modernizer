# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Validate step 1's rules against the collector and the Aurora DDL (#1764).

An error blocks its own rule; a warning never blocks. The Aurora tables are the ones
step 1 needs (``target_tables``: the generator's, with the saved design overlaid).
"""

from __future__ import annotations

from src.contracts.collector_output import Table
from src.contracts.lineage_ops import UnsupportedOp
from src.contracts.transformation_contract import Finding, Rule, Severity, StepContract
from src.shared.contract_versioning import load_contract
from src.tools.data_movement.plan import JobInputs, _aurora_wave
from src.tools.transformation_contract.pg_enums import enum_columns
from src.tools.transformation_contract.step1 import (
    TargetTable,
    _load_collector,
    _same_type,
    target_tables,
)

NO_KEY_MESSAGE = (
    "No primary or unique key: DMS cannot validate this table, "
    "and Debezium needs declared key columns."
)
# Findings the builder or this validator wrote about a rule that cannot be re-checked.
_KEPT = {"ambiguous_target", "missing_table"}


def _f(
    code: str, severity: Severity, message: str, table: str | None = None, column: str | None = None
) -> Finding:
    return Finding(code=code, severity=severity, message=message, table_id=table, column=column)


def _has_key(table: Table) -> bool:
    if table.primary_key:
        return True
    return any(i.is_unique and not i.predicate for i in table.indexes or [])


def _rule_findings(
    rule: Rule, tables: dict[str, Table], targets: dict[str, TargetTable]
) -> list[Finding]:
    tid = rule.source_table
    t = tables.get(tid)
    if t is None:
        return [_f("unknown_table", "error", f"{tid} is not in the collection", tid)]
    out: list[Finding] = []
    cols = {c.column_name for c in t.columns}
    target = targets.get(rule.target_table)
    design = {c.name: c.aurora_type for c in (target.columns if target else ())}
    for f in rule.fields:
        ref = getattr(f.op, "column", None)
        if ref is not None and ref.column not in cols:
            out.append(
                _f(
                    "unknown_column",
                    "error",
                    f"{ref.column} is not a column of {tid}",
                    tid,
                    ref.column,
                )
            )
        if f.target_column in design and not _same_type(design[f.target_column], f.target_type):
            out.append(
                _f(
                    "type_mismatch",
                    "error",
                    f"{f.target_column} is {f.target_type} here but {design[f.target_column]} "
                    "in the Aurora DDL",
                    tid,
                    f.source_column,
                )
            )
        if isinstance(f.op, UnsupportedOp):
            out.append(
                _f(
                    "unsupported_field",
                    "warning",
                    f"{f.target_column}: {f.op.description}",
                    tid,
                    f.source_column,
                )
            )
    for name in sorted(cols - design.keys()):
        out.append(
            _f(
                "missing_target_column",
                "error",
                f"{name} has no column in the Aurora DDL",
                tid,
                name,
            )
        )
    for name in sorted(design.keys() - cols):
        out.append(
            _f(
                "extra_target_column",
                "error",
                f"{name} is in the Aurora DDL but not in the collection",
                tid,
                name,
            )
        )
    if not _has_key(t):
        out.append(_f("no_key", "warning", NO_KEY_MESSAGE, tid))
    return out


def _checked(rule: Rule, findings: list[Finding]) -> Rule:
    blocked = any(f.severity == "error" for f in findings)
    return rule.model_copy(
        update={"findings": findings, "status": "blocked" if blocked else "valid"}
    )


def _revalidated(rule: Rule, tables: dict[str, Table], targets: dict[str, TargetTable]) -> Rule:
    if not any(f.code in _KEPT for f in rule.findings):
        return _checked(rule, _rule_findings(rule, tables, targets))
    # A kept rule has no fields to re-check, but its table may have left the collection.
    kept = list(rule.findings)
    if rule.source_table not in tables:
        kept += _rule_findings(rule, tables, targets)
    unique: dict[str, Finding] = {}
    for f in kept:
        unique.setdefault(f.code, f)
    return _checked(rule, list(unique.values()))


def _with_enum_findings(rule: Rule, fallbacks: dict[tuple[str, str], tuple[str, str]]) -> Rule:
    """A warning per field of the rule that stays TEXT although its PostgreSQL type may be
    an enum (#1834): no labels collected, or a type step 1 refuses to create."""
    added = []
    for f in rule.fields:
        if (rule.source_table, f.source_column) in fallbacks:
            code, why = fallbacks[(rule.source_table, f.source_column)]
            added.append(_f(code, "warning", why, rule.source_table, f.source_column))
    return rule.model_copy(update={"findings": [*rule.findings, *added]}) if added else rule


def validate_step1(contract: StepContract, inputs: JobInputs) -> StepContract:
    """A new contract with each rule's findings and status, and the step's counts, recomputed."""
    if contract.status in ("no_relational_anchor", "staged_move_pending"):
        return contract
    collector = _load_collector(inputs)
    tables = {t.table_id: t for t in collector.database_schema.tables}
    targets = target_tables(inputs, contract.target.engine, collector)
    fallbacks = enum_columns(collector, contract.target.engine).fallbacks
    rules = [
        _with_enum_findings(_revalidated(r, tables, targets), fallbacks) for r in contract.rules
    ]
    waves = sorted(inputs.report.get("migration_waves") or [], key=lambda w: int(w["wave"]))
    wave = (_aurora_wave(waves) if waves else None) or {}
    ruled = {r.source_table for r in rules} | set(contract.views)
    for missing in sorted(set(wave.get("tables") or []) - ruled):
        t = tables.get(missing)
        name = t.table_name if t else missing.split(".")[-1]
        finding = _f("missing_table", "error", f"{missing} is in wave 1 but has no rule", missing)
        rules.append(
            Rule(
                input_schema="collector",
                source_table=missing,
                target_table=name,
                status="blocked",
                findings=[finding],
            )
        )
    blocked = sum(1 for r in rules if r.status == "blocked")
    data = contract.model_dump(mode="json")
    data.update(
        rules=[r.model_dump(mode="json") for r in rules],
        blocked_rule_count=blocked,
        status="has_blocked_rules" if blocked else "valid",
    )
    return load_contract(StepContract, data, artifact="step 1 contract")
