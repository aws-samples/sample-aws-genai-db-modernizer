# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Run each valid step 1 rule on synthetic edge-value rows in DuckDB (#1764, ADR-033 Decision 6).

A value Aurora would reject blocks its own rule; a column no DuckDB type can answer
for is a warning and the rest of the rule is still checked.
"""

from __future__ import annotations

from src.contracts.transformation_contract import Finding, Rule, Severity, StepContract
from src.shared.contract_versioning import load_contract
from src.tools.data_movement.plan import JobInputs
from src.tools.synthetic_data.generator import generate_rows
from src.tools.transformation_contract.duckdb_backend import run_rule
from src.tools.transformation_contract.plan_ir import plan_rule
from src.tools.transformation_contract.step1 import _load_collector

_DIALECT = {"mysql": "mysql", "mariadb": "mysql", "postgresql": "postgresql"}
_OWN = {
    "cast_failure", "copy_edge_value", "unknown_target_type", "key_not_unique",
    "default_row_count", "unknown_source_dialect",
}  # fmt: skip
_ZERO_DATE = (
    "Aurora MySQL rejects zero dates when sql_mode has NO_ZERO_DATE/NO_ZERO_IN_DATE with "
    "strict mode (the 8.0 default); the source holds them, so set the target's sql_mode or "
    "clean the data before the DMS load."
)


def _f(
    code: str, severity: Severity, message: str, table: str, column: str | None = None
) -> Finding:
    return Finding(code=code, severity=severity, message=message, table_id=table, column=column)


def _check(rule: Rule, table: dict, dialect: str, engine: str, seed: int) -> Rule:
    tid = rule.source_table
    plan = plan_rule(rule)
    findings = [f for f in rule.findings if f.code not in _OWN]
    synthetic = generate_rows(table, dialect=dialect, seed=seed)
    if synthetic.row_count_basis == "default":
        findings.append(
            _f("default_row_count", "warning",
               "The collection has no row count; the check used a small default.", tid)
        )  # fmt: skip
    run = run_rule(
        plan, synthetic,
        source_columns=[c["column_name"] for c in table["columns"]], engine=engine,
    )  # fmt: skip
    sources = {c.target_column: c.source_column for c in plan.columns}
    for name in run.unchecked_columns:
        findings.append(
            _f("unknown_target_type", "warning",
               f"{name}: no faithful DuckDB type for the target type; this column was not checked",
               tid, sources.get(name))
        )  # fmt: skip
    copies = {c.target_column for c in plan.columns if c.cast_to is None}
    failed = {(f.target_column, str(f.value), f.to_type) for f in run.cast_failures}
    copy_values: dict[str, list[str]] = {}
    for column, value, to_type in sorted(failed):
        if column in copies:
            # A copy keeps the source's own type: the source already holds the value, so
            # this is a target setting or a data cleanup, not a conversion that fails.
            copy_values.setdefault(column, []).append(value)
            continue
        findings.append(
            _f("cast_failure", "error",
               f"`{value}` in `{column}` does not convert to `{to_type}`; Aurora would reject this row.",
               tid, sources.get(column))
        )  # fmt: skip
    for column, edge in copy_values.items():
        shown = ", ".join(f"`{v}`" for v in edge)
        why = (
            _ZERO_DATE if all(v.startswith("0000-00-00") for v in edge)
            else "the target may reject it depending on its settings; check the data before the DMS load."
        )  # fmt: skip
        findings.append(
            _f("copy_edge_value", "warning",
               f"{shown} in `{column}` is copied as is. {why}", tid, sources.get(column))
        )  # fmt: skip
    # A failed cast leaves NULL, already reported above. A primary key column is NOT NULL, so
    # NULLs in a key column that did not fail mean a real collapse.
    failed_columns = {column for column, _, _ in failed}
    key = [
        k for k in plan.primary_key
        if k in sources and k not in run.unchecked_columns and k not in failed_columns
    ]  # fmt: skip
    if key:
        values = [tuple(r[k] for k in key) for r in run.rows]
        if len(set(values)) != len(values) or any(None in v for v in values):
            findings.append(
                _f("key_not_unique", "error",
                   f"The key {', '.join(key)} is not unique after conversion.", tid)
            )  # fmt: skip
    blocked = any(f.severity == "error" for f in findings)
    return rule.model_copy(
        update={"findings": findings, "status": "blocked" if blocked else "valid"}
    )


def shape_check(contract: StepContract, inputs: JobInputs, *, seed: int = 0) -> StepContract:
    """A new contract: each valid rule run on synthetic rows, counts and status recomputed."""
    if not contract.rules:
        return contract
    source = (inputs.collector.get("metadata") or {}).get("source_database") or {}
    engine = str(source.get("engine") or "")
    dialect = _DIALECT.get(engine)
    # The migrated collector, as the pipeline reads it: a 4.x ``row_count: 0`` that the
    # migration turns into "unknown" must not be generated as an empty table.
    migrated = _load_collector(inputs).model_dump(mode="json")
    tables = {t["table_id"]: t for t in (migrated.get("database_schema") or {}).get("tables") or []}
    if dialect is None:
        # No edge values can be chosen without the source dialect: do not guess, skip the run.
        note = (
            f"The source engine {engine!r} has no synthetic-data dialect; "
            "the shape check did not run."
        )
        data = contract.model_dump(mode="json")
        for r in data["rules"]:
            r["findings"] = [f for f in r["findings"] if f["code"] != "unknown_source_dialect"]
            if r["status"] != "blocked":
                r["findings"].append(
                    _f("unknown_source_dialect", "warning", note, r["source_table"]).model_dump(
                        mode="json"
                    )
                )
        return load_contract(StepContract, data, artifact="step 1 contract")
    rules = [
        r if r.status == "blocked" or r.source_table not in tables
        else _check(r, tables[r.source_table], dialect, contract.target.engine, seed)
        for r in contract.rules
    ]  # fmt: skip
    blocked = sum(1 for r in rules if r.status == "blocked")
    data = contract.model_dump(mode="json")
    data.update(
        rules=[r.model_dump(mode="json") for r in rules],
        blocked_rule_count=blocked,
        status="has_blocked_rules" if blocked else "valid",
    )
    return load_contract(StepContract, data, artifact="step 1 contract")
