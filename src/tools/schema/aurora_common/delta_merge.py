"""Merge a model-written delta into the deterministic Aurora draft (issue #273).

The model never transcribes the draft. It writes an ``AuroraDesignDeltaContract``
(index changes, column-type overrides, residual rules by source type,
optimizations, app-layer notes, trade-offs); this module rebuilds the draft
from the same projected input the draft builder uses, applies the delta,
regenerates the DDL and returns a full ``Aurora*ModelOutputContract`` dict.

Pure and deterministic: the same input and delta always give the same output.
Every reference the delta makes (table, column, index) is checked against the
draft; an unknown one is a merge error, never silently dropped (unless the
caller asks for a lenient merge, which records the errors on the output).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from pydantic import ValidationError

from src.contracts.aurora_design_delta import AuroraDesignDeltaContract
from src.contracts.schema_design_input import (
    AgentAnalysisInput,
    AgentCollectorInput,
    AgentTable,
)
from src.tools.schema.aurora_common.ddl_generator import (
    DdlResult,
    TableDDL,
    TypeOverride,
    TypeOverrides,
    assemble_full_ddl,
    generate_mysql_ddl,
    generate_pg_ddl,
)
from src.tools.schema.aurora_common.source_family import migration_strategy

_GENERATORS = {"aurora_postgresql": generate_pg_ddl, "aurora_mysql": generate_mysql_ddl}

_IDENT = r'(?:"(?:[^"]|"")+"|`(?:[^`]|``)+`|[\w$]+)'
_QUALIFIED = rf"{_IDENT}(?:\s*\.\s*{_IDENT})*"
_CREATE_INDEX = re.compile(
    r"^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?(?:IF\s+NOT\s+EXISTS\s+)?"
    rf"(?P<name>{_IDENT})\s+ON\s+(?:ONLY\s+)?(?P<table>{_QUALIFIED})",
    re.IGNORECASE,
)


def _unquote(identifier: str) -> str:
    identifier = identifier.strip()
    if identifier[:1] in ('"', "`") and identifier[-1:] == identifier[:1]:
        q = identifier[0]
        return identifier[1:-1].replace(q + q, q)
    return identifier


def _table_key(name: str) -> str:
    """``discourse.users``, ``"users"`` and ``USERS`` all match draft table ``users``."""
    cleaned = "".join(c for c in name if c not in '`"[]').strip().lower()
    return cleaned.rsplit(".", 1)[-1].strip()


def index_name_of(statement: str) -> str | None:
    """Index name of a ``CREATE [UNIQUE] INDEX`` statement, or ``None`` if it is not one."""
    match = _CREATE_INDEX.match(statement)
    return _unquote(match.group("name")) if match else None


def _index_table_of(statement: str) -> str | None:
    match = _CREATE_INDEX.match(statement)
    return _table_key(match.group("table")) if match else None


@dataclass
class AuroraDesignBase:
    """Everything the draft is built from, plus the source types residual rules match."""

    engine: str
    job_id: str
    source_database: str
    source_engine: str
    tables: list[AgentTable]
    source_data_types: dict[tuple[str, str], str] = field(default_factory=dict)

    @property
    def migration_strategy(self) -> str:
        return migration_strategy(self.source_engine, self.engine)  # type: ignore[arg-type]

    @classmethod
    def from_inputs(
        cls,
        engine: str,
        agent_collector: AgentCollectorInput,
        raw_collector: dict | None = None,
    ) -> AuroraDesignBase:
        """``agent_collector`` is the projected input; ``raw_collector`` supplies ``data_type``."""
        return cls(
            engine=engine,
            job_id=agent_collector.job_id,
            source_database=agent_collector.source_database_name,
            source_engine=agent_collector.source_database_engine,
            tables=list(agent_collector.tables),
            source_data_types=source_data_types(raw_collector or {}),
        )

    def generate(self, type_overrides: TypeOverrides | None = None) -> DdlResult:
        return _GENERATORS[self.engine](self.tables, type_overrides)


def source_data_types(raw_collector: dict) -> dict[tuple[str, str], str]:
    """``(table_name, column_name) -> data_type`` from a raw collector output dict."""
    types: dict[tuple[str, str], str] = {}
    for table in (raw_collector.get("database_schema") or {}).get("tables") or []:
        for col in table.get("columns") or []:
            if col.get("data_type"):
                types[(table.get("table_name", ""), col.get("column_name", ""))] = str(
                    col["data_type"]
                )
    return types


@dataclass
class MergeResult:
    output: dict | None
    errors: list[str]
    summary: dict


def _resolve_column(table: AgentTable, name: str) -> str | None:
    names = [c.column_name for c in table.columns]
    if name in names:
        return name
    lowered = [n for n in names if n.lower() == name.lower()]
    return lowered[0] if len(lowered) == 1 else None


def _type_overrides(
    base: AuroraDesignBase,
    delta: AuroraDesignDeltaContract,
    by_key: dict[str, AgentTable],
    residuals: list[dict],
    errors: list[str],
) -> tuple[TypeOverrides, int]:
    overrides: TypeOverrides = {}
    for i, change in enumerate(delta.tables):
        table = by_key.get(_table_key(change.table_name))
        if table is None:
            continue  # reported by the caller
        for j, col_change in enumerate(change.column_types):
            column = _resolve_column(table, col_change.column)
            if column is None:
                errors.append(
                    f"tables[{i}].column_types[{j}]: table '{table.table_name}' has no "
                    f"column '{col_change.column}'"
                )
                continue
            overrides[(table.table_name, column)] = TypeOverride(
                col_change.aurora_type, col_change.reason or "Set by the schema designer."
            )

    rules = {r.source_data_type.strip().lower(): r for r in delta.type_rules}
    by_rule = 0
    if rules:
        for residual in residuals:
            key = (residual["table"], residual["column"])
            if key in overrides:
                continue
            raw = base.source_data_types.get(key, "")
            rule = rules.get(raw.strip().lower())
            if rule is not None:
                overrides[key] = TypeOverride(
                    rule.aurora_type,
                    rule.reason or f"Residual {raw} columns resolved by the schema designer.",
                )
                by_rule += 1
    return overrides, by_rule


def _check_statement(statement: str, table_name: str, where: str, errors: list[str]) -> bool:
    name = index_name_of(statement)
    if name is None:
        errors.append(f"{where}: not a CREATE [UNIQUE] INDEX ... ON ... statement: {statement!r}")
        return False
    if ";" in statement.strip().rstrip(";"):
        errors.append(f"{where}: one CREATE INDEX statement per entry: {statement!r}")
        return False
    if _index_table_of(statement) != _table_key(table_name):
        errors.append(f"{where}: index '{name}' is not ON table '{table_name}'")
        return False
    return True


def _normalize_statement(statement: str) -> str:
    return statement.strip().rstrip(";").rstrip() + ";"


def _apply_index_changes(
    delta: AuroraDesignDeltaContract,
    by_key: dict[str, AgentTable],
    table_ddls: dict[str, TableDDL],
    errors: list[str],
) -> dict[str, int]:
    counts = {"added": 0, "modified": 0, "removed": 0}
    for i, change in enumerate(delta.tables):
        table = by_key.get(_table_key(change.table_name))
        if table is None:
            continue
        ddl = table_ddls[table.table_name]
        current = {index_name_of(s): s for s in ddl.index_sql}
        statements = list(ddl.index_sql)

        for j, name in enumerate(change.remove_indexes):
            stmt = current.pop(_unquote(name), None)
            if stmt is None:
                errors.append(
                    f"tables[{i}].remove_indexes[{j}]: table '{table.table_name}' has no "
                    f"index '{name}'"
                )
                continue
            statements.remove(stmt)
            counts["removed"] += 1

        for j, mod in enumerate(change.modify_indexes):
            where = f"tables[{i}].modify_indexes[{j}]"
            stmt = current.get(_unquote(mod.index_name))
            if stmt is None:
                errors.append(
                    f"{where}: table '{table.table_name}' has no index '{mod.index_name}'"
                )
                continue
            if not _check_statement(mod.statement, table.table_name, where, errors):
                continue
            new_name = index_name_of(mod.statement)
            if new_name != _unquote(mod.index_name) and new_name in current:
                errors.append(f"{where}: index '{new_name}' already exists")
                continue
            new_stmt = _normalize_statement(mod.statement)
            statements[statements.index(stmt)] = new_stmt
            del current[_unquote(mod.index_name)]
            current[new_name] = new_stmt
            counts["modified"] += 1

        for j, statement in enumerate(change.add_indexes):
            where = f"tables[{i}].add_indexes[{j}]"
            if not _check_statement(statement, table.table_name, where, errors):
                continue
            added = index_name_of(statement)
            if added in current:
                errors.append(f"{where}: index '{added}' already exists on '{table.table_name}'")
                continue
            new_stmt = _normalize_statement(statement)
            statements.append(new_stmt)
            current[added] = new_stmt
            counts["added"] += 1

        ddl.index_sql = statements
    return counts


def _default_trade_off(base: AuroraDesignBase, n_tables: int) -> dict:
    strategy = base.migration_strategy
    how = (
        "carried over 1:1 from the source"
        if strategy == "carry_over"
        else "translated deterministically from the source types"
    )
    return {
        "description": (
            f"The {n_tables} tables are {how}; the deterministic draft's types, keys, "
            "indexes and foreign keys are kept unless listed as a change."
        ),
        "impact": (
            "The relational model the application already uses stays as it is, so the "
            "migration risk sits in data movement and cut-over rather than in a redesign."
        ),
        "engine": base.engine,
    }


def merge_design_delta(
    base: AuroraDesignBase, delta: dict | AuroraDesignDeltaContract, *, strict: bool = True
) -> MergeResult:
    """Apply ``delta`` to the draft built from ``base``.

    Strict (``--finalize``): any merge error returns ``output=None`` with the
    errors, so the caller fixes the delta. Lenient (Bedrock, after its
    correction attempt): invalid entries are skipped and the errors are
    recorded as ``validation_failures`` with ``validation_passed=false``.
    """
    if isinstance(delta, AuroraDesignDeltaContract):
        contract = delta
    else:
        try:
            contract = AuroraDesignDeltaContract.model_validate(delta)
        except ValidationError as exc:
            return MergeResult(None, [f"Invalid design delta: {exc}"], {})

    errors: list[str] = []
    by_key = {_table_key(t.table_name): t for t in base.tables}
    seen: set[str] = set()
    for i, change in enumerate(contract.tables):
        key = _table_key(change.table_name)
        if key not in by_key:
            errors.append(
                f"tables[{i}]: unknown table '{change.table_name}'; the draft has no such "
                "table (use a table_name from the design view)"
            )
        elif key in seen:
            errors.append(f"tables[{i}]: table '{change.table_name}' is listed more than once")
        seen.add(key)

    draft = base.generate()
    overrides, by_rule = _type_overrides(base, contract, by_key, draft.residuals, errors)
    ddl = base.generate(overrides)
    table_ddls = {t.table_name: t for t in ddl.tables}
    index_counts = _apply_index_changes(contract, by_key, table_ddls, errors)

    summary = {
        "tables_changed": len(seen & set(by_key)),
        "column_types_set": len(overrides) - by_rule,
        "residuals_resolved_by_rule": by_rule,
        "residuals_unresolved": len(ddl.residuals),
        "indexes_added": index_counts["added"],
        "indexes_modified": index_counts["modified"],
        "indexes_removed": index_counts["removed"],
    }
    if errors and strict:
        return MergeResult(None, errors, summary)

    trade_offs = [t.model_dump(mode="json") for t in contract.trade_offs] or [
        _default_trade_off(base, len(base.tables))
    ]
    output = {
        "contract_version": "1.0",
        "job_id": base.job_id,
        "source_database": base.source_database,
        "target_engine": base.engine,
        "migration_strategy": base.migration_strategy,
        "table_definitions": [
            {
                "table_name": t.table_name,
                "columns": [
                    {
                        "name": c.name,
                        "aurora_type": c.aurora_type,
                        "source_type": c.source_type,
                        "script_derived": c.script_derived,
                        "needs_judgment": c.needs_judgment,
                    }
                    for c in t_ddl.columns
                ],
                "primary_key": list(t.primary_key or []),
                "indexes": list(t_ddl.index_sql),
                "foreign_keys": list(t_ddl.fk_sql),
            }
            for t, t_ddl in ((t, table_ddls[t.table_name]) for t in base.tables)
        ],
        "generated_ddl": assemble_full_ddl(ddl.tables),
        "app_layer_notes": [n.model_dump(mode="json") for n in contract.app_layer_notes],
        "optimizations": [o.model_dump(mode="json") for o in contract.optimizations],
        "trade_offs": trade_offs,
        "validation_passed": not errors,
        "validation_failures": [f"Design delta: {e}" for e in errors],
    }
    return MergeResult(output, errors, summary)


def base_from_outputs(
    engine: str, collector_output: dict, analysis_output: dict
) -> tuple[AuroraDesignBase, AgentCollectorInput, AgentAnalysisInput]:
    """Project raw (assignment-filtered) collector + analysis dicts into a design base.

    The same projection ``scripts/run_schema_design.py`` and the Bedrock agent
    build the draft from, so prepare and ``--finalize`` see the same draft.
    Returns ``(base, agent_collector, agent_analysis)``.
    """
    from src.contracts.analysis_output import AnalysisOutputContract
    from src.contracts.collector_output import CollectorOutputContract
    from src.contracts.schema_design_input import project_schema_design_input

    collector = CollectorOutputContract.model_validate(collector_output)
    analysis = AnalysisOutputContract.model_validate(analysis_output)
    agent_collector, agent_analysis, _ = project_schema_design_input(collector, analysis)
    base = AuroraDesignBase.from_inputs(engine, agent_collector, collector_output)
    return base, agent_collector, agent_analysis
